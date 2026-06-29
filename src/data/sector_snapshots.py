"""板块雷达历史快照：把每日板块强度和龙头保存到 Supabase。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from src.supabase_client import SupabaseRestClient, upsert_row


@dataclass
class SectorSnapshotSaveResult:
    """保存快照后的统计结果，供影子引擎写中文提示。"""

    ok: bool
    sector_rows: int = 0
    leader_rows: int = 0
    errors: list[str] = field(default_factory=list)


def _to_float(value: object, default: float = 0.0) -> float:
    """把 pandas/numpy 标量安全转成 float，失败时给默认值。"""

    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_text(value: object) -> str:
    """统一清洗 Supabase 文本字段。"""

    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _normalize_snapshot_date(snapshot_date: date | str | None) -> str:
    """保存为 YYYY-MM-DD，方便作为 upsert 唯一键的一部分。"""

    if snapshot_date is None:
        return date.today().isoformat()
    if isinstance(snapshot_date, date):
        return snapshot_date.isoformat()
    return str(snapshot_date)


def _safe_frame(value: object) -> pd.DataFrame:
    """从 radar_result 中安全取出 DataFrame。"""

    return value if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _sector_payloads(sectors_df: pd.DataFrame, snapshot_date: str) -> list[dict[str, Any]]:
    """把板块强度表转成 Supabase 行，保留原始指标数值。"""

    if sectors_df.empty:
        return []

    rows: list[dict[str, Any]] = []
    ranked = sectors_df.reset_index(drop=True)
    for index, item in ranked.iterrows():
        sector_name = _to_text(item.get("板块"))
        if not sector_name:
            continue
        rows.append(
            {
                "snapshot_date": snapshot_date,
                "板块": sector_name,
                "代表ETF": _to_text(item.get("代表ETF")),
                "涨跌幅%": _to_float(item.get("涨跌幅%")),
                "RVOL": _to_float(item.get("RVOL")),
                "热度分": _to_float(item.get("热度分")),
                "热度排名": int(index) + 1,
            }
        )
    return rows


def _leader_payloads(leaders_df: pd.DataFrame, snapshot_date: str) -> list[dict[str, Any]]:
    """把板块龙头表转成 Supabase 行，保留原始指标数值。"""

    if leaders_df.empty:
        return []

    rows: list[dict[str, Any]] = []
    for _, item in leaders_df.iterrows():
        sector_name = _to_text(item.get("板块"))
        ticker = _to_text(item.get("代码")).upper()
        if not sector_name or not ticker:
            continue
        rows.append(
            {
                "snapshot_date": snapshot_date,
                "板块": sector_name,
                "代码": ticker,
                "涨跌幅%": _to_float(item.get("涨跌幅%")),
                "成交量": _to_float(item.get("成交量")),
                "RVOL": _to_float(item.get("RVOL")),
            }
        )
    return rows


def save_sector_snapshot(
    radar_result: dict[str, object],
    snapshot_date: date | str | None = None,
    *,
    client: SupabaseRestClient | None = None,
) -> SectorSnapshotSaveResult:
    """保存板块强度和龙头快照。

    设计要点：
    - 使用 Supabase upsert，唯一键由 schema 保证，同一天重复保存会覆盖。
    - 保存原始数值指标，未来即使热度公式变化，也能用历史涨跌幅/RVOL 重算。
    - client 为空时友好失败，不抛异常，避免影子组合页崩溃。
    """

    if client is None:
        return SectorSnapshotSaveResult(ok=False, errors=["Supabase 未连接，板块快照未保存。"])

    snapshot_date_text = _normalize_snapshot_date(snapshot_date)
    sector_rows = _sector_payloads(_safe_frame(radar_result.get("sectors")), snapshot_date_text)
    leader_rows = _leader_payloads(_safe_frame(radar_result.get("leaders")), snapshot_date_text)
    result = SectorSnapshotSaveResult(ok=True)

    for payload in sector_rows:
        status = upsert_row(client, "sector_snapshots", payload, on_conflict="snapshot_date,板块")
        if status.ok:
            result.sector_rows += 1
        else:
            result.errors.append(status.message)

    for payload in leader_rows:
        status = upsert_row(client, "sector_leader_snapshots", payload, on_conflict="snapshot_date,板块,代码")
        if status.ok:
            result.leader_rows += 1
        else:
            result.errors.append(status.message)

    result.ok = not result.errors
    return result
