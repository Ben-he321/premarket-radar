"""One bounded continuation's authenticated metadata and source acquisition.

No trading, account creation or automatic expansion of the requested universe.
The credit gate remains mandatory even when the desired sample is very small.
"""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import time
import uuid

from filelock import FileLock
import requests

from .config import load_config
from .data import MetadataClient, Request, timestamp
from .runtime import Guard, canonical_hash, digest, utcnow, write_json


class Continuation:
    def __init__(self, round_dir):
        self.config = load_config()
        self.output = Path(round_dir).resolve()
        self.state = json.loads((self.output / 'ROUND_STATE.json').read_text(encoding='utf-8-sig'))
        if self.config.data_dir != Path(self.state['data_root']).resolve():
            raise ValueError('CONTINUATION_DATA_ROOT_CHANGED')
        if not self.output.is_relative_to(self.config.data_dir / 'continuations'):
            raise ValueError('CONTINUATION_MUST_USE_SEPARATE_ROUND_DIRECTORY')
        start, end = map(timestamp, (self.state['start_utc'], self.state['hard_deadline_utc']))
        if end <= start or end - start > timedelta(hours=4):
            raise ValueError('CONTINUATION_MAX_FOUR_HOURS')
        if self.state.get('listed_history_budget_usd') != 10 or self.state.get('cash_authorization_usd') != 0:
            raise ValueError('CONTINUATION_AUTHORIZATION_MISMATCH')
        for name, info in self.state['old_artifacts'].items():
            if digest(self.config.data_dir / name) != info['sha256']:
                raise ValueError('ORIGINAL_F0_ARTIFACT_CHANGED')
        self.guard = Guard(self.state['hard_deadline_utc'], self.output)
        self.guard.check({'stage': 'continuation_initialization'})
        self.client = MetadataClient(self.config, guard=self.guard)
        self.session = requests.Session()

    def close(self):
        self.session.close()
        self.client.session.close()

    def _inherit_metadata(self, name):
        """Copy a hash-pinned prior round record, never redate or refetch it."""
        target = self.output / 'metadata' / name
        if target.exists():
            return target
        previous = self.state.get('previous_round')
        if not previous:
            return target
        prior = Path(previous).resolve()
        if prior == self.output or not prior.is_relative_to(self.config.data_dir / 'continuations'):
            raise ValueError('PRIOR_ROUND_PATH_INVALID')
        preservation = self.output / 'preservation' / 'PRIOR_ROUND_HASHES.json'
        if not preservation.is_file():
            raise ValueError('PRIOR_ROUND_HASH_MANIFEST_REQUIRED')
        inventory = json.loads(preservation.read_text(encoding='utf-8-sig'))
        key = str(Path('metadata') / name)
        pin = inventory.get(key) or inventory.get(key.replace('\\', '/'))
        source = prior / 'metadata' / name
        if pin is None or not source.exists():
            return target
        if source.stat().st_size != pin['bytes'] or digest(source) != pin['sha256']:
            raise ValueError('PRIOR_METADATA_CHANGED_NO_AUTO_REFETCH')
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(source.read_bytes())
        write_json(self.output / 'metadata_reuse' / name,
                   dict(source=str(source), source_sha256=pin['sha256'],
                        target_sha256=digest(target), copied_at=utcnow().isoformat(),
                        original_received_at_preserved=True, network_requests=0), immutable=True)
        return target

    def metadata(self, method, parameters):
        allowed = {'metadata.get_dataset_range', 'metadata.list_schemas',
                   'metadata.list_fields', 'metadata.get_dataset_condition', 'symbology.resolve'}
        if method not in allowed or parameters.get('dataset') != 'GLBX.MDP3':
            raise ValueError('FREE_METADATA_SCOPE_REJECTED')
        identity = canonical_hash({'method': method, 'parameters': parameters})
        path = self.output / 'metadata' / (identity + '.json')
        if method != 'metadata.get_dataset_range':
            self._inherit_metadata(path.name)
        # Historical schema/identity responses are reusable; account range is
        # read freshly for each separate process when used for permissions.
        if path.exists() and method != 'metadata.get_dataset_range':
            old = json.loads(path.read_text(encoding='utf-8'))
            if old.get('method') != method or old.get('parameters') != parameters:
                raise ValueError('METADATA_CACHE_REQUEST_MISMATCH')
            if old.get('result_sha256') != canonical_hash(old.get('result')):
                raise ValueError('METADATA_CACHE_HASH_MISMATCH')
            return old
        if not self.config.api_key:
            raise ValueError('DATABENTO_API_KEY_MISSING')
        self.guard.check({'stage': 'free_metadata', 'method': method})
        try:
            with self.session.get('https://hist.databento.com/v0/' + method,
                                  params=parameters, auth=(self.config.api_key, ''),
                                  timeout=(10, 30)) as response:
                if response.status_code != 200:
                    raise ValueError('FREE_METADATA_HTTP_' + str(response.status_code))
                result = response.json()
                record = dict(method=method, parameters=parameters, result=result,
                              result_sha256=canonical_hash(result), received_at=utcnow().isoformat(),
                              source_url='https://hist.databento.com/v0/' + method,
                              http_status=200, billable_data_request=False)
        except requests.RequestException:
            raise ValueError('FREE_METADATA_NETWORK_ERROR_REDACTED') from None
        write_json(path, record)
        time.sleep(0.1)
        return record

    @staticmethod
    def _symbol_parameters(request):
        p = request.parameters()
        first, last = timestamp(p['start']), timestamp(p['end'])
        # Symbology uses whole UTC dates; include any partial final date.
        last_date = last.date() if last.hour == last.minute == last.second == 0 else (last + timedelta(days=1)).date()
        return dict(dataset=p['dataset'], symbols=p['symbols'], stype_in='raw_symbol',
                    stype_out='instrument_id', start_date=str(first.date()), end_date=str(last_date))

    @staticmethod
    def _validate_symbols(request, record):
        p = Continuation._symbol_parameters(request)
        first = timestamp(p['start_date'] + 'T00:00:00Z').date()
        last_date = timestamp(p['end_date'] + 'T00:00:00Z').date()
        result = record['result']
        if result.get('not_found') or result.get('partial') or set(result.get('result', {})) != set(request.symbols):
            raise ValueError('EXACT_CONTRACT_MAPPING_INCOMPLETE')
        for symbol in request.symbols:
            intervals = result['result'][symbol]
            covered = first
            for row in sorted(intervals, key=lambda x: x['d0']):
                if timestamp(row['d0'] + 'T00:00:00Z').date() > covered or not str(row['s']).isdigit():
                    raise ValueError('CONTRACT_MAPPING_INTERVAL_GAP')
                covered = max(covered, timestamp(row['d1'] + 'T00:00:00Z').date())
            if covered < last_date:
                raise ValueError('CONTRACT_MAPPING_INTERVAL_INCOMPLETE')
        return record

    def verify_symbols(self, request):
        record = self.metadata('symbology.resolve', self._symbol_parameters(request))
        return self._validate_symbols(request, record)

    def _reuse_acquisition(self, request, budget, acquisition_path):
        """Reuse only preserved mapping, receipt, raw data and budget evidence.

        No network method is called here, including quote refresh. A completed
        receipt without its wrapper can be recovered from these same records.
        An existing wrapper is returned verbatim, including its acquisition time.
        """
        parameters = request.parameters()
        identity = canonical_hash(parameters)
        raw_path = self.config.data_dir / 'vendor_cache' / (identity + '.csv')
        receipt_path = raw_path.with_suffix('.receipt.json')
        if not (raw_path.exists() and receipt_path.exists()):
            raise ValueError('ACQUISITION_CACHE_INCOMPLETE_NO_AUTO_REBILL')
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        if receipt.get('request') != parameters or receipt.get('request_sha256') != identity or receipt.get('sha256') != digest(raw_path):
            raise ValueError('ACQUISITION_CACHED_RECEIPT_OR_RAW_HASH_MISMATCH')
        with budget._locked():
            ledger = budget._read()
        reserved = [r for r in ledger if r.get('event') == 'RESERVE' and r.get('reservation_id') == receipt.get('credit_reservation_id')]
        completed = [r for r in ledger if r.get('event') == 'COMPLETED' and r.get('reservation_id') == receipt.get('credit_reservation_id')]
        if len(reserved) != 1 or len(completed) != 1:
            raise ValueError('ACQUISITION_CACHE_BUDGET_COMPLETION_UNCONFIRMED')
        if (reserved[0].get('sha256') != receipt.get('budget_reservation_sha256')
                or reserved[0].get('request_sha256') != identity
                or reserved[0].get('request') != parameters
                or completed[0].get('receipt_sha256') != digest(receipt_path)
                or completed[0].get('request_sha256') != identity):
            raise ValueError('ACQUISITION_CACHED_BUDGET_RECEIPT_HASH_MISMATCH')
        mapping_parameters = self._symbol_parameters(request)
        mapping_id = canonical_hash(dict(method='symbology.resolve', parameters=mapping_parameters))
        mapping_path = self.output / 'metadata' / (mapping_id + '.json')
        self._inherit_metadata(mapping_path.name)
        if not mapping_path.exists():
            raise ValueError('ACQUISITION_CACHED_MAPPING_EVIDENCE_MISSING')
        mapping = json.loads(mapping_path.read_text(encoding='utf-8'))
        if (mapping.get('method') != 'symbology.resolve' or mapping.get('parameters') != mapping_parameters
                or mapping.get('result_sha256') != canonical_hash(mapping.get('result'))):
            raise ValueError('ACQUISITION_CACHED_MAPPING_HASH_MISMATCH')
        self._validate_symbols(request, mapping)
        if acquisition_path.exists():
            record = json.loads(acquisition_path.read_text(encoding='utf-8'))
            if (record.get('request') != parameters or record.get('receipt') != receipt
                    or record.get('mapping_evidence_sha256') != canonical_hash(mapping)
                    or record.get('mapping') != mapping['result']
                    or record.get('research_qualified') is not False
                    or timestamp(record['acquired_at']) < timestamp(receipt['completed_at'])):
                raise ValueError('ACQUISITION_WRAPPER_EVIDENCE_MISMATCH')
        else:
            record = dict(request=parameters, receipt=receipt,
                mapping_evidence_sha256=canonical_hash(mapping), mapping=mapping['result'],
                quotation_is_not_actual_billing_settlement=True,
                acquired_at=receipt['completed_at'], research_qualified=False,
                recovered_from_completed_cache=True, wrapper_recorded_at=utcnow().isoformat())
            write_json(acquisition_path, record, immutable=True)
        self._pin_acquisition(acquisition_path, receipt_path, raw_path, mapping_path)
        self.last_acquisition_action = 'CACHE_REUSED'
        return record

    @staticmethod
    def _pin_acquisition(acquisition_path, receipt_path, raw_path, mapping_path):
        # Legacy wrappers have no independent digest. After checking every
        # linked record, preserve a separate anchor without editing the wrapper.
        proof = dict(acquisition_sha256=digest(acquisition_path),
                     receipt_sha256=digest(receipt_path), raw_sha256=digest(raw_path),
                     mapping_file_sha256=digest(mapping_path))
        write_json(acquisition_path.parent / 'acquisition_proofs' / acquisition_path.name, proof, immutable=True)

    def acquire(self, request, budget):
        parameters = request.parameters()
        identity = canonical_hash(parameters)
        acquisition_path = self.output / 'metadata' / ('acquisition_' + identity + '.json')
        cache = self.config.data_dir / 'vendor_cache' / (identity + '.csv')
        if acquisition_path.exists() or cache.exists() or cache.with_suffix('.receipt.json').exists() or cache.with_suffix('.partial').exists():
            return self._reuse_acquisition(request, budget, acquisition_path)
        mapping = self.verify_symbols(request)
        # The downloader refreshes and archives its own authenticated quote.
        # An outer duplicate quote would overwrite old evidence on a rerun.
        receipt = self.client.download_with_credit(request, budget, exact_contracts_verified=True)
        # The boolean above is derived only after the authenticated exact
        # interval mapping check, never read from a user-edited manifest flag.
        record = dict(request=parameters, receipt=receipt,
                      mapping_evidence_sha256=canonical_hash(mapping),
                      mapping= mapping['result'],
                      quotation_is_not_actual_billing_settlement=True,
                      acquired_at=utcnow().isoformat(), research_qualified=False)
        write_json(acquisition_path, record, immutable=True)
        mapping_id = canonical_hash(dict(method='symbology.resolve', parameters=self._symbol_parameters(request)))
        self._pin_acquisition(acquisition_path, cache.with_suffix('.receipt.json'), cache,
                              self.output / 'metadata' / (mapping_id + '.json'))
        self.last_acquisition_action = 'RAW_DATA_ACQUIRED'
        return record


