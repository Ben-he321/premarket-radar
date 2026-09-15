"""Strict normalized-input reader; no implicit vendor/calendar transformation.

The raw Databento definitions/statistics-to-manifest integration still needs
actual-source qualification. A downloaded CSV is never accepted automatically.
"""
from dataclasses import fields
from datetime import date
import csv
import json
from pathlib import Path

from .data import verify_input_manifest, timestamp, SessionWindow
from .model import ContractSpec, SessionBar, MarketDay, RollInstruction
from .protocol import MARKETS
from .runtime import digest


def small_json(path, limit=8*1024**2):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError('METADATA_SIZE_BOUNDARY')
    return json.loads(path.read_text(encoding='utf-8-sig'), parse_constant=lambda x: (_ for _ in ()).throw(ValueError('NONFINITE_JSON')))


class QualifiedInputs:
    def __init__(self, manifest_path, registry_path):
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
        for name in ('listed','last_trade','safe_exit_session','margin_valid_until'):
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
        if spec.listed < date.fromisoformat(row['root_first_trade_date']) or spec.listed > spec.last_trade:
            raise ValueError('PRE_LAUNCH_OR_INVALID_CONTRACT_LIFETIME')
        return spec

    def _bar(self, item):
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
        for name in ('session','next_session'):
            if data.get(name): data[name] = date.fromisoformat(data[name])
        for name in ('opens_at','closes_at','available_at','received_at','settlement_available_at','settlement_reference_at'):
            if data.get(name): data[name] = timestamp(data[name])
        bar = SessionBar(**data)
        spec = self.specs.get(bar.contract_id)
        if spec is None or bar.is_mock:
            raise ValueError('REAL_SPECIFIC_CONTRACT_REQUIRED')
        if not date(2021,1,1) <= bar.session <= date(2025,12,31):
            raise ValueError('BAR_OUTSIDE_FROZEN_HISTORY')
        key = bar.contract_id + '|' + bar.session.isoformat()
        calendar = self.calendars.get(key)
        if not calendar:
            raise ValueError('MISSING_VERIFIED_SESSION_MAPPING:' + key)
        if calendar['timezone']!='America/Chicago':
            raise ValueError('WRONG_EXCHANGE_CALENDAR_TIMEZONE')
        segments = tuple((timestamp(a),timestamp(b)) for a,b in calendar['segments'])
        window = SessionWindow(bar.session, calendar['timezone'], segments, calendar['source'],
                               calendar['evidence_sha256'], calendar.get('verified') is True)
        window.validate()
        if bar.opens_at != segments[0][0] or bar.closes_at != segments[-1][1]:
            raise ValueError('BAR_DOES_NOT_MATCH_DATED_EXCHANGE_SESSION')
        if (bar.next_session.isoformat() if bar.next_session else None) != calendar.get('next_session'):
            raise ValueError('NEXT_SESSION_NOT_FROM_VERIFIED_CALENDAR')
        if bar.available_at < bar.closes_at or bar.source_hash not in self.manifest.get('source_object_hashes',[]):
            raise ValueError('PUBLICATION_OR_SOURCE_OBJECT_UNKNOWN')
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
                        signal=self._bar(row['signal'])
                    except (ValueError,TypeError,KeyError) as error:
                        self._quarantine(market,row['signal'].get('contract_id'),str(error))
                        continue
                    if self.specs[signal.contract_id].root != MARKETS[market]['signal']:
                        raise ValueError('WRONG_STANDARD_SIGNAL_CONTRACT')
                    execution=[]
                    for b in row['execution']:
                        try:
                            execution.append(self._bar(b))
                        except (ValueError,TypeError,KeyError) as error:
                            self._quarantine(market,b.get('contract_id'),str(error))
                    execution=tuple(execution)
                    if any(self.specs[b.contract_id].root not in MARKETS[market]['execution'] for b in execution):
                        raise ValueError('WRONG_EXECUTION_ROOT')
                    roll=None
                    if row.get('roll'):
                        r=dict(row['roll']);r['known_at']=timestamp(r['known_at']);roll=RollInstruction(**r)
                        if roll.evidence_hash not in self.manifest.get('verified_roll_decision_hashes',[]):
                            raise ValueError('CAUSAL_ROLL_DECISION_EVIDENCE_REQUIRED')
                    days.append(MarketDay(market,signal,execution,roll,row.get('mapping_verified') is True,
                                          row.get('mapping_source','UNKNOWN')))
                if not days: continue
                session=days[0].signal.session
                if previous is not None and session<=previous:
                    raise ValueError('INPUT_SESSION_OUT_OF_ORDER')
                previous=session
                yield days

    def _quarantine(self, market, contract, reason):
        self.quarantine_count += 1
        if len(self.quarantine)<1000:
            self.quarantine.append(dict(market=market,contract_id=contract,reason=reason))
