"""Read exactly the designated F0 key; never search user files for credentials."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Config:
    data_dir: Path
    api_key: str = field(default='', repr=False)
    key_source: str = 'ABSENT'


def load_config(data_dir=None, secrets_file=None):
    p = Path(secrets_file or os.getenv('FUTURES_F0_SECRETS_FILE') or ROOT / '.streamlit/secrets.toml')
    secret = {}
    if p.exists():
        try:
            secret = tomllib.loads(p.read_text(encoding='utf-8-sig'))
        except (ValueError, OSError):
            raise ValueError('INVALID_F0_SECRETS_CONFIG') from None
    key = os.getenv('DATABENTO_API_KEY', '').strip() or str(secret.get('DATABENTO_API_KEY', '')).strip()
    location = data_dir or os.getenv('FUTURES_F0_DATA_DIR') or secret.get('FUTURES_F0_DATA_DIR')
    if not location:
        raise ValueError('EXPLICIT_FUTURES_F0_DATA_DIR_REQUIRED')
    output = Path(location).expanduser().resolve()
    if output == ROOT or output.is_relative_to(ROOT):
        raise ValueError('F0_DATA_MUST_BE_OUTSIDE_SOURCE_WORKTREE')
    # A named project output prevents accidental writes into existing stock data.
    if not output.name.startswith('futures-f0-'):
        raise ValueError('F0_REQUIRES_OWN_FUTURES_F0_DIRECTORY')
    return Config(output, key, 'ENVIRONMENT' if os.getenv('DATABENTO_API_KEY', '').strip()
                  else str(p) if key else 'ABSENT')
