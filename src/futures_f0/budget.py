"""Fail-closed, credit-only historical-download reservations.

An evidence manifest contains selectors and hashes, never asserted balances or
``verified`` flags. Values are read from archived visible Databento pages. The
archive is auditable evidence, not a cryptographic attestation by the vendor.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid

from filelock import FileLock, Timeout

from .runtime import canonical_hash, digest, safe_child, utcnow, write_json


ROUND_CAP = Decimal('10')
EVIDENCE_MAX_AGE_SECONDS = 3600
QUOTE_MAX_AGE_SECONDS = 60
FORMAT = 'futures-f0-databento-visible-dom-v1'
APPLICATION_SENTENCE = 'credits are automatically applied before any charges are made to the user'


def _at(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        raise ValueError('BUDGET_INVALID_TIMESTAMP') from None
    if result.tzinfo is None:
        raise ValueError('BUDGET_TIMESTAMP_REQUIRES_TIMEZONE')
    return result.astimezone(timezone.utc)


def _money(value, *, decimal_separator='.', quote=False):
    if quote and type(value) not in (int, float):
        raise ValueError('BUDGET_INVALID_QUOTE_AMOUNT')
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('BUDGET_INVALID_AMOUNT')
    text = str(value).strip()
    if isinstance(value, str):
        if decimal_separator not in ('.', ','):
            raise ValueError('BUDGET_UNKNOWN_AMOUNT_LOCALE')
        other = ',' if decimal_separator == '.' else '.'
        pattern = (r'(?:USD\s*|\$\s*)?[0-9]+(?:' + re.escape(other) + r'[0-9]{3})*'
                   r'(?:' + re.escape(decimal_separator) + r'[0-9]+)?(?:\s*USD)?')
        if not re.fullmatch(pattern, text):
            raise ValueError('BUDGET_INVALID_AMOUNT')
        text = text.replace('USD', '').replace('$', '').strip().replace(other, '')
        text = text.replace(decimal_separator, '.')
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError('BUDGET_INVALID_AMOUNT') from None
    if not amount.is_finite() or amount < 0:
        raise ValueError('BUDGET_INVALID_AMOUNT')
    return amount


def _expiry(value):
    text = value.strip()
    # A date without a timezone is conservatively the beginning of that UTC day.
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        return _at(text + 'T00:00:00Z')
    return _at(text)


def _one(text, pattern):
    if not isinstance(pattern, str) or len(pattern) > 2000:
        raise ValueError('BUDGET_INVALID_DOM_SELECTOR')
    try:
        regex = re.compile(pattern)
        if 'value' not in regex.groupindex:
            raise ValueError('BUDGET_SELECTOR_REQUIRES_VALUE_GROUP')
        found = list(regex.finditer(text))
    except re.error:
        raise ValueError('BUDGET_INVALID_DOM_SELECTOR') from None
    if len(found) != 1 or not found[0].group('value'):
        raise ValueError('BUDGET_DOM_FIELD_MISSING_OR_AMBIGUOUS')
    return found[0].group('value').strip()


class EvidenceArchive:
    """Parse source-grounded values; no caller-provided verification booleans."""

    def __init__(self, manifest):
        self.path = Path(manifest).resolve()

    def verify(self, parameters, api_key, deadline):
        now = utcnow()
        if now >= deadline:
            raise ValueError('BUDGET_AUTHORIZATION_EXPIRED')
        raw = self.path.read_bytes()
        manifest_hash = hashlib.sha256(raw).hexdigest()
        manifest = json.loads(raw.decode('utf-8-sig'))
        if manifest.get('format') != FORMAT or 'verified' in manifest:
            raise ValueError('BUDGET_SOURCE_ARCHIVE_REQUIRED')
        session_id = manifest.get('browser_session_id')
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError('BUDGET_VISIBLE_BROWSER_SESSION_REQUIRED')
        sources = manifest.get('sources', {})
        if not isinstance(sources, dict) or not sources:
            raise ValueError('BUDGET_SOURCES_REQUIRED')
        capture_ref = manifest.get('capture_provenance', {})
        if set(capture_ref) != {'path', 'sha256'}:
            raise ValueError('BUDGET_BROWSER_CAPTURE_PROVENANCE_REQUIRED')
        capture_path = safe_child(self.path.parent, capture_ref['path'])
        capture_bytes = capture_path.read_bytes()
        if hashlib.sha256(capture_bytes).hexdigest() != capture_ref['sha256']:
            raise ValueError('BUDGET_CAPTURE_PROVENANCE_HASH_MISMATCH')
        capture = json.loads(capture_bytes.decode('utf-8-sig'))
        if capture.get('browser_session_id') != session_id:
            raise ValueError('BUDGET_CAPTURE_SESSION_MISMATCH')
        loaded, source_hashes = {}, {}
        source_hashes['capture_provenance'] = capture_ref['sha256']
        for name, source in sources.items():
            if not isinstance(source, dict) or 'verified' in source:
                raise ValueError('BUDGET_SOURCE_ARCHIVE_REQUIRED')
            url = urlsplit(source.get('source_url', ''))
            if url.scheme != 'https' or url.hostname not in ('databento.com', 'www.databento.com') or url.username or url.password or url.query or url.fragment:
                raise ValueError('BUDGET_OFFICIAL_SOURCE_URL_REQUIRED')
            captured = _at(source['captured_at'])
            if not 0 <= (now - captured).total_seconds() <= EVIDENCE_MAX_AGE_SECONDS:
                raise ValueError('BUDGET_SOURCE_STALE_OR_FUTURE')
            scope = source.get('scope')
            if scope == 'account':
                if not url.path.startswith('/portal/') or source.get('browser_session_id') != session_id:
                    raise ValueError('BUDGET_ACCOUNT_BROWSER_SESSION_MISMATCH')
                captured_sources = [s for s in capture.get('sources', []) if s.get('file') == source['path'] and s.get('url') == source['source_url']]
                if len(captured_sources) != 1 or _at(capture['captured_at']) != captured:
                    raise ValueError('BUDGET_CAPTURE_SOURCE_ASSOCIATION_UNPROVEN')
            elif scope == 'public':
                if not url.path.startswith('/docs/'):
                    raise ValueError('BUDGET_OFFICIAL_TERMS_REQUIRED')
            else:
                raise ValueError('BUDGET_UNKNOWN_SOURCE_SCOPE')
            path = safe_child(self.path.parent, source['path'])
            data = path.read_bytes()
            hashed = hashlib.sha256(data).hexdigest()
            if hashed != source.get('sha256'):
                raise ValueError('BUDGET_SOURCE_HASH_MISMATCH')
            loaded[name] = data.decode('utf-8-sig')
            source_hashes[name] = hashed

        account_source = manifest.get('account_source')
        if account_source not in loaded or sources[account_source]['scope'] != 'account':
            raise ValueError('BUDGET_UNIQUE_ACCOUNT_SOURCE_REQUIRED')
        if capture.get('account_source') != sources[account_source]['path']:
            raise ValueError('BUDGET_CAPTURE_ACCOUNT_ASSOCIATION_UNPROVEN')
        account = _one(loaded[account_source], manifest.get('account_pattern')).casefold()
        if len(account) < 6 or account in ('unknown', 'n/a', 'account', 'databento'):
            raise ValueError('BUDGET_UNIQUE_ACCOUNT_ID_REQUIRED')
        # A page that visibly identifies an account must agree with the linked
        # account page. Other account pages are tied to the same capture session.
        for name, source in sources.items():
            if source.get('account_pattern') and _one(loaded[name], source['account_pattern']).casefold() != account:
                raise ValueError('BUDGET_WRONG_ACCOUNT')

        fields = manifest.get('fields', {})

        def field(name, scope='account'):
            selector = fields.get(name)
            if not isinstance(selector, dict) or set(selector) != {'source', 'pattern'}:
                raise ValueError('BUDGET_REQUIRED_SOURCE_FIELD_' + name.upper())
            source = selector['source']
            if source not in loaded or sources[source]['scope'] != scope:
                raise ValueError('BUDGET_FIELD_SOURCE_SCOPE_MISMATCH')
            return _one(loaded[source], selector['pattern']), sources[source]

        key_field = 'api_key_dom_sha256' if 'api_key_dom_sha256' in fields else 'api_key_masked'
        key_value, key_source = field(key_field)
        if urlsplit(key_source['source_url']).path.rstrip('/') != '/portal/keys':
            raise ValueError('BUDGET_API_KEY_PAGE_REQUIRED')
        # Never retain or return the visible key fragment. Masked prefix must
        # contain at least eight key characters after the vendor's db- prefix.
        if key_field == 'api_key_dom_sha256':
            # This fingerprint is calculated from the visible DOM key before
            # redaction by the local capture, not from a config assertion. Its
            # exact provenance label and pre-redaction DOM hash are mandatory.
            dom_hash = re.findall(r'(?m)^DOM key SHA256: ([0-9a-f]{64})\s*$', loaded[fields[key_field]['source']])
            raw_hash = re.findall(r'(?m)^Original (?:visible )?DOM SHA256(?: \(not retained\))?: ([0-9a-f]{64})\s*$', loaded[fields[key_field]['source']])
            if len(dom_hash) != 1 or len(raw_hash) != 1 or key_value != dom_hash[0] or not api_key or key_value != hashlib.sha256(api_key.encode()).hexdigest():
                raise ValueError('BUDGET_CONFIGURED_KEY_ACCOUNT_BINDING_UNPROVEN')
        else:
            key_match = re.fullmatch(r'(db-[A-Za-z0-9_-]{8,})(?:\*+|\.{3}|…+)([A-Za-z0-9_-]*)', key_value)
            if not key_match or not api_key or not api_key.startswith(key_match[1]) or (key_match[2] and not api_key.endswith(key_match[2])):
                raise ValueError('BUDGET_CONFIGURED_KEY_ACCOUNT_BINDING_UNPROVEN')

        amounts = {}
        for name in ('credit_balance_usd', 'monthly_usage_usd', 'monthly_limit_usd'):
            value, source = field(name)
            amounts[name] = _money(value, decimal_separator=source.get('decimal_separator', '.'))
        month, month_source = field('billing_month')
        if month.casefold() == 'current billing cycle':
            month = _at(month_source['captured_at']).strftime('%Y-%m')
        if month != now.strftime('%Y-%m'):
            raise ValueError('BUDGET_CURRENT_MONTH_USAGE_REQUIRED')
        access, _ = field('usage_based_access')
        if access.casefold() != 'enabled':
            raise ValueError('BUDGET_USAGE_BASED_ACCESS_NOT_ENABLED')
        scope, _ = field('credit_scope')
        if scope.casefold() != 'historical':
            raise ValueError('BUDGET_HISTORICAL_CREDIT_APPLICABILITY_UNKNOWN')
        if parameters.get('dataset') != 'GLBX.MDP3' or parameters.get('schema') not in ('definition', 'ohlcv-1h', 'ohlcv-1m', 'statistics', 'status'):
            raise ValueError('BUDGET_DATASET_SCHEMA_OUTSIDE_AUTHORIZATION')
        terms, _ = field('credit_application_terms', scope='public')
        if APPLICATION_SENTENCE not in ' '.join(terms.casefold().split()).rstrip('.'):
            raise ValueError('BUDGET_AUTOMATIC_CREDIT_BEFORE_CASH_UNPROVEN')
        expiry, _ = field('credit_expires_at')
        expiries = [_expiry(expiry)]
        if 'credit_expiry_summary' in fields:
            other, _ = field('credit_expiry_summary')
            expiries.append(_expiry(other))
        if min(expiries) <= deadline:
            raise ValueError('BUDGET_CREDIT_EXPIRES_BEFORE_AUTHORIZATION_END')
        # Reading the manifest again catches concurrent evidence replacement.
        if digest(self.path) != manifest_hash:
            raise ValueError('BUDGET_MANIFEST_HASH_CHANGED')
        return dict(manifest_sha256=manifest_hash, source_hashes=source_hashes,
                    account=account, key_sha256=hashlib.sha256(api_key.encode()).hexdigest(),
                    month=month, credit_expiries=[d.isoformat() for d in sorted(set(expiries))],
                    credit_expiry_conflict=len(set(expiries)) > 1,
                    applicability=dict(service=scope, dataset=parameters['dataset'], schema=parameters['schema']),
                    **amounts)


class BudgetGate:
    """One durable journal for this authorized run, with no release operation."""

    def __init__(self, data_dir, evidence_manifest, *, deadline):
        self.data_dir = Path(data_dir).resolve()
        self.archive = EvidenceArchive(evidence_manifest)
        self.deadline = _at(deadline)
        self.directory = self.data_dir / 'credit_budget'
        self.journal = self.directory / 'journal.jsonl'

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(str(self.directory / 'budget.lock'), timeout=0):
                yield
        except Timeout:
            raise ValueError('BUDGET_CONCURRENT_REQUEST_REJECTED') from None

    def _read(self):
        if not self.journal.exists():
            if (self.directory / 'quotes').exists():
                raise ValueError('BUDGET_MISSING_JOURNAL_REQUIRES_REVIEW')
            return []
        result, previous = [], None
        for line in self.journal.read_text(encoding='utf-8').splitlines():
            try:
                value = json.loads(line)
                checksum = value.pop('sha256')
                if value.get('previous_sha256') != previous or canonical_hash(value) != checksum:
                    raise ValueError('BUDGET_JOURNAL_HASH_MISMATCH')
            except (ValueError, KeyError, TypeError):
                raise ValueError('BUDGET_JOURNAL_CORRUPT_REQUIRES_REVIEW') from None
            value['sha256'] = checksum
            result.append(value)
            previous = checksum
        if not result or result[0].get('event') != 'OPEN':
            raise ValueError('BUDGET_JOURNAL_CORRUPT_REQUIRES_REVIEW')
        return result

    def _append(self, records, value):
        value = dict(value, at=utcnow().isoformat(), previous_sha256=records[-1]['sha256'] if records else None)
        value['sha256'] = canonical_hash(value)
        payload = (json.dumps(value, sort_keys=True, allow_nan=False) + '\n').encode()
        with self.journal.open('ab') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        records.append(value)
        return value

    def _reserve_fresh_quote(self, parameters, estimate, *, api_key):
        """Called only with MetadataClient's newly authenticated estimate."""
        cost = _money(estimate.get('get_cost'), quote=True)
        request_hash = canonical_hash(parameters)
        if estimate.get('request_sha256') != request_hash or estimate.get('request') != parameters or estimate.get('source') != 'https://hist.databento.com/v0/':
            raise ValueError('BUDGET_AUTHENTICATED_ESTIMATE_REQUEST_MISMATCH')
        if not 0 <= (utcnow() - _at(estimate['estimated_at'])).total_seconds() <= QUOTE_MAX_AGE_SECONDS:
            raise ValueError('BUDGET_FRESH_VENDOR_QUOTE_REQUIRED')
        with self._locked():
            evidence = self.archive.verify(parameters, api_key, self.deadline)
            records = self._read()
            if not records:
                salt = uuid.uuid4().hex
                binding = hashlib.sha256((salt + '\0' + evidence['account'] + '\0' + evidence['key_sha256']).encode()).hexdigest()
                self._append(records, dict(event='OPEN', round_cap_usd=str(ROUND_CAP),
                    deadline=self.deadline.isoformat(), month=evidence['month'], binding_salt=salt,
                    account_binding_sha256=binding, evidence_manifest_sha256=evidence['manifest_sha256'],
                    source_hashes=evidence['source_hashes'], credit_expiries=evidence['credit_expiries'],
                    credit_expiry_conflict=evidence['credit_expiry_conflict'],
                    credit_balance_usd=format(evidence['credit_balance_usd'], 'f'),
                    monthly_usage_usd=format(evidence['monthly_usage_usd'], 'f'),
                    monthly_limit_usd=format(evidence['monthly_limit_usd'], 'f')))
            first = records[0]
            binding = hashlib.sha256((first['binding_salt'] + '\0' + evidence['account'] + '\0' + evidence['key_sha256']).encode()).hexdigest()
            if binding != first['account_binding_sha256']:
                raise ValueError('BUDGET_WRONG_ACCOUNT_OR_KEY')
            if first['evidence_manifest_sha256'] != evidence['manifest_sha256'] or first['source_hashes'] != evidence['source_hashes']:
                raise ValueError('BUDGET_PINNED_EVIDENCE_HASH_CHANGED')
            if first['deadline'] != self.deadline.isoformat() or first['month'] != evidence['month']:
                raise ValueError('BUDGET_AUTHORIZATION_OR_MONTH_CHANGED')
            reserved = [r for r in records if r['event'] == 'RESERVE']
            if any(r['request_sha256'] == request_hash for r in reserved):
                raise ValueError('BUDGET_ALREADY_RESERVED_NO_AUTOMATIC_RETRY')
            used = sum((_money(r['reserved_cost_usd']) for r in reserved), Decimal(0))
            cap = min(ROUND_CAP, evidence['credit_balance_usd'],
                      min(ROUND_CAP, evidence['monthly_limit_usd']) - evidence['monthly_usage_usd'])
            if cost + used > cap:
                raise ValueError('BUDGET_ROUND_MONTH_OR_CREDIT_LIMIT_EXCEEDED')
            reservation_id = uuid.uuid4().hex
            quote_path = self.directory / 'quotes' / (reservation_id + '.json')
            write_json(quote_path, estimate, immutable=True)
            return self._append(records, dict(event='RESERVE', reservation_id=reservation_id,
                request_sha256=request_hash, request=parameters, reserved_cost_usd=format(cost, 'f'),
                cumulative_reserved_usd=format(used + cost, 'f'), available_cap_usd=format(cap, 'f'),
                quote_path=str(quote_path.relative_to(self.data_dir)), quote_sha256=digest(quote_path),
                evidence_manifest_sha256=evidence['manifest_sha256'],
                credit_applicability=evidence['applicability'], cash_authorization_usd='0'))

    def _assert_send_allowed(self, reservation, *, api_key):
        """Recheck preserved evidence and the deadline immediately before send."""
        with self._locked():
            evidence = self.archive.verify(reservation['request'], api_key, self.deadline)
            records = self._read()
            matching = [r for r in records if r.get('reservation_id') == reservation['reservation_id']]
            if len(matching) != 1 or matching[0] != reservation:
                raise ValueError('BUDGET_RESERVATION_STATE_REQUIRES_REVIEW')
            if evidence['manifest_sha256'] != records[0]['evidence_manifest_sha256'] or evidence['source_hashes'] != records[0]['source_hashes']:
                raise ValueError('BUDGET_PINNED_EVIDENCE_HASH_CHANGED')
            quote = safe_child(self.data_dir, reservation['quote_path'])
            if digest(quote) != reservation['quote_sha256']:
                raise ValueError('BUDGET_QUOTE_ARCHIVE_HASH_CHANGED')
            saved = json.loads(quote.read_text(encoding='utf-8'))
            if not 0 <= (utcnow() - _at(saved['estimated_at'])).total_seconds() <= QUOTE_MAX_AGE_SECONDS:
                raise ValueError('BUDGET_FRESH_VENDOR_QUOTE_REQUIRED')
            if utcnow() >= self.deadline:
                raise ValueError('BUDGET_AUTHORIZATION_EXPIRED')

    def _finish(self, reservation, *, receipt_path=None, failure=None):
        with self._locked():
            records = self._read()
            identity = reservation['reservation_id']
            matching = [r for r in records if r.get('reservation_id') == identity]
            if len(matching) != 1 or matching[0]['event'] != 'RESERVE' or matching[0]['sha256'] != reservation['sha256']:
                raise ValueError('BUDGET_RESERVATION_STATE_REQUIRES_REVIEW')
            value = dict(event='UNCERTAIN' if failure else 'COMPLETED', reservation_id=identity,
                         request_sha256=reservation['request_sha256'],
                         reserved_cost_usd=reservation['reserved_cost_usd'],
                         released_usd='0', cash_payment_status='NOT_INDEPENDENTLY_RECONCILED')
            if failure:
                value['failure_class'] = failure
            else:
                path = Path(receipt_path)
                value.update(receipt_path=str(path.relative_to(self.data_dir)), receipt_sha256=digest(path))
            return self._append(records, value)

    def snapshot(self):
        """Read-only summary; reserved/uncertain amounts are never refunded."""
        with self._locked():
            records = self._read()
            reserved = [r for r in records if r['event'] == 'RESERVE']
            return dict(round_cap_usd=str(ROUND_CAP), requests=len(reserved),
                        cumulative_reserved_usd=format(sum((_money(r['reserved_cost_usd']) for r in reserved), Decimal(0)), 'f'),
                        completed=sum(r['event'] == 'COMPLETED' for r in records),
                        unresolved=len(reserved) - sum(r['event'] == 'COMPLETED' for r in records),
                        cash_payment_status='NOT_INDEPENDENTLY_RECONCILED')
