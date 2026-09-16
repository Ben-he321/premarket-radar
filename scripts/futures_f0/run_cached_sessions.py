"""Offline, bounded reproduction of the cached concrete-contract session audit."""
from datetime import date
import csv
import argparse
import json
from pathlib import Path
import socket
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from src.futures_f0.runtime import Guard, digest, write_json
from src.futures_f0.vendor import RawSource, EvidenceFile
from src.futures_f0.session_inputs import DatedSession, integrate_session, settlement_versions_at
from src.futures_f0.provenance import verify_session_derivation, verify_bar_derivation

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', type=Path, required=True)
parser.add_argument('--round', dest='round_dir', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
ROOT, ROUND, OUTPUT = args.root.resolve(), args.round_dir.resolve(), args.output.resolve()
if not ROUND.is_relative_to(ROOT) or not OUTPUT.is_relative_to(ROUND / 'resumes'):
    raise ValueError('OUTPUT_MUST_BE_IN_AUTHORIZED_RESUME_WITHIN_ORIGINAL_DATA_ROOT')
state = json.loads((ROUND / 'ROUND_STATE.json').read_text(encoding='utf-8-sig'))
if Path(state['data_root']).resolve() != ROOT:
    raise ValueError('ORIGINAL_DATA_ROOT_MISMATCH')
if OUTPUT.exists():
    raise ValueError('PRESERVE_EXISTING_AUDIT_OUTPUT_USE_NEW_ATTEMPT_DIRECTORY')
OUTPUT.mkdir(parents=True)
PREVIOUS = Path(state['previous_round'])
GUARD = Guard(state['hard_deadline_utc'], OUTPUT)
policy_path = ROUND / 'evidence/causality/CAPTURE_PREFIX_POLICY.json'
causality = EvidenceFile(policy_path, digest(policy_path),
    'Official public documentation chain; explicit dataset applicability inference, not per-file certification')



def network_disabled(*args, **kwargs):
    raise RuntimeError('NETWORK_DISABLED_CACHED_SESSION_AUDIT')


socket.create_connection = network_disabled
socket.socket.connect = network_disabled
GUARD.check({'stage': 'cached_session_start'})
sources = []
before = {}
for path in sorted((ROOT / 'vendor_cache').glob('*.receipt.json')):
    raw = path.with_name(path.name.replace('.receipt.json', '.csv'))
    before[str(path)], before[str(raw)] = digest(path), digest(raw)
    sources.append(RawSource.from_receipt(raw, path, price_encoding='fixed_1e9'))
registry_path = REPO / 'docs/futures_f0/contract_registry.csv'
with registry_path.open(encoding='utf-8-sig', newline='') as stream:
    registry = {row['root']: row for row in csv.DictReader(stream)}
reference_path = PREVIOUS / 'normalization_sample/CME_TRADING_REFERENCE_DATE_SEMANTICS.json'
reference = EvidenceFile(reference_path, digest(reference_path), 'https://databento.com/docs/venues-and-datasets/glbx-mdp3')
review_path = PREVIOUS / 'engineering/CALENDAR_BOUNDARY_REVIEW.md'
review_ref = dict(path=str(review_path), sha256=digest(review_path), status='PREVIOUS_SOURCE_REVIEW_NOT_DATED_CALENDAR_CERTIFICATION')
rules = {'ES': 'https://www.cmegroup.com/rulebook/CME/IV/350/358/358.pdf',
         'MES': 'https://www.cmegroup.com/rulebook/CME/IV/350/353/353.pdf',
         'GC': 'https://www.cmegroup.com/rulebook/COMEX/1a/113.pdf',
         'MGC': 'https://www.cmegroup.com/rulebook/COMEX/1a/120.pdf',
         'HG': 'https://www.cmegroup.com/rulebook/COMEX/1a/111.pdf',
         'MHG': 'https://www.cmegroup.com/rulebook/COMEX/9/914.pdf',
         'CL': 'https://www.cmegroup.com/rulebook/NYMEX/2/200.pdf',
         'MCL': 'https://www.cmegroup.com/rulebook/NYMEX/3/309.pdf',
         'ZC': 'https://www.cmegroup.com/rulebook/CBOT/I/10.pdf',
         'MZC': 'https://www.cmegroup.com/rulebook/CBOT/I/10O.pdf',
         '6E': 'https://www.cmegroup.com/rulebook/CME/III/250/261/261.pdf',
         'M6E': 'https://www.cmegroup.com/rulebook/CME/III/250/292/292.pdf',
         '1OZ': 'https://www.cmegroup.com/content/dam/cmegroup/notices/clearing/2024/12/chadv24-385.pdf'}
notice_rule = 'https://www.cmegroup.com/rulebook/NYMEX/1/7.pdf'
pause_notice = 'https://www.cmegroup.com/notices/electronic-trading/2021/06/20210621.html'
write_json(OUTPUT / 'OFFICIAL_SOURCE_LOOKUP.json', dict(retrieved_at=None, method='REUSE_HASH_BOUND_PREVIOUS_SOURCE_REVIEW_NO_NEW_WEB_REQUEST',
    sources=[dict(url=rules['ES'], observed='Cash final settlement; expiry tied to primary exchange opening on the final settlement day; third Friday subject to holidays.',
                  limitation='Current rule PDF alone does not establish historical rule version or complete dated holidays.'),
             dict(url=rules['MES'], observed='Specific micro contract rule; 5 USD per index point and 0.25 point tick; termination tied to primary listing exchange opening.',
                  limitation='Historical expiry requires dated rule and holiday reconciliation.'),
             dict(url=notice_rule, observed='Official metal-delivery chapter retrieved; previous review identifies 706.C notice rule.',
                  limitation='A product rule is not an acquired specific-contract first-notice calendar row.'),
             dict(url=pause_notice, observed='Official dated June 2021 electronic-trading notice retrieved; previous review binds ES/MES pause removal effective 2021-06-28.',
                  limitation='Dated normal template is not full special-event reconciliation.')],
    prior_source_review=review_ref, research_qualified=False), immutable=True)

identities = {}
for source in sources:
    if source.validate()['request']['schema'] != 'definition':
        continue
    for record in source.records(guard=GUARD):
        symbol = record['raw_symbol']
        if symbol not in identities or record['ts_recv_ns'] < identities[symbol]['ts_recv_ns']:
            identities[symbol] = record

summaries = []
for symbol, definition in sorted(identities.items()):
    try:
        root = next(r for r in sorted(registry, key=len, reverse=True) if symbol.startswith(r))
        is_old = symbol in ('ESZ4', 'MESZ4')
        session = '2024-10-01' if is_old else '2025-03-04'
        if is_old:
            segments = [['2024-09-30T22:00:00Z', '2024-10-01T21:00:00Z']]
            next_session, next_open = '2024-10-02', '2024-10-01T22:00:00Z'
        elif root in ('ZC', 'MZC'):
            segments = [['2025-03-04T01:00:00Z', '2025-03-04T13:45:00Z'],
                        ['2025-03-04T14:30:00Z', '2025-03-04T19:20:00Z']]
            next_session, next_open = '2025-03-05', '2025-03-05T01:00:00Z'
        else:
            segments = [['2025-03-03T23:00:00Z', '2025-03-04T22:00:00Z']]
            next_session, next_open = '2025-03-05', '2025-03-04T23:00:00Z'
        calendar_path = OUTPUT / 'dated_evidence' / (symbol + '_calendar.json')
        write_json(calendar_path, dict(kind='DATED_SESSION_INPUT_CANDIDATE', mock=False,
            scope=dict(contract_id=symbol, session=session), timezone='America/Chicago', segments=segments,
            next_session=next_session, next_open=next_open,
            source_urls=[pause_notice, rules[root]] if root in ('ES', 'MES') else [registry[root]['spec_source_url'], rules[root]],
            source_status='DATED_NORMAL_TEMPLATE_CANDIDATE_SPECIAL_EVENT_RECONCILIATION_PENDING',
            source_review=review_ref, interpretation='Explicit dated candidate; no weekday-generated research qualification.'), immutable=True)
        boundary_path = OUTPUT / 'dated_evidence' / (symbol + '_boundaries.json')
        cash = registry[root]['delivery_type'] in ('CASH', 'CASH_SOQ')
        first_notice = dict(status='NOT_APPLICABLE_CASH_SETTLED' if cash else 'UNKNOWN_APPLICABLE_DELIVERY_BOUNDARY',
            date=None, source_urls=[rules[root]] if cash else ([notice_rule] if root in ('GC', 'MGC', 'HG') else [rules[root]]))
        write_json(boundary_path, dict(kind='SPECIFIC_CONTRACT_BOUNDARIES_CANDIDATE', mock=False,
            scope=dict(contract_id=symbol), first_notice=first_notice,
            last_trade=dict(at=definition['expiration'], status='VENDOR_EXPIRATION_WITH_RULE_REFERENCE_NOT_DATED_EXCHANGE_CALENDAR_ROW',
                source_urls=[rules[root]], vendor_record_sha256=definition['raw_record_sha256'], vendor_source_sha256=definition['source_sha256']),
            preceding_exchange_sessions=['2024-12-13', '2024-12-16', '2024-12-17', '2024-12-18', '2024-12-19'] if is_old else [],
            session_list_source_urls=[rules[root], 'https://ir.theice.com/press/news-details/2023/NYSE-Group-Announces-2024-2025-and-2026-Holiday-and-Early-Closings-Calendar/default.aspx'] if is_old else [],
            session_list_status='PREVIOUS_RULE_DERIVED_EXAMPLE_NOT_COMPLETE_EXCHANGE_CALENDAR' if is_old else 'UNKNOWN',
            source_review=review_ref), immutable=True)
        calendar = EvidenceFile(calendar_path, digest(calendar_path), 'Official URLs and hash-bound previous review; candidate interpretation')
        boundary = EvidenceFile(boundary_path, digest(boundary_path), 'Concrete vendor definition plus official rule references; candidate interpretation')
        result = integrate_session(sources, window=DatedSession(symbol, date.fromisoformat(session), calendar),
            decision_at=next_open, registry_row=registry[root], boundary_evidence=boundary,
            reference_evidence=reference, causality_evidence=causality, guard=GUARD)
        verified = verify_session_derivation(result, source_root=ROOT,
            raw_hashes=[source.validate()['sha256'] for source in sources],
            registry_row=registry[root], guard=GUARD)
        if result['engine_record_candidate'] is not None:
            verify_bar_derivation(result['engine_record_candidate'], verified)
        result_path = OUTPUT / 'sessions' / (symbol + '_' + session + '.json')
        write_json(result_path, result, immutable=True)
        cutoffs = [segments[-1][1], next_open]
        if is_old:
            cutoffs.append('2024-10-01T23:59:59.999999999Z')
        else:
            cutoffs.append('2025-03-04T23:59:59.999999999Z')
        prefix = [settlement_versions_at(sources, contract_id=symbol, instrument_id=definition['instrument_id'],
            publisher_id=definition['publisher_id'], session=session, decision_at=cutoff,
            reference_evidence=reference, guard=GUARD) for cutoff in sorted(set(cutoffs))]
        write_json(OUTPUT / 'settlements' / (symbol + '_decision_prefixes.json'),
                   dict(contract_id=symbol, snapshots=prefix, research_qualified=False), immutable=True)
        summaries.append(dict(contract_id=symbol, market=result['market'], session=session,
            input_path=str(result_path), input_sha256=digest(result_path),
            derivation_sha256=result['derivation_sha256'], raw_to_derivation_recomputation='PASS',
            temporal_policy_sha256=causality.sha256, qualification_issues=result['qualification_issues'],
            ohlcv=result['prices']['session_ohlcv'], next_open=result['next_open'], unit_comparison=result['definition']['units']['unit_comparison'],
            price_coverage=result['prices']['price_coverage'], selected_buckets=result['prices']['selected_bucket_count'],
            missing_hours=len(result['prices']['missing_hour_buckets']), missing_boundary_minutes=len(result['prices']['missing_boundary_minutes']),
            settlement_at_next_open=result['settlement']['settlement'], settlement_status=result['settlement']['status'],
            settlement_versions_at_next_open=len(result['settlement']['versions_visible_at_cutoff']),
            status_events=len(result['execution_status']['events_captured_during_session']),
            first_notice_status=result['boundaries']['first_notice']['status'],
            safe_exit_candidate=result['boundaries']['five_session_safe_exit_candidate'], research_qualified=False))
        print(symbol, summaries[-1]['price_coverage'], summaries[-1]['settlement_status'], flush=True)
    except ValueError as error:
        failure=dict(contract_id=symbol,status='QUARANTINED_CACHED_INPUT',reason=str(error),research_qualified=False)
        write_json(OUTPUT / 'quarantine' / (symbol + '.json'), failure, immutable=True)
        summaries.append(failure)
        print(symbol, failure['status'], failure['reason'], flush=True)
    write_json(OUTPUT / 'PROGRESS.json', dict(completed_contracts=len(summaries),
        total_contracts=len(identities),last_contract=symbol,completed_accounts=0,
        resource=GUARD.check({'stage':'contract_checked','contract_id':symbol}),
        peak_rss_bytes=GUARD.peak_rss,minimum_free_ram_bytes=GUARD.min_available))

after = {path: digest(path) for path in before}
assert before == after
reading = GUARD.check({'stage': 'cached_session_complete'})
write_json(OUTPUT / 'SESSION_INPUT_SUMMARY.json', dict(status='REAL_CACHE_INTEGRATED_CANDIDATES_ONLY',
    network_disabled=True, requests_sent=0, source_objects=len(sources), source_and_receipt_hashes_unchanged=True,
    source_hashes=before, registry_path=str(registry_path), registry_sha256=digest(registry_path),
    implementation_sha256=digest(REPO / 'src/futures_f0/session_inputs.py'),
    sessions=summaries, reconstructed_complete_sessions=sum(s.get('price_coverage')=='COMPLETE_OBSERVED_BUCKETS' for s in summaries),
    unit_matches=sum(s.get('unit_comparison')=='MATCH' for s in summaries), qualified_sessions=0,
    real_futures_backtest_run=False, completed_accounts=0, resource_final=reading,
    temporal_policy_path=str(policy_path), temporal_policy_sha256=causality.sha256,
    causal_policy_contract='SAME_CAPTURE_PREFIX_POLICY_AS_QUALIFIED_INPUTS',
    source_recomputation_sessions=sum(s.get('raw_to_derivation_recomputation')=='PASS' for s in summaries),
    minimal_account_end_to_end='NOT_RUN_UNQUALIFIED_CONTRACT_CALENDAR_EXECUTION_SETTLEMENT_AND_WARMUP',
    peak_rss_bytes=GUARD.peak_rss, minimum_free_ram_bytes=GUARD.min_available), immutable=True)
print('COMPLETED', len(summaries), 'sessions', flush=True)
