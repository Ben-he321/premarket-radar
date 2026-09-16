"""Offline renewal regressions: synthetic archives and journals only in tmp_path."""
from datetime import timedelta
import hashlib
import json

import pytest

from src.futures_f0.budget import AUTHORIZATION_FORMAT, BudgetGate, EvidenceArchive
from src.futures_f0.runtime import digest, utcnow
from test_futures_f0_budget import (TEST_KEY, archive, download, edit_manifest, plan,
                                    replace_source, setup)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def renewal(tmp_path, *, old_count=1, cost=1, reserve_only=False):
    client, old, old_manifest = setup(tmp_path, cost=cost)
    reservations = []
    for index in range(old_count):
        request = plan(f'ESH{index + 2}')
        if reserve_only:
            reservations.append(old._reserve_fresh_quote(request.parameters(), client.estimate(request), api_key=TEST_KEY))
        else:
            download(client, old, request)
    preserved = old.journal.read_bytes()
    output = tmp_path / 'continuations' / 'new_round'
    output.mkdir(parents=True)
    start = utcnow() - timedelta(seconds=1)
    deadline = start + timedelta(hours=4)
    state = dict(data_root=str(tmp_path), round_dir=str(output), start_utc=start.isoformat(),
                 hard_deadline_utc=deadline.isoformat(), listed_history_budget_usd=10,
                 cash_authorization_usd=0)
    save(output / 'ROUND_STATE.json', state)
    save(output / 'preservation' / 'BUDGET_PREFIX.json',
         dict(path=str(old.journal), bytes=len(preserved), lines=len(preserved.splitlines()),
              sha256=hashlib.sha256(preserved).hexdigest()))
    authorization = dict(format=AUTHORIZATION_FORMAT, data_root=str(tmp_path),
        start_utc=state['start_utc'], hard_deadline_utc=state['hard_deadline_utc'],
        listed_history_budget_usd=10, cash_authorization_usd=0,
        received_scope='Offline fixture: bounded credit-only continuation', source='Synthetic unit test',
        recorded_at=utcnow().isoformat(), round_state_sha256=digest(output / 'ROUND_STATE.json'),
        prior_journal_prefix_sha256=digest(output / 'preservation' / 'BUDGET_PREFIX.json'))
    save(output / 'AUTHORIZATION.json', authorization)
    fresh = archive(output)
    gate = BudgetGate(tmp_path, fresh, deadline=deadline.isoformat(),
                      round_state=output / 'ROUND_STATE.json', authorization=output / 'AUTHORIZATION.json')
    return client, old, gate, old_manifest, preserved, reservations


def authorize(gate, previous):
    return gate.authorize_continuation(plan('MESH2').parameters(), api_key=TEST_KEY,
                                       previous_evidence_manifest=previous)


def repin_authorization(gate):
    value = json.loads(gate.authorization.read_text())
    value['round_state_sha256'] = digest(gate.round_state)
    value['prior_journal_prefix_sha256'] = digest(gate.prior_journal_prefix)
    save(gate.authorization, value)


def records(gate):
    return [json.loads(line) for line in gate.journal.read_text().splitlines()]


def test_twelve_completions_preserved_byte_for_byte_and_cap_is_cumulative(tmp_path):
    client, old, gate, previous, preserved, _ = renewal(tmp_path, old_count=12, cost=0.5)
    prior_hash = digest(previous)
    authorization = authorize(gate, previous)
    assert gate.journal.read_bytes().startswith(preserved)
    assert digest(previous) == prior_hash
    assert [r['event'] for r in records(gate)][25:] == ['AUTHORIZATION', 'EVIDENCE']
    assert authorize(gate, previous) == authorization  # Idempotent; no duplicate events.
    client.session.cost = 4
    download(client, gate, plan('MESH2'))
    snapshot = gate.snapshot()
    assert snapshot['cumulative_reserved_usd'] == '10.0'
    assert snapshot['requests'] == snapshot['completed'] == 13
    client.session.cost = 0.00001
    with pytest.raises(ValueError, match='LIMIT_EXCEEDED'):
        download(client, gate, plan('MESM2'))
    assert gate.journal.read_bytes().startswith(preserved)
    assert len([r for r in records(gate) if r['event'] == 'OPEN']) == 1


