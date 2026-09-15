"""Offline boundary tests; all artificial artifacts stay under tmp_path."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from src.futures_f0 import acquisition
from src.futures_f0.budget import BudgetGate
from src.futures_f0.data import Request
from src.futures_f0.runtime import canonical_hash, digest, utcnow, write_json


def fixture_round(tmp_path, monkeypatch, hours=4):
    root = tmp_path / 'futures-f0-test'
    out = root / 'continuations' / 'test'
    out.mkdir(parents=True)
    prior = root / 'FROZEN_PROTOCOL.json'
    prior.write_text('offline test fixture', encoding='utf-8')
    now = datetime.now(timezone.utc)
    state = dict(data_root=str(root), start_utc=now.isoformat(),
                 hard_deadline_utc=(now + timedelta(hours=hours)).isoformat(),
                 listed_history_budget_usd=10, cash_authorization_usd=0,
                 old_artifacts={prior.name: {'sha256': digest(prior)}})
    (out / 'ROUND_STATE.json').write_text(json.dumps(state), encoding='utf-8')
    monkeypatch.setattr(acquisition, 'load_config', lambda: SimpleNamespace(data_dir=root, api_key='offline-test'))
    monkeypatch.setattr(acquisition.Guard, 'check', lambda *a: None)
    return out, prior


def test_old_protocol_not_rewritten(tmp_path, monkeypatch):
    out, prior = fixture_round(tmp_path, monkeypatch)
    before = digest(prior)
    c = acquisition.Continuation(out)
    c.close()
    assert digest(prior) == before


def test_later_round_cannot_extend_past_four_hours(tmp_path, monkeypatch):
    out, _ = fixture_round(tmp_path, monkeypatch, hours=4.01)
    with pytest.raises(ValueError, match='MAX_FOUR_HOURS'):
        acquisition.Continuation(out)


def test_tampered_old_result_stops_before_request(tmp_path, monkeypatch):
    out, prior = fixture_round(tmp_path, monkeypatch)
    prior.write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='ORIGINAL_F0_ARTIFACT_CHANGED'):
        acquisition.Continuation(out)


@pytest.mark.parametrize('response', [
    {'partial': ['ESZ4'], 'not_found': [], 'result': {}},
    {'partial': [], 'not_found': [], 'result': {'ESZ4': [{'d0': '2024-10-02', 'd1': '2024-10-03', 's': '1'}]}},
    {'partial': [], 'not_found': [], 'result': {'ESZ4': [{'d0': '2024-10-01', 'd1': '2024-10-02', 's': '1'}]}},
])
def test_partial_or_gapped_mapping_not_downloadable(tmp_path, monkeypatch, response):
    out, _ = fixture_round(tmp_path, monkeypatch)
    c = acquisition.Continuation(out)
    monkeypatch.setattr(c, 'metadata', lambda *a: {'result': response})
    with pytest.raises(ValueError, match='MAPPING_'):
        c.verify_symbols(Request(('ESZ4',), 'definition', '2024-10-01T00:00:00Z', '2024-10-03T00:00:00Z'))
    c.close()


def test_actual_mapping_can_span_multiple_vendor_intervals(tmp_path, monkeypatch):
    out, _ = fixture_round(tmp_path, monkeypatch)
    c = acquisition.Continuation(out)
    response = {'partial': [], 'not_found': [], 'result': {'ESZ4': [
        {'d0': '2024-10-01', 'd1': '2024-10-02', 's': '123'},
        {'d0': '2024-10-02', 'd1': '2024-10-03', 's': '123'}]}}
    monkeypatch.setattr(c, 'metadata', lambda *a: {'result': response})
    assert c.verify_symbols(Request(('ESZ4',), 'definition', '2024-10-01T00:00:00Z', '2024-10-03T00:00:00Z'))['result'] == response
    c.close()


def completed_cache(tmp_path, monkeypatch, *, wrapper=True, finished=True):
    out, _ = fixture_round(tmp_path, monkeypatch)
    c = acquisition.Continuation(out)
    request = Request(('ESZ4',), 'definition', '2024-10-01T00:00:00Z', '2024-10-02T00:00:00Z')
    parameters = request.parameters(); identity = canonical_hash(parameters)
    mapping_parameters = c._symbol_parameters(request)
    result = {'partial': [], 'not_found': [], 'result': {'ESZ4': [
        {'d0': '2024-10-01', 'd1': '2024-10-02', 's': '123'}]}}
    mapping = dict(method='symbology.resolve', parameters=mapping_parameters,
                   result=result, result_sha256=canonical_hash(result), received_at=utcnow().isoformat())
    mapping_path = out / 'metadata' / (canonical_hash(dict(method='symbology.resolve', parameters=mapping_parameters)) + '.json')
    write_json(mapping_path, mapping)
    raw = c.config.data_dir / 'vendor_cache' / (identity + '.csv')
    raw.parent.mkdir(); raw.write_bytes(b'instrument_id,open\n123,100\n')
    budget = BudgetGate(c.config.data_dir, out / 'missing-credit-evidence-is-not-needed-for-cache.json',
                        deadline=c.state['hard_deadline_utc'])
    records = []
    with budget._locked():
        budget._append(records, dict(event='OPEN'))
        reservation = budget._append(records, dict(event='RESERVE', reservation_id='OFFLINE_RESERVATION',
            request=parameters, request_sha256=identity, reserved_cost_usd='0.001'))
    receipt = dict(request=parameters, request_sha256=identity, sha256=digest(raw),
                   completed_at=utcnow().isoformat(), quoted_cost_usd=0.001, reserved_list_price_usd='0.001',
                   credit_reservation_id='OFFLINE_RESERVATION', budget_reservation_sha256=reservation['sha256'],
                   cash_payment_usd=None, cash_authorization_usd=0, research_qualified=False)
    receipt_path = raw.with_suffix('.receipt.json'); write_json(receipt_path, receipt)
    if finished:
        with budget._locked():
            budget._append(records, dict(event='COMPLETED', reservation_id='OFFLINE_RESERVATION',
                request_sha256=identity, receipt_sha256=digest(receipt_path)))
    record = dict(request=parameters, receipt=receipt, mapping_evidence_sha256=canonical_hash(mapping),
                  mapping=result, quotation_is_not_actual_billing_settlement=True,
                  acquired_at=utcnow().isoformat(), research_qualified=False)
    acquisition_path = out / 'metadata' / ('acquisition_' + identity + '.json')
    if wrapper: write_json(acquisition_path, record, immutable=True)
    old_quote = out / 'metadata' / ('quote_' + identity + '.json')
    write_json(old_quote, dict(request=parameters, request_sha256=identity, get_cost=0.001,
                              estimated_at=(utcnow() - timedelta(hours=2)).isoformat()))
    def reject_network(*args, **kwargs):
        pytest.fail('Cached acquisition must not call network, quote, or downloader')
    monkeypatch.setattr(acquisition.Continuation, 'metadata', reject_network)
    monkeypatch.setattr(acquisition.MetadataClient, 'estimate', reject_network)
    monkeypatch.setattr(acquisition.MetadataClient, 'download_with_credit', reject_network)
    return c, budget, request, acquisition_path, receipt_path, raw, mapping_path, old_quote


def test_completed_cache_reuses_original_wrapper_quote_and_positive_ledger(tmp_path, monkeypatch):
    c, budget, request, path, receipt, raw, mapping, quote = completed_cache(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in (path, receipt, raw, mapping, quote, budget.journal)}
    original = json.loads(path.read_text())
    assert c.acquire(request, budget) == original
    assert c.acquire(request, budget) == original
    assert c.last_acquisition_action == 'CACHE_REUSED'
    assert all(p.read_bytes() == content for p, content in before.items())
    proof = json.loads((path.parent/'acquisition_proofs'/path.name).read_text())
    assert proof['acquisition_sha256'] == digest(path) and proof['receipt_sha256'] == digest(receipt)
    assert budget.snapshot()['cumulative_reserved_usd'] == '0.001'
    c.close()


def test_completed_download_recovers_missing_wrapper_once_without_quote(tmp_path, monkeypatch):
    c, budget, request, path, receipt, *_ = completed_cache(tmp_path, monkeypatch, wrapper=False)
    original_journal = budget.journal.read_bytes()
    first = c.acquire(request, budget)
    assert first['recovered_from_completed_cache'] is True
    assert first['acquired_at'] == json.loads(receipt.read_text())['completed_at']
    assert c.acquire(request, budget) == first
    assert budget.journal.read_bytes() == original_journal
    c.close()


@pytest.mark.parametrize('changed', ['wrapper', 'receipt', 'raw', 'mapping', 'journal'])
def test_reuse_rejects_changed_linked_evidence_without_rebilling(tmp_path, monkeypatch, changed):
    c, budget, request, path, receipt, raw, mapping, _ = completed_cache(tmp_path, monkeypatch)
    c.acquire(request, budget)  # Pins the original wrapper's independent digest.
    target = dict(wrapper=path, receipt=receipt, raw=raw, mapping=mapping, journal=budget.journal)[changed]
    if changed == 'raw': target.write_bytes(b'changed')
    elif changed == 'journal':
        with target.open('a') as stream: stream.write('interrupted')
    else:
        value = json.loads(target.read_text()); value['unexpected_change'] = 1
        target.write_text(json.dumps(value))
    with pytest.raises(ValueError): c.acquire(request, budget)
    c.close()


def test_cache_with_unknown_budget_completion_is_not_rebilled(tmp_path, monkeypatch):
    c, budget, request, *_ = completed_cache(tmp_path, monkeypatch, finished=False)
    before = budget.journal.read_bytes()
    with pytest.raises(ValueError, match='COMPLETION_UNCONFIRMED'): c.acquire(request, budget)
    assert budget.journal.read_bytes() == before
    c.close()


def test_repeated_plan_preserves_prior_progress_stop_and_reports_cache_reuse(tmp_path, monkeypatch, capsys):
    c, budget, request, path, *_ = completed_cache(tmp_path, monkeypatch)
    out = c.output
    progress = out / 'ACQUISITION_PROGRESS.json'; progress.write_text('{"original_progress":4}\n')
    stop = out / 'ACQUISITION_STOP.json'; stop.write_text('{"original_failure":"preserve"}\n')
    preserved = {p: p.read_bytes() for p in (progress, stop, budget.journal, path)}
    plan = out / 'plan.json'
    write_json(plan, [dict(symbols=list(request.symbols), schema=request.schema, start=request.start, end=request.end)])
    args = ['--round-dir', str(out), '--request-plan', str(plan), '--credit-evidence', str(out/'missing.json')]
    acquisition.main(args); acquisition.main(args)
    assert all(p.read_bytes() == content for p, content in preserved.items())
    attempts = list((out/'acquisition_attempts').iterdir())
    assert len(attempts) == 2
    for attempt in attempts:
        record = json.loads((attempt/'COMPLETE.json').read_text())
        assert record['reused_cached_requests'] == 1 and record['new_download_requests'] == 0
        assert record['acquisition_sha256'] == digest(path)
    latest = json.loads((out/'ACQUISITION_LATEST.json').read_text())
    assert latest['status'] == 'PLAN_COMPLETE_RAW_DATA_ONLY'
    assert digest(out/latest['path']) == latest['sha256']
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all(r['status'] == 'CACHE_REUSED' and r['new_list_price_reserved_usd'] == '0' and r['original_quoted_cost_usd'] == 0.001 for r in printed)
    c.close()


@pytest.mark.parametrize('failure', [ValueError('OFFLINE_FAILURE'), KeyboardInterrupt()])
def test_failed_plan_attempts_preserve_original_stop_and_append_versions(tmp_path, monkeypatch, failure):
    c, budget, request, *_ = completed_cache(tmp_path, monkeypatch)
    out = c.output
    original = out/'ACQUISITION_STOP.json'; original.write_text('{"original_stop":true}\n')
    original_bytes = original.read_bytes(); journal_bytes = budget.journal.read_bytes()
    plan = out/'plan.json'
    write_json(plan, [dict(symbols=list(request.symbols), schema=request.schema, start=request.start, end=request.end)])
    def fail(*args): raise failure
    monkeypatch.setattr(acquisition.Continuation, 'acquire', fail)
    args = ['--round-dir',str(out),'--request-plan',str(plan),'--credit-evidence',str(out/'missing.json')]
    for _ in range(2):
        with pytest.raises(SystemExit, match='ACQUISITION_STOPPED'): acquisition.main(args)
    assert original.read_bytes() == original_bytes and budget.journal.read_bytes() == journal_bytes
    failures = list((out/'acquisition_attempts').glob('*/STOP.json'))
    assert len(failures) == 2
    expected = 'OFFLINE_FAILURE' if isinstance(failure, ValueError) else 'REDACTED_REQUEST_OR_RUNTIME_ERROR'
    assert all(json.loads(p.read_text())['reason'] == expected for p in failures)
    latest = json.loads((out/'ACQUISITION_LATEST.json').read_text())
    assert latest['status'] == 'STOPPED_NO_AUTO_RETRY' and digest(out/latest['path']) == latest['sha256']
    c.close()
