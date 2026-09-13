"""Synthetic temporary cache verification, no market requests."""
import json
import pandas as pd
import pytest
from src.ben_b1.b11_data import attach_page_receipts, hashfile


def test_page_receipt_is_actual_download_and_original_historical_receive_unknown(tmp_path):
    for page, stamp in enumerate(['2026-01-02T21:04:55Z', '2026-01-02T21:04:56Z']):
        path = tmp_path / f'page_{page:05d}.json'
        path.write_text(json.dumps({'quotes': {'TEST': [{'t': stamp, 'bp': 10, 'ap': 10.01}]}}))
        path.with_name(path.stem + '_receipt.json').write_text(json.dumps({'sha256': hashfile(path), 'source_received_at': f'2026-09-14T00:00:0{page}Z'}))
    frame = pd.DataFrame({'t': ['2026-01-02T21:04:55Z', '2026-01-02T21:04:56Z']})
    result = attach_page_receipts(frame, {'path': str(tmp_path), 'kind': 'quotes', 'last_source_received_at': '2026-09-14T00:00:01Z'})
    assert result.source_received_at.tolist() == ['2026-09-14T00:00:00Z', '2026-09-14T00:00:01Z']
    assert result.historical_network_received_at.tolist() == ['UNKNOWN', 'UNKNOWN']


def test_changed_page_is_rejected_before_replay(tmp_path):
    path = tmp_path / 'page_00000.json'
    path.write_text('{}')
    path.with_name(path.stem + '_receipt.json').write_text(json.dumps({'sha256': 'wrong'}))
    with pytest.raises(ValueError, match='CACHE_PAGE_HASH_MISMATCH'):
        attach_page_receipts(pd.DataFrame({'t': ['2026-01-02T21:04:55Z']}), {'path': str(tmp_path), 'kind': 'quotes', 'last_source_received_at': 'UNKNOWN'})


def test_legacy_cache_without_page_receipt_does_not_invent_one(tmp_path):
    result = attach_page_receipts(pd.DataFrame({'t': ['2026-01-02T21:04:55Z']}), {'path': str(tmp_path), 'kind': 'quotes', 'last_source_received_at': '2026-09-14T00:00:00Z'})
    assert result.source_received_at.iloc[0] == 'UNKNOWN'