def test_unresolved_reservation_survives_renewal_and_cannot_be_retried(tmp_path):
    client, old, gate, previous, preserved, reserved = renewal(tmp_path, reserve_only=True, cost=6)
    authorize(gate, previous)
    with pytest.raises(ValueError, match='AUTHORIZATION_OR_EVIDENCE_CHANGED'):
        gate._assert_send_allowed(reserved[0], api_key=TEST_KEY)
    with pytest.raises(ValueError, match='NO_AUTOMATIC_RETRY'):
        download(client, gate, plan('ESH2'))
    client.session.cost = 5
    with pytest.raises(ValueError, match='LIMIT_EXCEEDED'):
        download(client, gate, plan('MESH2'))
    assert gate.snapshot()['cumulative_reserved_usd'] == '6'
    assert gate.snapshot()['unresolved'] == 1
    assert gate.journal.read_bytes().startswith(preserved)


@pytest.mark.parametrize('field,value,reason', [
    ('cash_authorization_usd', 1, 'CAP_OR_CASH_CHANGED'),
    ('listed_history_budget_usd', 20, 'CAP_OR_CASH_CHANGED'),
    ('cash_authorization_usd', False, 'INVALID_AMOUNT'),
    ('data_root', 'some_other_root', 'DATA_ROOT_CHANGED'),
    ('round_state_sha256', '0' * 64, 'AUTHORIZATION_MISMATCH'),
    ('prior_journal_prefix_sha256', '0' * 64, 'AUTHORIZATION_MISMATCH'),
    ('received_scope', '', 'AUTHORIZATION_MISMATCH'),
])
def test_invalid_authorization_never_appends(tmp_path, field, value, reason):
    _, _, gate, previous, preserved, _ = renewal(tmp_path)
    edit_manifest(gate.authorization, lambda doc: doc.__setitem__(field, value))
    with pytest.raises(ValueError, match=reason):
        authorize(gate, previous)
    assert gate.journal.read_bytes() == preserved


@pytest.mark.parametrize('mutation,reason', [('long', 'MAX_FOUR_HOURS'), ('future', 'NOT_STARTED'),
                                           ('expired', 'AUTHORIZATION_EXPIRED'), ('root', 'DATA_ROOT_CHANGED')])
def test_round_boundaries_are_enforced(tmp_path, mutation, reason):
    _, _, gate, previous, preserved, _ = renewal(tmp_path)
    state = json.loads(gate.round_state.read_text())
    if mutation == 'long':
        state['hard_deadline_utc'] = (utcnow() + timedelta(hours=5)).isoformat()
    elif mutation == 'future':
        state['start_utc'] = (utcnow() + timedelta(minutes=1)).isoformat()
    elif mutation == 'expired':
        state['start_utc'] = (utcnow() - timedelta(hours=2)).isoformat()
        state['hard_deadline_utc'] = (utcnow() - timedelta(hours=1)).isoformat()
    else:
        state['data_root'] = str(tmp_path / 'other')
    save(gate.round_state, state)
    repin_authorization(gate)
    with pytest.raises(ValueError, match=reason):
        authorize(gate, previous)
    assert gate.journal.read_bytes() == preserved


@pytest.mark.parametrize('mutation', ['hash', 'length', 'path', 'boundary'])
def test_authorization_pins_the_exact_original_journal_prefix(tmp_path, mutation):
    _, _, gate, previous, preserved, _ = renewal(tmp_path)
    prefix = json.loads(gate.prior_journal_prefix.read_text())
    if mutation == 'hash':
        prefix['sha256'] = '0' * 64
    elif mutation == 'length':
        prefix['bytes'] = True
    elif mutation == 'path':
        prefix['path'] = str(tmp_path / 'other_journal')
    else:
        prefix['bytes'] -= 1
    save(gate.prior_journal_prefix, prefix)
    repin_authorization(gate)
    with pytest.raises(ValueError, match='JOURNAL_PREFIX_CHANGED'):
        authorize(gate, previous)
    assert gate.journal.read_bytes() == preserved


def test_new_reservations_since_the_authorized_prefix_require_new_authorization(tmp_path):
    client, old, gate, previous, preserved, _ = renewal(tmp_path)
    download(client, old, plan('MESH2'))
    before_attempt = gate.journal.read_bytes()
    with pytest.raises(ValueError, match='JOURNAL_PREFIX_CHANGED'):
        authorize(gate, previous)
    assert gate.journal.read_bytes() == before_attempt


@pytest.mark.parametrize('mutation,reason', [
    ('old_manifest', 'PRESERVED_EVIDENCE_CHANGED'), ('old_source', 'PRESERVED_EVIDENCE_CHANGED'),
    ('new_account', 'WRONG_ACCOUNT_OR_KEY'), ('new_key', 'BINDING_UNPROVEN'),
    ('stale', 'STALE_OR_FUTURE'), ('forged_verified', 'SOURCE_ARCHIVE_REQUIRED')])
