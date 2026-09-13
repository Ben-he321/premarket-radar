"""Synthetic credentials only; these tests never contact providers."""
from pathlib import Path
import pytest
from src.ben_b1.credentials import bridge_finnhub, classify_response, probe_finnhub


@pytest.mark.parametrize('http,payload,expected', [
    (401, {}, 'AUTH_FAILED'),
    (403, {'error': 'You do not have access to this resource.'}, 'ENDPOINT_ENTITLEMENT_DENIED'),
    (400, {'error': 'Date range exceeds permitted historical range'}, 'HISTORY_RANGE_LIMIT'),
    (200, {'earningsCalendar': []}, 'EMPTY_RESPONSE'),
    (200, {'earningsCalendar': [{'symbol': 'TEST'}]}, 'ACCESS_OK'),
    (200, [{'period': '2026-06-30', 'actual': 1.0}], 'ACCESS_OK'),
    (200, {'error': 'Invalid API key'}, 'AUTH_FAILED'),
    (429, {}, 'RATE_LIMITED'),
])
def test_endpoint_outcomes_distinct(http, payload, expected):
    assert classify_response(http, payload) == expected


def test_existing_project_configuration_bridges_without_exposing_key(tmp_path):
    old = tmp_path / 'premarket-radar/.streamlit/secrets.toml'
    old.parent.mkdir(parents=True)
    old.write_text('FINNHUB_API_KEY = "SYNTHETIC_TEST_ONLY"\n', encoding='utf-8')
    key, report = bridge_finnhub(tmp_path, {}, tmp_path / 'global_missing')
    assert key == 'SYNTHETIC_TEST_ONLY'
    assert report['selected_source']['path'] == str(old)
    assert 'SYNTHETIC_TEST_ONLY' not in str(report)


def test_unobserved_nested_alias_does_not_imply_usable_credential(tmp_path):
    old = tmp_path / 'premarket-radar/.streamlit/secrets.toml'
    old.parent.mkdir(parents=True)
    old.write_text('[finnhub]\nkey = "SYNTHETIC_TEST_ONLY"\n', encoding='utf-8')
    key, report = bridge_finnhub(tmp_path, {}, tmp_path / 'global_missing')
    assert not key
    assert report['status'] == 'CREDENTIAL_MISSING'
    assert report['guessed_aliases_used'] == []


def test_missing_key_never_runs_endpoint_and_reports_not_tested(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Request made without credential')
    monkeypatch.setattr('src.ben_b1.credentials.requests.get', forbidden)
    report = probe_finnhub(tmp_path / 'out', base=tmp_path, environment={}, global_secrets=tmp_path / 'no_global')
    assert report['request_attempt_count'] == 0
    assert report['calls'] == []
    assert len(report['planned_tests_not_executed']) == 4
    assert all(x['reason'] == 'CREDENTIAL_MISSING' for x in report['planned_tests_not_executed'])
