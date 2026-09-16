"""Offline-only credit-budget tests; every synthetic source stays in tmp_path."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
import threading

from filelock import FileLock
import pytest
import requests

from src.futures_f0.budget import BudgetGate, EvidenceArchive, FORMAT
from src.futures_f0.config import Config
from src.futures_f0.data import MetadataClient, Request
from src.futures_f0.runtime import canonical_hash, digest, utcnow


TEST_KEY = 'db-abcdefgh0123456789TEST_ONLY'


def plan(symbol='MESH2'):
    return Request((symbol,), 'ohlcv-1h', '2022-01-01T00:00:00Z', '2022-01-02T00:00:00Z')


def archive(tmp_path, *, balance='125,00', usage='0,00', limit='10,00', dom_key=False):
    root = tmp_path / 'evidence'; root.mkdir()
    now = utcnow()
    texts = {
        'billing': f'Credit balance: ${balance}\nCredit expiry: 2099-01-01\nScope: Historical\nUsage-based access: Enabled\nMonthly limit: ${limit}\n',
        'usage': f'Current billing cycle\nTotal usage ($): {usage}\n',
        'keys': 'User ID: account-test-123\nKey: db-abcdefgh...ONLY\n',
        'policy': 'Credits are automatically applied before any charges are made to the user.\n',
    }
    if dom_key:
        texts['keys'] = ('User ID: account-test-123\nKey: [REDACTED]\nDOM key SHA256: '
                        + hashlib.sha256(TEST_KEY.encode()).hexdigest()
                        + '\nOriginal DOM SHA256 (not retained): ' + '9' * 64 + '\n')
    manifest = dict(format=FORMAT, browser_session_id='OFFLINE_TEST_SESSION',
                    account_source='keys', account_pattern=r'User ID: (?P<value>[^\n]+)', sources={}, fields={})
    for name, text in texts.items():
        path = root / (name + '.txt'); path.write_text(text)
        manifest['sources'][name] = dict(path=path.name, sha256=digest(path),
            source_url='https://databento.com/' + ('docs/faqs/usage-pricing-and-data-credits' if name == 'policy' else 'portal/' + ('data-usage' if name == 'usage' else name)),
            captured_at=now.isoformat(), scope='public' if name == 'policy' else 'account',
            browser_session_id='OFFLINE_TEST_SESSION', decimal_separator=',')
    patterns = {
        'api_key_masked': ('keys', r'Key: (?P<value>[^\n]+)'),
        'credit_balance_usd': ('billing', r'Credit balance: (?P<value>[^\n]+)'),
        'credit_expires_at': ('billing', r'Credit expiry: (?P<value>[^\n]+)'),
        'credit_scope': ('billing', r'Scope: (?P<value>[^\n]+)'),
        'usage_based_access': ('billing', r'Usage-based access: (?P<value>[^\n]+)'),
        'monthly_limit_usd': ('billing', r'Monthly limit: (?P<value>[^\n]+)'),
        'monthly_usage_usd': ('usage', r'Total usage \(\$\): (?P<value>[^\n]+)'),
        'billing_month': ('usage', r'(?P<value>Current billing cycle)'),
        'credit_application_terms': ('policy', r'(?P<value>Credits are[^\n]+)'),
    }
    if dom_key:
        del patterns['api_key_masked']
        patterns['api_key_dom_sha256'] = ('keys', r'DOM key SHA256: (?P<value>[0-9a-f]{64})')
    manifest['fields'] = {name: dict(source=source, pattern=pattern) for name, (source, pattern) in patterns.items()}
    provenance = dict(browser_session_id='OFFLINE_TEST_SESSION', account_source='keys.txt',
        captured_at=now.isoformat(), sources=[dict(file=s['path'], url=s['source_url'])
        for s in manifest['sources'].values() if s['scope'] == 'account'])
    capture = root / 'capture.json'; capture.write_text(json.dumps(provenance))
    manifest['capture_provenance'] = dict(path=capture.name, sha256=digest(capture))
    path = root / 'manifest.json'; path.write_text(json.dumps(manifest))
    return path


def edit_manifest(path, edit):
    value = json.loads(path.read_text()); edit(value); path.write_text(json.dumps(value))


def replace_source(path, source, before, after):
    manifest = json.loads(path.read_text())
    raw = path.parent / manifest['sources'][source]['path']
    raw.write_text(raw.read_text().replace(before, after))
    manifest['sources'][source]['sha256'] = digest(raw)
    path.write_text(json.dumps(manifest))


class Response:
    status_code = 200
    headers = {}
    def __init__(self, value=None, payload=b'instrument_id,open\n1234,100\n'):
        self.value, self.payload, self.closed = value, payload, False
    def json(self): return self.value
    def close(self): self.closed = True
    def iter_content(self, chunk_size): yield self.payload


class Session:
    def __init__(self, cost=1, fail=None, on_download=None):
        self.cost, self.fail, self.on_download = cost, fail, on_download
        self.calls = []
    def post(self, url, **kwargs):
        self.calls.append(url)
        if 'metadata.' in url:
            return Response(self.cost if url.endswith('get_cost') else 3)
        if self.on_download: self.on_download()
        if self.fail: raise self.fail
        return Response()


def setup(tmp_path, *, cost=1, fail=None, dom_key=False, **evidence):
    path = archive(tmp_path, dom_key=dom_key, **evidence)
    gate = BudgetGate(tmp_path, path, deadline=(utcnow() + timedelta(hours=4)).isoformat())
    session = Session(cost, fail)
    client = MetadataClient(Config(tmp_path, TEST_KEY), session=session)
    return client, gate, path


def download(client, gate, request=None):
    return client.download_with_credit(request or plan(), gate, exact_contracts_verified=True)


def billable_calls(client):
    return [c for c in client.session.calls if 'timeseries.' in c]


def test_positive_fresh_list_price_reserved_durable_and_cache_first(tmp_path):
    client, gate, path = setup(tmp_path, cost=0.000568114221)
    receipt = download(client, gate)
    assert receipt['quoted_cost_usd'] == 0.000568114221
    assert receipt['cash_payment_usd'] is None and receipt['credit_debit_usd'] is None
    assert receipt['cash_authorization_usd'] == 0 and receipt['research_qualified'] is False
    assert gate.snapshot()['cumulative_reserved_usd'] == '0.000568114221'
    records = [json.loads(s) for s in gate.journal.read_text().splitlines()]
    assert [r['event'] for r in records] == ['OPEN', 'RESERVE', 'COMPLETED']
    quote = tmp_path / records[1]['quote_path']
    assert digest(quote) == records[1]['quote_sha256']
    assert json.loads(quote.read_text())['get_cost'] == receipt['quoted_cost_usd']
    assert TEST_KEY not in gate.journal.read_text() and 'db-abcdefgh' not in gate.journal.read_text()
    path.unlink()  # Cache verification precedes evidence or free network calls.
    assert download(client, gate) == receipt and len(client.session.calls) == 4


def test_old_signature_delegates_but_ignores_caller_fake_zero_quote(tmp_path):
    client, gate, _ = setup(tmp_path, cost=2)
    receipt = client.download_zero_quote(plan(), {'get_cost': 0, 'verified': True}, budget=gate, exact_contracts_verified=True)
    assert receipt['quoted_cost_usd'] == 2 and len(billable_calls(client)) == 1


@pytest.mark.parametrize('cost', [-1, float('nan'), float('inf'), True, False, '0.1'])
def test_invalid_fresh_vendor_amount_never_calls_billable_endpoint(tmp_path, cost):
    client, gate, _ = setup(tmp_path, cost=cost)
    with pytest.raises(ValueError): download(client, gate)
    assert not billable_calls(client)


@pytest.mark.parametrize('amounts', [dict(cost=10.00000001), dict(cost=3, usage='8,00'),
    dict(cost=2, balance='1,00'), dict(cost=2, limit='1,00')])
def test_round_month_and_actual_credit_caps(tmp_path, amounts):
    client, gate, _ = setup(tmp_path, **amounts)
    with pytest.raises(ValueError, match='LIMIT_EXCEEDED'): download(client, gate)
    assert not billable_calls(client) and gate.snapshot()['requests'] == 0


def test_cumulative_round_limit_and_tiny_decimal_amounts(tmp_path):
    client, gate, _ = setup(tmp_path, cost=1e-12)
    download(client, gate)
    assert gate.snapshot()['cumulative_reserved_usd'] == '0.000000000001'
    client.session.cost = 10
    with pytest.raises(ValueError, match='LIMIT_EXCEEDED'): download(client, gate, plan('ESH2'))
    assert len(billable_calls(client)) == 1


@pytest.mark.parametrize('failure', [requests.ConnectionError('DO_NOT_PRINT_SECRET'), KeyboardInterrupt(), RuntimeError('unknown')])
def test_unknown_or_interrupted_charge_keeps_full_reservation_no_retry(tmp_path, failure):
    client, gate, _ = setup(tmp_path, cost=6, fail=failure)
    with pytest.raises((RuntimeError, KeyboardInterrupt)): download(client, gate)
    assert gate.snapshot()['cumulative_reserved_usd'] == '6'
    assert gate.snapshot()['unresolved'] == 1
    assert 'DO_NOT_PRINT_SECRET' not in gate.journal.read_text()
    calls = len(client.session.calls)
    with pytest.raises(ValueError, match='NO_AUTO_REBILL'): download(client, gate)
    assert len(client.session.calls) == calls
    client.session.fail = None
    with pytest.raises(ValueError, match='LIMIT_EXCEEDED'): download(client, gate, plan('ESH2'))
    assert len(billable_calls(client)) == 1


def test_reservation_without_response_or_partial_still_forbids_repeat(tmp_path):
    client, gate, _ = setup(tmp_path)
    estimate = client.estimate(plan())
    gate._reserve_fresh_quote(plan().parameters(), estimate, api_key=TEST_KEY)
    with pytest.raises(ValueError, match='NO_AUTOMATIC_RETRY'): download(client, gate)
    assert not billable_calls(client)


@pytest.mark.parametrize('field', ['credit_balance_usd', 'credit_expires_at', 'monthly_usage_usd',
    'monthly_limit_usd', 'credit_scope', 'credit_application_terms', 'api_key_masked', 'billing_month'])
def test_missing_real_source_field_cannot_be_replaced_by_verified_true(tmp_path, field):
    client, gate, path = setup(tmp_path)
    edit_manifest(path, lambda m: m['fields'].__setitem__(field, {'verified': True, 'value': 125}))
    with pytest.raises(ValueError, match='REQUIRED_SOURCE_FIELD'): download(client, gate)
    assert not billable_calls(client)


@pytest.mark.parametrize('source,before,after,reason', [
    ('billing', '2099-01-01', '2000-01-01', 'CREDIT_EXPIRES'),
    ('billing', 'Historical', 'Live', 'APPLICABILITY'),
    ('billing', 'Enabled', 'Disabled', 'NOT_ENABLED'),
    ('policy', 'automatically applied before', 'possibly applied after', 'BEFORE_CASH'),
    ('keys', 'db-abcdefgh', 'db-wrongkey', 'BINDING_UNPROVEN'),
    ('billing', '$125,00', '$NaN', 'INVALID_AMOUNT'),
])
def test_ineligible_or_unknown_credit_fails_closed(tmp_path, source, before, after, reason):
    client, gate, path = setup(tmp_path)
    replace_source(path, source, before, after)
    with pytest.raises(ValueError, match=reason): download(client, gate)
    assert not billable_calls(client)


def test_real_dom_key_digest_binding_and_raw_capture_hash_required(tmp_path):
    client, gate, path = setup(tmp_path, dom_key=True)
    value = gate.archive.verify(plan().parameters(), TEST_KEY, gate.deadline)
    assert value['account'] == 'account-test-123'
    with pytest.raises(ValueError, match='BINDING_UNPROVEN'):
        gate.archive.verify(plan().parameters(), 'db-different', gate.deadline)
    replace_source(path, 'keys', 'Original DOM SHA256 (not retained)', 'Unsupported assertion')
    with pytest.raises(ValueError, match='BINDING_UNPROVEN'): download(client, gate)


def test_expiry_disagreement_uses_earliest_and_is_preserved(tmp_path):
    client, gate, path = setup(tmp_path)
    replace_source(path, 'billing', 'Credit expiry: 2099-01-01', 'Credit expiry: 2099-01-01\nSummary expiry: 2099-04-01')
    edit_manifest(path, lambda m: m['fields'].__setitem__('credit_expiry_summary', dict(source='billing', pattern=r'Summary expiry: (?P<value>[^\n]+)')))
    download(client, gate)
    first = json.loads(gate.journal.read_text().splitlines()[0])
    assert first['credit_expiry_conflict'] is True
    assert first['credit_expiries'][0].startswith('2099-01-01')


def test_source_hash_tamper_and_manifest_replacement_are_blocked(tmp_path):
    client, gate, path = setup(tmp_path)
    download(client, gate)
    raw = path.parent / 'billing.txt'; original = raw.read_text(); raw.write_text(original + 'tamper')
    with pytest.raises(ValueError, match='SOURCE_HASH_MISMATCH'): download(client, gate, plan('ESH2'))
    raw.write_text(original)
    edit_manifest(path, lambda m: m.__setitem__('note', 'changed after first reservation'))
    with pytest.raises(ValueError, match='PINNED_EVIDENCE_HASH_CHANGED'): download(client, gate, plan('ESH2'))
    assert len(billable_calls(client)) == 1


def test_wrong_visible_account_and_session_are_rejected(tmp_path):
    client, gate, path = setup(tmp_path)
    replace_source(path, 'billing', 'Credit balance', 'User ID: wrong-account\nCredit balance')
    edit_manifest(path, lambda m: m['sources']['billing'].__setitem__('account_pattern', r'User ID: (?P<value>[^\n]+)'))
    with pytest.raises(ValueError, match='WRONG_ACCOUNT'): download(client, gate)
    edit_manifest(path, lambda m: m['sources']['billing'].__setitem__('browser_session_id', 'DIFFERENT'))
    with pytest.raises(ValueError, match='SESSION_MISMATCH'): download(client, gate)


@pytest.mark.parametrize('age_hours', [-1, 2])
def test_evidence_time_boundary(tmp_path, age_hours):
    client, gate, path = setup(tmp_path)
    edit_manifest(path, lambda m: m['sources']['billing'].__setitem__('captured_at', (utcnow() - timedelta(hours=age_hours)).isoformat()))
    with pytest.raises(ValueError, match='STALE_OR_FUTURE'): download(client, gate)
    assert not billable_calls(client)


def test_authorization_deadline_blocks_even_with_fresh_metadata(tmp_path):
    client, gate, _ = setup(tmp_path)
    gate.deadline = utcnow() - timedelta(seconds=1)
    with pytest.raises(ValueError, match='AUTHORIZATION_EXPIRED'): download(client, gate)
    assert not billable_calls(client)


@pytest.mark.parametrize('mutation', ['deadline', 'source', 'quote'])
def test_post_reservation_mutation_is_checked_before_send(tmp_path, monkeypatch, mutation):
    client, gate, path = setup(tmp_path, cost=2)
    original = gate._reserve_fresh_quote
    def reserve(*args, **kwargs):
        value = original(*args, **kwargs)
        if mutation == 'deadline':
            gate.deadline = utcnow() - timedelta(seconds=1)
        elif mutation == 'source':
            with (path.parent / 'billing.txt').open('a') as stream: stream.write('changed')
        else:
            with (tmp_path / value['quote_path']).open('a') as stream: stream.write('changed')
        return value
    monkeypatch.setattr(gate, '_reserve_fresh_quote', reserve)
    with pytest.raises(ValueError): download(client, gate)
    assert not billable_calls(client)
    assert gate.snapshot()['cumulative_reserved_usd'] == '2'
    assert gate.snapshot()['unresolved'] == 1


def test_durable_journal_corruption_fails_closed(tmp_path):
    client, gate, _ = setup(tmp_path)
    download(client, gate)
    with gate.journal.open('a') as stream: stream.write('{interrupted')
    with pytest.raises(ValueError, match='JOURNAL_CORRUPT'): download(client, gate, plan('ESH2'))
    assert len(billable_calls(client)) == 1


def test_process_lock_and_duplicate_concurrent_download_do_not_rebill(tmp_path):
    client, gate, _ = setup(tmp_path)
    gate.directory.mkdir()
    with FileLock(str(gate.directory / 'budget.lock')):
        with pytest.raises(ValueError, match='CONCURRENT'): download(client, gate)
    entered, release = threading.Event(), threading.Event()
    client.session.on_download = lambda: (entered.set(), release.wait(5))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(download, client, gate)
        assert entered.wait(5)
        try:
            with pytest.raises(ValueError, match='NO_AUTO_REBILL|NO_AUTOMATIC_RETRY'):
                download(client, gate)
        finally:
            release.set()
        first.result()
    assert len(billable_calls(client)) == 1 and gate.snapshot()['requests'] == 1


def test_parallel_different_requests_share_single_atomic_cap(tmp_path):
    client, gate, _ = setup(tmp_path, cost=6)
    second = MetadataClient(Config(tmp_path, TEST_KEY), session=Session(6))
    barrier = threading.Barrier(2)
    def attempt(selected, symbol):
        barrier.wait()
        try: return download(selected, gate, plan(symbol))
        except ValueError as error: return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, client, 'MESH2'), pool.submit(attempt, second, 'ESH2')]
        results = [future.result() for future in futures]
    assert sum(isinstance(r, dict) for r in results) == 1
    assert len(billable_calls(client)) + len(billable_calls(second)) == 1
    assert gate.snapshot()['cumulative_reserved_usd'] == '6'