def test_real_new_evidence_and_old_preservation_are_required(tmp_path, mutation, reason):
    _, _, gate, previous, preserved, _ = renewal(tmp_path)
    if mutation == 'old_manifest':
        edit_manifest(previous, lambda doc: doc.__setitem__('forged_freshness', 'changed'))
    elif mutation == 'old_source':
        (previous.parent / 'billing.txt').write_text('edited old source')
    elif mutation == 'new_account':
        replace_source(gate.archive.path, 'keys', 'account-test-123', 'another-account')
    elif mutation == 'new_key':
        replace_source(gate.archive.path, 'keys', 'db-abcdefgh', 'db-another1')
    elif mutation == 'stale':
        edit_manifest(gate.archive.path, lambda doc: doc['sources']['billing'].__setitem__(
            'captured_at', (utcnow() - timedelta(hours=2)).isoformat()))
    else:
        edit_manifest(gate.archive.path, lambda doc: doc.__setitem__('verified', True))
    with pytest.raises(ValueError, match=reason):
        authorize(gate, previous)
    assert gate.journal.read_bytes() == preserved


def test_new_path_alone_does_not_replace_a_new_capture(tmp_path):
    _, _, gate, previous, preserved, _ = renewal(tmp_path)
    gate.archive = EvidenceArchive(previous)
    with pytest.raises(ValueError, match='NEW_CAPTURE_REQUIRED'):
        authorize(gate, previous)
    assert gate.journal.read_bytes() == preserved


@pytest.mark.parametrize('path_name', ['authorization', 'round_state', 'prior_journal_prefix'])
def test_changed_authorization_documents_block_send_after_reserving(tmp_path, path_name):
    client, _, gate, previous, _, _ = renewal(tmp_path)
    authorize(gate, previous)
    request = plan('MESH2')
    reservation = gate._reserve_fresh_quote(request.parameters(), client.estimate(request), api_key=TEST_KEY)
    path = getattr(gate, path_name)
    path.write_bytes(path.read_bytes() + b'\n')
    with pytest.raises(ValueError, match='PINNED_AUTHORIZATION_FILE_CHANGED'):
        gate._assert_send_allowed(reservation, api_key=TEST_KEY)
    assert gate.snapshot()['unresolved'] == 1


def test_new_evidence_refresh_keeps_deadline_and_invalidates_old_send(tmp_path):
    client, _, gate, previous, _, _ = renewal(tmp_path)
    authorization = authorize(gate, previous)
    request = plan('MESH2')
    reservation = gate._reserve_fresh_quote(request.parameters(), client.estimate(request), api_key=TEST_KEY)
    before = gate.journal.read_bytes()
    new_capture = gate.round_state.parent / 'capture2'
    new_capture.mkdir()
    gate.archive = EvidenceArchive(archive(new_capture))
    new_record = gate.refresh_evidence(request.parameters(), api_key=TEST_KEY)
    assert new_record['deadline'] == authorization['deadline'] == gate.deadline.isoformat()
    assert new_record['authorization_sha256'] == authorization['sha256']
    assert gate.journal.read_bytes().startswith(before)
    with pytest.raises(ValueError, match='AUTHORIZATION_OR_EVIDENCE_CHANGED'):
        gate._assert_send_allowed(reservation, api_key=TEST_KEY)
    download(client, gate, plan('MESM2'))
    with pytest.raises(ValueError, match='NEW_CAPTURE_REQUIRED'):
        gate.refresh_evidence(request.parameters(), api_key=TEST_KEY)


def test_refresh_cannot_extend_deadline_or_change_old_manifest_in_place(tmp_path):
    _, _, gate, previous, _, _ = renewal(tmp_path)
    authorize(gate, previous)
    before = gate.journal.read_bytes()
    gate.deadline += timedelta(minutes=1)
    with pytest.raises(ValueError, match='AUTHORIZATION_OR_MONTH_CHANGED'):
        gate.refresh_evidence(plan().parameters(), api_key=TEST_KEY)
    gate.deadline -= timedelta(minutes=1)
    edit_manifest(gate.archive.path, lambda doc: doc.__setitem__('refreshed', True))
    with pytest.raises(ValueError, match='PRESERVED_EVIDENCE_CHANGED'):
        gate.refresh_evidence(plan().parameters(), api_key=TEST_KEY)
    assert gate.journal.read_bytes() == before


def test_legacy_gate_cannot_send_after_explicit_renewal(tmp_path):
    client, old, gate, previous, _, _ = renewal(tmp_path)
    authorize(gate, previous)
    with pytest.raises(ValueError, match='AUTHORIZATION_FILES_REQUIRED'):
        download(client, old, plan('MESH2'))


