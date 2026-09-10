"""AI-M1 research-only configuration; never changes the existing shadow ledger."""
from dataclasses import dataclass, field
from datetime import date
import os
from pathlib import Path
from typing import Mapping

TRADE_UNIVERSE = ("NVDA", "AMD", "AVGO", "MRVL", "MU", "WDC", "STX", "LITE",
                  "AAOI", "QCOM", "ALAB", "TSLA", "PLTR", "MSFT", "RKLB")
REFERENCES = ("SPY", "QQQ", "SOXX")
SYMBOLS = TRADE_UNIVERSE + REFERENCES
PROBE_SYMBOLS = ("NVDA", "AMD", "MSFT")
HISTORY_START = date(2016, 1, 1)
FUTURE_INITIAL_CAPITAL_USD = 5500
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DataConfig:
    api_key: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    data_dir: Path = PROJECT_ROOT / "data" / "alpaca"
    revision_sessions: int = 5
    # Operational cutoff, NOT an Alpaca guarantee of immutable final prices.
    finalize_hour_ny: int = 6
    requests_per_minute: int = 120

    @property
    def has_credentials(self):
        return bool(self.api_key and self.secret_key)


def load_config(secrets: Mapping | None = None) -> DataConfig:
    def read(name):
        try:
            value = secrets.get(name, "") if secrets is not None else ""
        except (FileNotFoundError, KeyError):
            value = ""
        return str(value or os.environ.get(name, "")).strip()

    path = read("ALPACA_DATA_DIR")
    root = Path(path).expanduser() if path else PROJECT_ROOT / "data" / "alpaca"
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return DataConfig(read("ALPACA_API_KEY"), read("ALPACA_SECRET_KEY"), root.resolve())
