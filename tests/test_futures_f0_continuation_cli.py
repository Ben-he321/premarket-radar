"""Regression checks for preserving old output when a continuation is invalid."""
import json

import pytest

from src.futures_f0 import __main__ as entry
from src.futures_f0 import acquisition
from src.futures_f0.config import Config


@pytest.mark.parametrize('relative', ['', 'old_output'])
def test_invalid_round_cannot_overwrite_old_error_record(tmp_path, monkeypatch, relative):
    root = tmp_path / 'offline-f0'
    old = root / relative
    old.mkdir(parents=True)
    prior = old / 'LAST_RUN_ERROR.json'
    prior.write_bytes(b'original old-round evidence\n')
    (old / 'ROUND_STATE.json').write_text(json.dumps({'data_root': str(root)}))
    config = Config(root)
    monkeypatch.setattr(entry, 'load_config', lambda *a: config)
    monkeypatch.setattr(acquisition, 'load_config', lambda *a: config)
    with pytest.raises(ValueError, match='SEPARATE_ROUND_DIRECTORY'):
        entry.main(['run-normalized', '--round-dir', str(old),
                    '--input-manifest', str(tmp_path / 'not_read.json')])
    assert prior.read_bytes() == b'original old-round evidence\n'
    assert not (old / 'run_errors').exists()
    assert not (root / 'runs').exists()


def test_continuation_cannot_overwrite_original_preflight(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError('Configuration/output must not be opened')
    monkeypatch.setattr(entry, 'load_config', forbidden)
    with pytest.raises(SystemExit) as error:
        entry.main(['preflight', '--round-dir', str(tmp_path / 'round')])
    assert error.value.code == 2
    assert not (tmp_path / 'round').exists()
