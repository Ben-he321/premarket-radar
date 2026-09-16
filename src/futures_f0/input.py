"""Strict normalized-input reader; no implicit vendor/calendar transformation.

The raw Databento definitions/statistics-to-manifest integration still needs
actual-source qualification. A downloaded CSV is never accepted automatically.
"""
from dataclasses import fields,asdict
from datetime import date
import csv
import json
from pathlib import Path
import math

from .data import verify_input_manifest, timestamp, SessionWindow
from .model import ContractSpec, SessionBar, MarketDay, RollInstruction
from .protocol import MARKETS
from .runtime import digest,canonical_hash
from .provenance import verify_session_derivation, verify_bar_derivation
from .session_inputs import capture_prefix_clock
from .vendor import EvidenceFile, timestamp_ns,model_time


def small_json(path, limit=8*1024**2):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError('METADATA_SIZE_BOUNDARY')
    return json.loads(path.read_text(encoding='utf-8-sig'), parse_constant=lambda x: (_ for _ in ()).throw(ValueError('NONFINITE_JSON')))


class QualifiedInputs:
    def __init__(self, manifest_path, registry_path, *, guard=None):
        self.path = Path(manifest_path).resolve()
        self.manifest = verify_input_manifest(self.path)
        self.manifest_hash = digest(self.path)
        self.quarantine = []
        self.quarantine_count = 0
        self.files = {name: self.path.parent / value['path'] for name,value in self.manifest.items()
                      if isinstance(value,dict) and 'path' in value}
        self.specs = {}
        with Path(registry_path).open(encoding='utf-8-sig', newline='') as f:
            registry = {r['root']:r for r in csv.DictReader(f)}
        self.registry = registry
        self.qualification_context=None;self.qualification_catalog=None
        if self.manifest.get('qualified_source_context'):
            from .qualification import load_context
            entry=self.manifest['qualified_source_context'];p=Path(entry['path']).resolve()
            if not p.is_relative_to(Path(self.manifest['derivation_source_root']).resolve()):
                raise ValueError('QUALIFIED_CONTEXT_OUTSIDE_SOURCE_ROOT')
            self.qualification_context,self.qualification_catalog=load_context(
                EvidenceFile(p,entry['sha256'],entry['source']),guard=guard)
            if any(x['source_sha256'] not in self.manifest.get('source_object_hashes',[])
                   for x in self.qualification_context['sources']):
                raise ValueError('QUALIFIED_CONTEXT_OBJECT_NOT_IN_MANIFEST')
        self.settlements=small_json(self.files['settlements']) if self.qualification_context else None
        self.statuses=small_json(self.files['status']) if self.qualification_context else None
        self.mappings=small_json(self.files['mapping']) if self.qualification_context else None
        self.roll_decisions=None
        if self.qualification_context:
            entry=self.manifest.get('roll_decisions',{});p=(self.path.parent/entry.get('path','')).resolve()
            if not p.is_relative_to(self.path.parent) or not p.is_file() or digest(p)!=entry.get('sha256'):
                raise ValueError('SOURCE_BOUND_ROLL_DECISIONS_REQUIRED')
            self.roll_decisions=small_json(p)
            if not isinstance(self.roll_decisions,list) or len(self.roll_decisions)>32:raise ValueError('BOUNDED_ROLL_DECISIONS_REQUIRED')
        entries = small_json(self.files['definitions'])
        if not isinstance(entries,list) or not 1 <= len(entries) <= 1000:
            raise ValueError('INVALID_BOUNDED_DEFINITIONS')
        seen_contracts = set()
        for entry in entries:
            identity = entry.get('contract_id')
            if identity in seen_contracts:
                self.specs.pop(identity,None)
                self._quarantine(entry.get('market'),identity,'DUPLICATE_SPEC_VERSION_REQUIRES_DATED_RESOLUTION')
                continue
            seen_contracts.add(identity)
            try:
                spec = self._parse_spec(entry, registry)
                if spec.contract_id in self.specs:
                    raise ValueError('DUPLICATE_SPEC_VERSION_REQUIRES_DATED_RESOLUTION')
                self.specs[spec.contract_id] = spec
            except (ValueError, TypeError, KeyError) as error:
                self._quarantine(entry.get('market'), entry.get('contract_id'), str(error))
        if not self.specs:
            raise ValueError('NO_QUALIFIED_CONTRACT_DEFINITIONS')
        self.calendars = small_json(self.files['calendar'])
        if not isinstance(self.calendars,dict) or len(self.calendars)>20000:
            raise ValueError('INVALID_BOUNDED_SESSION_CALENDAR')
        if self.manifest.get('integration_review') != 'VERIFIED_RAW_TO_NORMALIZED':
            raise ValueError('VENDOR_NORMALIZATION_REVIEW_REQUIRED')

    def _parse_spec(self, entry, registry):
        allowed = {f.name for f in fields(ContractSpec)}
        if set(entry) - allowed:
            raise ValueError('UNKNOWN_CONTRACT_DEFINITION_FIELD')
        data = dict(entry)
        for name in ('listed','last_trade','safe_exit_session','margin_valid_until','eligible_from'):
            if data.get(name): data[name] = date.fromisoformat(data[name])
        if data.get('margin_asof'): data['margin_asof'] = timestamp(data['margin_asof'])
        spec = ContractSpec(**data)
        row = registry.get(spec.root)
        if row is None or spec.market != row['market'] or spec.currency != 'USD':
            raise ValueError('OUTSIDE_FROZEN_MARKET_DEFINITIONS')
        if spec.exchange != row['exchange'] or spec.quote_unit != row['quote_unit']:
            raise ValueError('EXCHANGE_OR_QUOTE_UNIT_LABEL_MISMATCH')
        if not spec.verified or not spec.vendor_definition_verified or not spec.calendar_verified or not spec.boundary_verified:
            raise ValueError('SPEC_NOT_DUAL_VERIFIED')
        if spec.multiplier != float(row['usd_multiplier_per_quote_unit']) or spec.tick_size != float(row['tick_in_quote_units']):
            raise ValueError('VENDOR_EXCHANGE_SPEC_UNIT_MISMATCH')
        eligible=spec.eligible_from or spec.listed
        if eligible is None or eligible < date.fromisoformat(row['root_first_trade_date']) or eligible > spec.last_trade:
            raise ValueError('PRE_LAUNCH_OR_INVALID_CONTRACT_LIFETIME')
        if spec.eligible_from is not None or spec.listed is None:
            if self.qualification_context is None:raise ValueError('OBSERVED_EXISTENCE_RAW_PROOF_REQUIRED')
            from .qualification import qualified_contract
            proof=qualified_contract(self.qualification_catalog,self.qualification_context,spec.contract_id,row)
            if (spec.listed is not None or str(spec.eligible_from)!=proof['eligible_from'] or
                spec.eligibility_basis!=proof['eligibility_basis'] or spec.qualification_sha256!=canonical_hash(proof)
                or str(spec.last_trade)!=proof['last_trade'] or str(spec.safe_exit_session)!=proof['safe_exit_session']):
                raise ValueError('OBSERVED_EXISTENCE_OR_BOUNDARY_PROOF_MISMATCH')
        return spec

    def _bar(self, item, guard=None):
        # Model defaults are convenient for engineering fixtures. Production
        # imports must never interpret omitted halt/limit evidence as tradable.
        explicit = {'status', 'tradable_open', 'tradable_stop', 'session_verified', 'is_mock'}
        if not explicit <= set(item):
            raise ValueError('EXPLICIT_BAR_QUALIFICATION_AND_EXECUTION_FLAGS_REQUIRED')
        if any(type(item[name]) is not bool for name in explicit - {'status'}):
            raise ValueError('BAR_EVIDENCE_FLAGS_MUST_BE_BOOLEAN')
        if not isinstance(item['status'], str) or not item['status']:
            raise ValueError('EXPLICIT_BAR_STATUS_REQUIRED')
        data = dict(item)
        for name in ('open', 'high', 'low', 'close', 'settlement'):
            if data.get(name) is not None:
                if isinstance(data[name], bool):
                    raise ValueError('INVALID_NORMALIZED_PRICE')
                data[name] = float(data[name])
                if not math.isfinite(data[name]):
                    raise ValueError('INVALID_NORMALIZED_PRICE')
        for name in ('session','next_session'):
            if data.get(name): data[name] = date.fromisoformat(data[name])
        for name in ('opens_at','closes_at','available_at','received_at','settlement_available_at','settlement_reference_at',
                     'input_cutoff','internal_calculated_at','supplier_published_at','calendar_confirmed_at'):
            if data.get(name): data[name] = model_time(timestamp_ns(data[name]))
        bar = SessionBar(**data)
        if self.manifest.get('session_derivations') and bar.availability_basis != 'INTERNAL_CAPTURE_PREFIX':
            raise ValueError('DERIVATION_MANIFEST_REQUIRES_CAPTURE_PREFIX_NO_RAW_FALLBACK')
        if bar.availability_basis == 'INTERNAL_CAPTURE_PREFIX':
            evidence_entry = self.manifest.get('temporal_evidence', {})
            evidence_path = self.path.parent / evidence_entry.get('path', '')
            if (not evidence_path.is_file() or not evidence_path.resolve().is_relative_to(self.path.parent)
                    or digest(evidence_path) != evidence_entry.get('sha256')
                    or bar.temporal_evidence_hash != evidence_entry.get('sha256')):
                raise ValueError('CAPTURE_PREFIX_EVIDENCE_MISSING_OR_CHANGED')
            derived_entry = self.manifest.get('session_derivations', {}).get(bar.source_hash)
            if not isinstance(derived_entry, dict):
                raise ValueError('CAPTURE_PREFIX_SESSION_DERIVATION_REQUIRED')
            derived_path = (self.path.parent / derived_entry['path']).resolve()
            if (not derived_path.is_relative_to(self.path.parent) or not derived_path.is_file()
                    or digest(derived_path) != derived_entry['sha256']):
                raise ValueError('SESSION_DERIVATION_FILE_CHANGED_OR_OUTSIDE_MANIFEST')
            derived = small_json(derived_path)
            if derived.get('derivation_sha256') != bar.source_hash:
                raise ValueError('BAR_SESSION_DERIVATION_HASH_MISMATCH')
            capture_prefix_clock(EvidenceFile(evidence_path, evidence_entry['sha256'], evidence_entry['source']),
                start=timestamp_ns(item['opens_at']), end=timestamp_ns(item['closes_at']),
                used_schemas={record['schema'] for record in derived['prices']['records']})
            root = self.manifest.get('derivation_source_root')
            if not root or not Path(root).is_absolute():
                raise ValueError('EXPLICIT_DERIVATION_SOURCE_ROOT_REQUIRED')
            expected_registry = self.registry.get(derived.get('root'))
            if expected_registry is None:
                raise ValueError('DERIVATION_REGISTRY_ROOT_UNKNOWN')
            verify_session_derivation(derived, source_root=root,
                raw_hashes=self.manifest.get('source_object_hashes', []),
                registry_row=expected_registry, guard=guard)
            verify_bar_derivation(item, derived)
            if (bar.input_cutoff != bar.closes_at or bar.internal_calculated_at != max(bar.input_cutoff,bar.calendar_confirmed_at or bar.input_cutoff)
                    or bar.available_at != bar.internal_calculated_at or bar.supplier_published_at is not None
                    or bar.received_at is not None):
                raise ValueError('CAPTURE_PREFIX_CLOCKS_OR_UNKNOWN_RECEIPT_MISREPRESENTED')
        elif bar.availability_basis != 'SUPPLIER_PUBLICATION':
            raise ValueError('UNKNOWN_AVAILABILITY_BASIS')
        elif any(value is not None for value in (bar.input_cutoff, bar.internal_calculated_at, bar.temporal_evidence_hash)):
            raise ValueError('SUPPLIER_CLOCK_CANNOT_HIDE_INTERNAL_CAPTURE_PREFIX')
        elif bar.supplier_published_at is not None and bar.supplier_published_at != bar.available_at:
            raise ValueError('SUPPLIER_PUBLICATION_CLOCK_MISMATCH')
        spec = self.specs.get(bar.contract_id)
        if spec is None or bar.is_mock:
            raise ValueError('REAL_SPECIFIC_CONTRACT_REQUIRED')
        if bar.availability_basis == 'INTERNAL_CAPTURE_PREFIX' and (
                derived['root'] != spec.root or derived['market'] != spec.market):
            raise ValueError('DERIVATION_CONTRACT_SPEC_IDENTITY_MISMATCH')
        if not date(2021,1,1) <= bar.session <= date(2025,12,31):
            raise ValueError('BAR_OUTSIDE_FROZEN_HISTORY')
        key = bar.contract_id + '|' + bar.session.isoformat()
        if self.qualification_context:
            if not derived.get('research_qualified'):raise ValueError('COMPUTED_INPUT_NOT_QUALIFIED')
            if (self.settlements.get(key)!=derived['settlement'] or self.statuses.get(key)!=derived['execution_status']):
                raise ValueError('SETTLEMENT_OR_STATUS_MANIFEST_NOT_BOUND_TO_RAW_DERIVATION')
        calendar = self.calendars.get(key)
        if not calendar:
            raise ValueError('MISSING_VERIFIED_SESSION_MAPPING:' + key)
        if calendar['timezone']!='America/Chicago':
            raise ValueError('WRONG_EXCHANGE_CALENDAR_TIMEZONE')
        segments = tuple((timestamp(a),timestamp(b)) for a,b in calendar['segments'])
        window = SessionWindow(bar.session, calendar['timezone'], segments, calendar['source'],
                               calendar['evidence_sha256'], calendar.get('verified') is True,calendar.get('dated_evidence'))
        window.validate(guard=guard)
        if bar.availability_basis == 'INTERNAL_CAPTURE_PREFIX':
            actual_segments = [(timestamp_ns(a), timestamp_ns(b)) for a, b in calendar['segments']]
            derived_segments = [(timestamp_ns(a), timestamp_ns(b)) for a, b in derived['calendar']['segments']]
            if (actual_segments != derived_segments or
                    calendar['evidence_sha256'] != derived['calendar']['evidence']['sha256']):
                raise ValueError('DERIVATION_VERIFIED_CALENDAR_SEGMENTS_OR_EVIDENCE_MISMATCH')
        if bar.opens_at != segments[0][0] or bar.closes_at != segments[-1][1]:
            raise ValueError('BAR_DOES_NOT_MATCH_DATED_EXCHANGE_SESSION')
        if (bar.next_session.isoformat() if bar.next_session else None) != calendar.get('next_session'):
            raise ValueError('NEXT_SESSION_NOT_FROM_VERIFIED_CALENDAR')
        if bar.available_at < bar.closes_at or (bar.availability_basis != 'INTERNAL_CAPTURE_PREFIX'
                and bar.source_hash not in self.manifest.get('source_object_hashes',[])):
            raise ValueError('AVAILABILITY_OR_SOURCE_OBJECT_UNKNOWN')
        return bar

    def batches(self, guard=None):
        previous = None
        with self.files['bars'].open(encoding='utf-8-sig') as f:
            while True:
                line = f.readline(1024*1024 + 1)
                if not line: break
                if len(line)>1024*1024: raise ValueError('NORMALIZED_BATCH_SIZE_LIMIT')
                if not line.strip(): continue
                if guard: guard.check({'stage':'read_normalized_batch','last_session':str(previous)})
                rows = json.loads(line)
                if not isinstance(rows,list) or not 1 <= len(rows)<=6:
                    raise ValueError('MAX_SIX_MARKETS_PER_BATCH')
                days=[]
                for row in rows:
                    market=row['market']
                    if market not in MARKETS or len(row.get('execution',[]))>6:
                        raise ValueError('FROZEN_MARKET_OR_EXPIRY_BOUNDARY')
                    try:
                        signal=self._bar(row['signal'], guard=guard)
                    except (ValueError,TypeError,KeyError) as error:
                        self._quarantine(market,row['signal'].get('contract_id'),str(error))
                        continue
                    if self.specs[signal.contract_id].root != MARKETS[market]['signal']:
                        raise ValueError('WRONG_STANDARD_SIGNAL_CONTRACT')
                    execution=[]
                    for b in row['execution']:
                        try:
                            execution.append(self._bar(b, guard=guard))
                        except (ValueError,TypeError,KeyError) as error:
                            self._quarantine(market,b.get('contract_id'),str(error))
                    execution=tuple(execution)
                    if any(self.specs[b.contract_id].root not in MARKETS[market]['execution'] for b in execution):
                        raise ValueError('WRONG_EXECUTION_ROOT')
                    if self.qualification_context:
                        mapping=self.mappings.get(market+'|'+str(signal.session))
                        expected=dict(market=market,signal=signal.contract_id,execution=[b.contract_id for b in execution],
                                      session=str(signal.session),quote_unit=self.specs[signal.contract_id].quote_unit)
                        if mapping!=expected or any(self.specs[b.contract_id].quote_unit!=expected['quote_unit'] for b in execution):
                            raise ValueError('DATED_STANDARD_MICRO_MAPPING_PROOF_MISMATCH')
                        if not row.get('mapping_verified') or row.get('mapping_source')!='RAW_DEFINITION_IDENTITY_AND_FROZEN_UNIT_MAPPING':
                            raise ValueError('COMPUTED_MAPPING_ATTESTATION_MISMATCH')
                    roll=None
                    if row.get('roll'):
                        r=dict(row['roll']);r['known_at']=timestamp(r['known_at']);roll=RollInstruction(**r)
                        if roll.evidence_hash not in self.manifest.get('verified_roll_decision_hashes',[]):
                            raise ValueError('CAUSAL_ROLL_DECISION_EVIDENCE_REQUIRED')
                        if self.qualification_context:
                            self._verify_roll(roll,signal.session,guard)
                    days.append(MarketDay(market,signal,execution,roll,row.get('mapping_verified') is True,
                                          row.get('mapping_source','UNKNOWN')))
                if not days: continue
                session=days[0].signal.session
                if previous is not None and session<=previous:
                    raise ValueError('INPUT_SESSION_OUT_OF_ORDER')
                previous=session
                yield days

    def _verify_roll(self,roll,session,guard):
        from .rolls import decide_roll
        entries=[x for x in self.roll_decisions if x['instruction']['evidence_hash']==roll.evidence_hash]
        if len(entries)!=1:raise ValueError('UNIQUE_SOURCE_BOUND_ROLL_PROOF_REQUIRED')
        entry=entries[0];instruction=dict(entry['instruction']);instruction['known_at']=timestamp(instruction['known_at'])
        if RollInstruction(**instruction)!=roll:raise ValueError('ROLL_INSTRUCTION_CHANGED_FROM_BOUND_PROOF')
        hashes=entry['prior_derivation_hashes']
        if len(hashes) not in (4,8):raise ValueError('ROLL_REQUIRES_BOUNDED_PRIOR_FOUR_WAY_OVERLAP')
        bars=[]
        for h in hashes:
            d=self.manifest['session_derivations'][h];p=(self.path.parent/d['path']).resolve()
            if not p.is_relative_to(self.path.parent) or digest(p)!=d['sha256']:raise ValueError('ROLL_OVERLAP_DERIVATION_CHANGED')
            value=small_json(p)
            if value['derivation_sha256']!=h:raise ValueError('ROLL_OVERLAP_HASH_CHANGED')
            bars.append(self._bar(value['engine_record_candidate'],guard=guard))
        pairs=[bars[n:n+4] for n in range(0,len(bars),4)]
        actual=decide_roll(self.specs[roll.old_contract],self.specs[roll.new_contract],
            [(x[0],x[1]) for x in pairs],decision_at=roll.known_at,next_session=session,
            signal_overlap=(pairs[-1][2],pairs[-1][3]))
        if actual!=roll:raise ValueError('ROLL_RAW_OVERLAP_RECOMPUTATION_MISMATCH')

    def _quarantine(self, market, contract, reason):
        self.quarantine_count += 1
        if len(self.quarantine)<1000:
            self.quarantine.append(dict(market=market,contract_id=contract,reason=reason))
