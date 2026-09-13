"""Narrow, read-only bridge to existing project credentials; never returns values in reports."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tomllib
from datetime import datetime, timezone
import requests

BASE = Path(r'C:\Users\benhe\OneDrive\Documentos\GITHUB')
RELATED = ('premarket-radar-ben-b1-1', 'premarket-radar-ben-b1', 'premarket-radar-ai-m1', 'premarket-radar')


def utc():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def flatten_keys(value, prefix=''):
    """Only names/presence escape this function, never config values."""
    found = []
    for name, item in value.items():
        key = f'{prefix}.{name}' if prefix else str(name)
        if isinstance(item, dict):
            found.extend(flatten_keys(item, key))
        else:
            found.append({'field': key, 'nonempty': bool(str(item).strip())})
    return found


def bridge_finnhub(base=BASE, environment=None, global_secrets=None):
    """Top-level FINNHUB_API_KEY is the only actually observed provider key.

    The related provider accepts Streamlit secrets, then environment, then .env.
    Streamlit's documented global secrets are inspected only at that exact path.
    We do not guess aliases or search unrelated repositories or the machine.
    """
    environment = os.environ if environment is None else environment
    inventory, sources, candidates = [], [], []
    paths = [Path(base) / name / '.streamlit/secrets.toml' for name in RELATED]
    paths += [Path(global_secrets) if global_secrets is not None else Path.home() / '.streamlit/secrets.toml']
    for path in paths:
        row = {'path': str(path), 'exists': path.is_file(), 'kind': 'STREAMLIT_SECRETS'}
        if path.is_file():
            try:
                data = tomllib.loads(path.read_text(encoding='utf-8-sig'))
                row['fields'] = flatten_keys(data)
                key = str(data.get('FINNHUB_API_KEY') or '').strip()
                if key:
                    candidates.append((key, {'path': str(path), 'field': 'FINNHUB_API_KEY'}))
            except (OSError, ValueError):
                row['status'] = 'INVALID_CONFIG'
        inventory.append(row)
    value = str(environment.get('FINNHUB_API_KEY') or '').strip()
    inventory.append({'path': 'PROCESS_ENVIRONMENT', 'field': 'FINNHUB_API_KEY', 'nonempty': bool(value)})
    if value:
        candidates.append((value, {'path': 'PROCESS_ENVIRONMENT', 'field': 'FINNHUB_API_KEY'}))
    for name in RELATED:
        path = Path(base) / name / '.env'
        row = {'path': str(path), 'exists': path.is_file(), 'kind': 'DOTENV'}
        if path.is_file():
            data = {}
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                    key, item = line.split('=', 1)
                    data[key.strip()] = item.strip().strip('\"\'')
            row['fields'] = flatten_keys(data)
            key = data.get('FINNHUB_API_KEY', '')
            if key:
                candidates.append((key, {'path': str(path), 'field': 'FINNHUB_API_KEY'}))
        inventory.append(row)
    for name in RELATED:
        path = Path(base) / name / 'src/data/finnhub_client.py'
        if path.is_file():
            sources.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                            'observed_field': 'FINNHUB_API_KEY', 'nesting': 'TOP_LEVEL',
                            'provider_order': ['STREAMLIT_SECRETS', 'PROCESS_ENVIRONMENT', 'DOTENV']})
    key, source = candidates[0] if candidates else ('', None)
    report = {'checked_at': utc(), 'credential_present': bool(key), 'selected_source': source,
              'candidate_sources': [source for _, source in candidates], 'config_inventory': inventory,
              'provider_evidence': sources, 'guessed_aliases_used': [], 'credential_values_logged': False,
              'status': 'CREDENTIAL_PRESENT_PERMISSION_NOT_TESTED' if key else 'CREDENTIAL_MISSING',
              'minimal_configuration_field': str(Path(base) / 'premarket-radar-ai-m1/.streamlit/secrets.toml') + ': FINNHUB_API_KEY'}
    return key, report


def classify_response(status, payload):
    """Keep authentication, endpoint entitlement, range and empty outcomes separate."""
    error = str(payload.get('error', '')) if isinstance(payload, dict) else ''
    low = error.lower()
    if status == 401 or any(word in low for word in ('invalid api key', 'invalid token', 'unauthorized')):
        return 'AUTH_FAILED'
    if any(word in low for word in ('historical range', 'history limit', 'date range', 'lookback')):
        return 'HISTORY_RANGE_LIMIT'
    if status == 403 or any(word in low for word in ('premium', 'subscription', 'access to this resource')):
        return 'ENDPOINT_ENTITLEMENT_DENIED'
    if status == 429:
        return 'RATE_LIMITED'
    if status >= 400 or error:
        return 'REQUEST_FAILED'
    rows = payload.get('earningsCalendar', []) if isinstance(payload, dict) else payload
    return 'ACCESS_OK' if rows else 'EMPTY_RESPONSE'


def probe_finnhub(output, *, environment=None, base=BASE, global_secrets=None):
    key, report = bridge_finnhub(base, environment, global_secrets)
    report.update({'calls': [], 'request_attempt_count': 0, 'hidden_retry_count': 0,
                   'earnings_pit': 'NOT_ESTABLISHED', 'subscription_purchase': False})
    tests = [('recent_calendar', 'calendar/earnings', {'symbol': 'MRVL', 'from': '2026-08-01', 'to': '2026-10-31'}),
             ('fixed_window_calendar', 'calendar/earnings', {'symbol': 'MRVL', 'from': '2026-01-01', 'to': '2026-09-11'}),
             ('historical_calendar', 'calendar/earnings', {'symbol': 'MRVL', 'from': '2018-01-01', 'to': '2018-06-30'}),
             ('actual_earnings', 'stock/earnings', {'symbol': 'MRVL', 'limit': 12})]
    if key:
        for label, endpoint, params in tests:
            row = {'label': label, 'endpoint': endpoint, 'parameters': params, 'requested_at': utc()}
            try:
                report['request_attempt_count'] += 1
                response = requests.get('https://finnhub.io/api/v1/' + endpoint, params=params,
                                        headers={'X-Finnhub-Token': key}, timeout=(10, 30))
                payload = response.json()
                row.update({'received_at': utc(), 'http_status': response.status_code,
                            'status': classify_response(response.status_code, payload)})
                if response.ok and not (isinstance(payload, dict) and payload.get('error')):
                    rows = payload.get('earningsCalendar', []) if isinstance(payload, dict) else payload
                    row['rows'] = len(rows) if isinstance(rows, list) else None
                    atomic_json(Path(output) / 'data' / ('finnhub_' + label + '.json'),
                                {'receipt': row, 'payload': payload,
                                 'calendar_return_does_not_establish_historical_plan_availability': True,
                                 'earnings_period_is_fiscal_period_not_release_date': endpoint == 'stock/earnings'})
            except (requests.RequestException, ValueError):
                row.update({'received_at': utc(), 'status': 'NETWORK_OR_RESPONSE_ERROR'})
            report['calls'].append(row)
    else:
        report['planned_tests_not_executed'] = [{'label': x[0], 'reason': 'CREDENTIAL_MISSING'} for x in tests]
    report['permission_test_status'] = 'EXECUTED_SEE_SEPARATE_ENDPOINT_RESULTS' if key else 'NOT_TESTED_CREDENTIAL_MISSING'
    if key:
        report['status'] = 'ENDPOINT_TESTS_COMPLETED'
    report['complete'] = True
    atomic_json(Path(output) / 'CREDENTIAL_CAPABILITY_REDACTED.json', report)
    return report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = probe_finnhub(args.output)
    print(json.dumps({'status': result['status'], 'calls': result['calls']}, ensure_ascii=False))
