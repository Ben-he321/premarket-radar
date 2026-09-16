"""Bounded re-derivation of a session, distinct from raw-object identity.

This proves reproducibility of the stated capture-prefix inputs. It does not
certify calendars, historical publication times or trading eligibility.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

from .runtime import canonical_hash, digest
from .session_inputs import DatedSession, integrate_session
from .vendor import EvidenceFile, RawSource

DERIVED_VIEWS = {'derivation_sha256', 'engine_record_candidate', 'raw_source_object_hashes'}


def derivation_hash(value):
    return canonical_hash({key: item for key, item in value.items() if key not in DERIVED_VIEWS})


def _bounded_path(value, root):
    path = Path(value).resolve()
    if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
        raise ValueError('DERIVATION_SOURCE_OUTSIDE_DECLARED_ROOT_OR_MISSING')
    return path


def _evidence(entry, root):
    return EvidenceFile(_bounded_path(entry['path'], root), entry['sha256'], entry['source'])


def verify_session_derivation(value, *, source_root, raw_hashes, registry_row=None, guard=None):
    """Re-select all records and recompute every derived field, streaming CSVs.

    Hashes bind physical CSV files and their receipts. Record selection uses the
    existing parser's physical end-line number and ordered original CSV fields;
    all streams are exhausted, including its final changed-file check.
    """
    if guard:
        guard.check({'stage': 'verify_session_derivation', 'contract_id': value.get('contract_id')})
    if value.get('derivation_contract_version') != 1:
        raise ValueError('UNSUPPORTED_SESSION_DERIVATION_CONTRACT')
    if derivation_hash(value) != value.get('derivation_sha256'):
        raise ValueError('SESSION_DERIVATION_PAYLOAD_HASH_MISMATCH')
    entries = value['sources']
    if not isinstance(entries, list) or not 1 <= len(entries) <= 64:
        raise ValueError('DERIVATION_SOURCE_COUNT_BOUNDARY')
    sources, seen = [], set()
    for entry in entries:
        if entry['source_sha256'] not in raw_hashes:
            raise ValueError('DERIVATION_RAW_OBJECT_NOT_IN_MANIFEST')
        if entry['request_sha256'] in seen:
            raise ValueError('DERIVATION_DUPLICATE_SOURCE_REQUEST')
        seen.add(entry['request_sha256'])
        path = _bounded_path(entry['path'], source_root)
        receipt = _bounded_path(entry['receipt_path'], source_root)
        if digest(receipt) != entry['receipt_sha256']:
            raise ValueError('DERIVATION_RECEIPT_HASH_MISMATCH')
        source = RawSource.from_receipt(path, receipt, price_encoding=entry['price_encoding'])
        actual = source.validate()
        if (actual['sha256'] != entry['source_sha256'] or
                actual['request_sha256'] != entry['request_sha256']):
            raise ValueError('DERIVATION_RAW_RECEIPT_BINDING_MISMATCH')
        sources.append(source)
    row = value['integration_inputs']['registry_row']
    if registry_row is not None and row != registry_row:
        raise ValueError('DERIVATION_REGISTRY_ROW_MISMATCH')
    calendar = _evidence(value['calendar']['evidence'], source_root)
    boundary = _evidence(value['boundaries']['evidence'], source_root)
    reference = _evidence(value['integration_inputs']['reference_evidence'], source_root)
    temporal = value.get('temporal')
    policy = _evidence(temporal['temporal_evidence'], source_root) if temporal else None
    context_entry=value['integration_inputs'].get('qualification_context')
    context=_evidence(context_entry,source_root) if context_entry else None
    if context is not None:
        context_value=context.read(kind='F0_CONTINUOUS_SOURCE_CONTEXT')
        if any(x['source_sha256'] not in raw_hashes for x in context_value['sources']):
            raise ValueError('QUALIFICATION_CONTEXT_RAW_OBJECT_NOT_IN_MANIFEST')
    rebuilt = integrate_session(sources,
        window=DatedSession(value['contract_id'], date.fromisoformat(value['session']), calendar),
        decision_at=value['decision_at'], registry_row=row, boundary_evidence=boundary,
        reference_evidence=reference, causality_evidence=policy,qualification_context=context, guard=guard)
    if rebuilt != value:
        raise ValueError('SESSION_DERIVATION_RECOMPUTATION_MISMATCH')
    # Evidence could change while raw records were being streamed.
    for evidence in (calendar, boundary, reference, policy, context):
        if evidence is not None and digest(evidence.path) != evidence.sha256:
            raise ValueError('DERIVATION_EVIDENCE_CHANGED_DURING_READ')
    for entry in entries:
        if digest(entry['receipt_path']) != entry['receipt_sha256']:
            raise ValueError('DERIVATION_RECEIPT_CHANGED_DURING_READ')
        if digest(entry['path']) != entry['source_sha256']:
            raise ValueError('DERIVATION_RAW_CHANGED_DURING_READ')
    for entry in (temporal or {}).get('source_review_documents', []):
        if digest(_bounded_path(entry['path'], source_root)) != entry['sha256']:
            raise ValueError('DERIVATION_POLICY_REVIEW_CHANGED_DURING_READ')
    return rebuilt


def verify_bar_derivation(item, value):
    """Bind all data/clock fields; qualification flags remain a separate gate."""
    candidate = value.get('engine_record_candidate')
    if candidate is None:
        raise ValueError('DERIVATION_HAS_NO_COMPLETE_SESSION_BAR')
    numeric = {'open', 'high', 'low', 'close', 'settlement'}
    review_fields = {'status', 'tradable_open', 'tradable_stop', 'session_verified', 'settlement_reference_at'}
    if value['integration_inputs'].get('qualification_context'):
        review_fields=set()
        if value.get('research_qualified') is not True:
            raise ValueError('RECOMPUTED_QUALIFICATION_FAILED')
    for key, expected in candidate.items():
        if key in review_fields:
            continue
        actual = item.get(key)
        equal = (Decimal(str(actual)) == Decimal(str(expected))
                 if key in numeric and actual is not None and expected is not None else actual == expected)
        if not equal:
            raise ValueError('BAR_DERIVATION_FIELD_MISMATCH:' + key)