def _audit_record(output, attempt, name, record, *, status, legacy=None):
    """Append an immutable attempt artifact and update a separate latest pointer."""
    path = attempt / name
    write_json(path, record, immutable=True)
    with FileLock(str(output / 'acquisition_audit.lock'), timeout=5):
        if legacy and not (output / legacy).exists():
            write_json(output / legacy, record, immutable=True)
        write_json(output / 'ACQUISITION_LATEST.json', dict(at=utcnow().isoformat(),
            attempt_id=attempt.name, status=status, path=str(path.relative_to(output)), sha256=digest(path)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--round-dir', required=True)
    parser.add_argument('--request-plan', required=True)
    parser.add_argument('--credit-evidence', required=True)
    parser.add_argument('--previous-credit-evidence', help='Explicit append-only authorization renewal; never resets the original budget')
    args = parser.parse_args(argv)
    from .budget import BudgetGate
    continuation = Continuation(args.round_dir)
    output = continuation.output
    plan_path = Path(args.request_plan).resolve()
    if not plan_path.is_relative_to(output) or plan_path.stat().st_size > 1024 * 1024:
        raise ValueError('BOUNDED_ROUND_LOCAL_PLAN_REQUIRED')
    plan = json.loads(plan_path.read_text(encoding='utf-8-sig'))
    if not isinstance(plan, list) or not 1 <= len(plan) <= 1000:
        raise ValueError('BOUNDED_REQUEST_PLAN_REQUIRED')
    for row in plan:
        Request(tuple(row['symbols']), row['schema'], row['start'], row['end']).parameters()
    authorization = output / 'AUTHORIZATION.json'
    budget = BudgetGate(continuation.config.data_dir, args.credit_evidence,
                        deadline=continuation.state['hard_deadline_utc'],
                        **({'round_state': output / 'ROUND_STATE.json', 'authorization': authorization}
                           if authorization.exists() else {}))
    if args.previous_credit_evidence:
        first = plan[0]
        budget.authorize_continuation(Request(tuple(first['symbols']), first['schema'], first['start'], first['end']).parameters(),
                                      api_key=continuation.config.api_key,
                                      previous_evidence_manifest=args.previous_credit_evidence)
    plan_hash = digest(plan_path)
    attempt = output / 'acquisition_attempts' / (utcnow().strftime('%Y%m%dT%H%M%S%fZ') + '_' + uuid.uuid4().hex[:8])
    write_json(attempt / 'request_plan.json', plan, immutable=True)
    write_json(attempt / 'START.json', dict(at=utcnow().isoformat(), request_plan_sha256=plan_hash,
               total_requests=len(plan), attempt_id=attempt.name), immutable=True)
    reused, new_downloads, completed = 0, 0, 0
    try:
        with FileLock(str(continuation.config.data_dir / 'f0.lock'), timeout=0):
            for index, row in enumerate(plan):
                continuation.guard.check({'stage': 'request_plan', 'index': index})
                if digest(plan_path) != plan_hash:
                    raise ValueError('ACQUISITION_REQUEST_PLAN_CHANGED_DURING_ATTEMPT')
                request = Request(tuple(row['symbols']), row['schema'], row['start'], row['end'])
                result = continuation.acquire(request, budget)
                receipt = result['receipt']
                action = continuation.last_acquisition_action
                reused += action == 'CACHE_REUSED'
                new_downloads += action == 'RAW_DATA_ACQUIRED'
                completed = index + 1
                acquisition_path = output / 'metadata' / ('acquisition_' + canonical_hash(request.parameters()) + '.json')
                progress = dict(at=utcnow().isoformat(), request_plan_sha256=plan_hash,
                                attempt_id=attempt.name, reused_cached_requests=reused,
                                new_download_requests=new_downloads, latest_action=action,
                                completed_requests=index + 1, total_requests=len(plan),
                                latest_request=request.parameters(), latest_receipt=receipt,
                                acquisition_sha256=digest(acquisition_path),
                                status='DATA_ACQUIRED_NOT_RESEARCH_QUALIFIED')
                _audit_record(output, attempt, f'progress_{index + 1:06d}.json', progress, status=action)
                print(json.dumps({'completed_requests': index + 1, 'total_requests': len(plan),
                                  'schema': request.schema, 'symbols': request.symbols,
                                  'original_quoted_cost_usd': receipt.get('quoted_cost_usd'),
                                  'new_list_price_reserved_usd': '0' if action == 'CACHE_REUSED' else receipt.get('reserved_list_price_usd'),
                                  'status': action}, ensure_ascii=True), flush=True)
            _audit_record(output, attempt, 'COMPLETE.json', progress | {'status': 'PLAN_COMPLETE_RAW_DATA_ONLY'},
                          status='PLAN_COMPLETE_RAW_DATA_ONLY', legacy='ACQUISITION_PROGRESS.json')
    except BaseException as exc:
        _audit_record(output, attempt, 'STOP.json', dict(at=utcnow().isoformat(),
                   attempt_id=attempt.name, completed_requests=completed,
                   reused_cached_requests=reused, new_download_requests=new_downloads,
                   error_type=type(exc).__name__, reason=str(exc) if isinstance(exc, ValueError) else 'REDACTED_REQUEST_OR_RUNTIME_ERROR',
                   status='STOPPED_NO_AUTO_RETRY', plan_sha256=plan_hash),
                   status='STOPPED_NO_AUTO_RETRY', legacy='ACQUISITION_STOP.json')
        raise SystemExit('ACQUISITION_STOPPED_SEE_REDACTED_LOCAL_RECORD') from None
    finally:
        continuation.close()


if __name__ == '__main__':
    main()