def test_continuation_cannot_be_activated_implicitly_by_a_download(tmp_path):
    client, _, gate, _, preserved, _ = renewal(tmp_path)
    with pytest.raises(ValueError, match='MUST_BE_EXPLICITLY_AUTHORIZED'):
        download(client, gate, plan('MESH2'))
    assert gate.journal.read_bytes() == preserved
    assert not any('timeseries.' in url for url in client.session.calls[4:])


def test_round_state_and_authorization_must_share_an_isolated_continuation(tmp_path):
    _, _, gate, previous, preserved, _ = renewal(tmp_path)
    original_state = gate.round_state
    gate.round_state = tmp_path / 'ROUND_STATE.json'
    gate.round_state.write_bytes(original_state.read_bytes())
    with pytest.raises(ValueError, match='SEPARATE_ROUND_DIRECTORY'):
        authorize(gate, previous)
    gate.round_state = original_state
    original_auth = gate.authorization
    gate.authorization = tmp_path / 'AUTHORIZATION.json'
    gate.authorization.write_bytes(original_auth.read_bytes())
    with pytest.raises(ValueError, match='SEPARATE_ROUND_DIRECTORY'):
        authorize(gate, previous)
    assert gate.journal.read_bytes() == preserved


def test_different_key_with_same_visible_mask_still_fails_original_binding(tmp_path):
    _, _, gate, previous, preserved, _ = renewal(tmp_path)
    with pytest.raises(ValueError, match='WRONG_ACCOUNT_OR_KEY'):
        gate.authorize_continuation(plan().parameters(), api_key='db-abcdefghA_DIFFERENT_KEY_ONLY',
                                    previous_evidence_manifest=previous)
    assert gate.journal.read_bytes() == preserved


def test_stale_old_archive_is_preserved_while_real_new_archive_is_verified(tmp_path, monkeypatch):
    from src.futures_f0 import budget as budget_module
    import test_futures_f0_budget as fixture_module
    _, _, gate, previous, preserved, _ = renewal(tmp_path)
    old_bytes = previous.read_bytes()
    future = utcnow() + timedelta(hours=2)
    monkeypatch.setattr(budget_module, 'utcnow', lambda: future)
    monkeypatch.setattr(fixture_module, 'utcnow', lambda: future)
    with pytest.raises(ValueError, match='STALE_OR_FUTURE'):
        EvidenceArchive(previous).verify(plan().parameters(), TEST_KEY, gate.deadline)
    recapture = gate.round_state.parent / 'later_capture'
    recapture.mkdir()
    gate.archive = EvidenceArchive(archive(recapture))
    authorize(gate, previous)
    assert gate.journal.read_bytes().startswith(preserved)
    assert previous.read_bytes() == old_bytes


def test_interrupted_authorization_without_evidence_is_fail_closed(tmp_path, monkeypatch):
    client, _, gate, previous, preserved, _ = renewal(tmp_path)
    append = gate._append
    def interrupted(records, value):
        if value['event'] == 'EVIDENCE':
            raise OSError('synthetic interrupted evidence append')
        return append(records, value)
    monkeypatch.setattr(gate, '_append', interrupted)
    with pytest.raises(OSError):
        authorize(gate, previous)
    assert records(gate)[-1]['event'] == 'AUTHORIZATION'
    with pytest.raises(ValueError, match='ACTIVE_EVIDENCE_REQUIRED'):
        download(client, gate, plan('MESH2'))
    assert gate.journal.read_bytes().startswith(preserved)
    assert gate.snapshot()['cumulative_reserved_usd'] == '1'


def test_capture_provenance_supports_individual_page_capture_times(tmp_path):
    _, _, gate, _, _, _ = renewal(tmp_path)
    manifest = json.loads(gate.archive.path.read_text())
    capture_path = gate.archive.path.parent / manifest['capture_provenance']['path']
    capture = json.loads(capture_path.read_text())
    for index, item in enumerate(capture['sources']):
        at = (utcnow() - timedelta(seconds=index + 3)).isoformat()
        item['captured_at'] = at
        for source in manifest['sources'].values():
            if source['path'] == item['file']:
                source['captured_at'] = at
    save(capture_path, capture)
    manifest['capture_provenance']['sha256'] = digest(capture_path)
    save(gate.archive.path, manifest)
    gate.archive.verify(plan().parameters(), TEST_KEY, gate.deadline)
    capture['sources'][0]['captured_at'] = utcnow().isoformat()
    save(capture_path, capture)
    manifest['capture_provenance']['sha256'] = digest(capture_path)
    save(gate.archive.path, manifest)
    with pytest.raises(ValueError, match='CAPTURE_SOURCE_ASSOCIATION_UNPROVEN'):
        gate.archive.verify(plan().parameters(), TEST_KEY, gate.deadline)
