"""板块快照历史回填脚本。

默认只打印样本；只有显式传入 --write-supabase 才会写入 Supabase。
本脚本不跑回测，不修改任何选股、打分、市场总闸或现金闸逻辑。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sys
from typing import Any

import pandas as pd
import requests
import yfinance as yf


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import SECTOR_RADAR_CONFIG
from src.data.safe_yfinance import YFINANCE_TIMEOUT_SECONDS
from src.scoring.sector_radar import calculate_leader_score, calculate_sector_heat_score


SESSION = "prev_close"
DATA_SOURCE = "yfinance"
SOURCE = "backfill"
DEFAULT_DAYS = 504
DEFAULT_TOP_SECTOR_COUNT = 3
SUPABASE_TIMEOUT_SECONDS = 30


logging.getLogger("yfinance").setLevel(logging.CRITICAL)


@dataclass(frozen=True)
class TickerSnapshot:
    """某个 ticker 在某个交易日收盘后的历史快照。"""

    trade_date: str
    close: float
    previous_close: float
    volume: float
    avg_volume20: float

    @property
    def change_pct(self) -> float:
        return (self.close - self.previous_close) / self.previous_close * 100

    @property
    def rvol(self) -> float:
        return self.volume / self.avg_volume20

    @property
    def dollar_volume(self) -> float:
        return self.close * self.volume


@dataclass
class BackfillResult:
    """写库结果汇总。"""

    sector_upserts: int = 0
    leader_upserts: int = 0
    skipped_live_sector_rows: int = 0
    skipped_live_leader_rows: int = 0
    earliest_trade_date: str = ""
    latest_trade_date: str = ""


def _load_dotenv() -> None:
    """轻量读取 .env，避免新增 python-dotenv 依赖。"""

    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _load_streamlit_secrets() -> None:
    """本地脚本读取 .streamlit/secrets.toml；仓库不会提交该文件。"""

    secrets_path = PROJECT_ROOT / ".streamlit" / "secrets.toml"
    if not secrets_path.exists():
        return
    try:
        import tomllib
    except ModuleNotFoundError:
        return
    data = tomllib.loads(secrets_path.read_text(encoding="utf-8"))
    for key in ("SUPABASE_URL", "SUPABASE_KEY"):
        value = data.get(key)
        if value:
            os.environ.setdefault(key, str(value).strip())


def supabase_config() -> tuple[str, str]:
    """读取 Supabase 配置，不打印密钥。"""

    _load_dotenv()
    _load_streamlit_secrets()
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("未检测到 SUPABASE_URL / SUPABASE_KEY，请先配置环境变量或 .streamlit/secrets.toml。")
    return url, key


def supabase_headers(key: str) -> dict[str, str]:
    """统一 Supabase REST 请求头；不要把 key 打印到日志里。"""

    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def collect_tickers() -> list[str]:
    """从当前板块雷达配置收集 ETF 和成分股；回填使用同一份写死配置。"""

    tickers: set[str] = set()
    for sector_config in SECTOR_RADAR_CONFIG.values():
        tickers.add(str(sector_config["etf"]).upper())
        tickers.update(str(ticker).upper() for ticker in sector_config["tickers"])
    return sorted(tickers)


def download_history(tickers: list[str], *, period: str = "3y") -> pd.DataFrame:
    """一次性下载历史日线，避免每个交易日/每只股票重复请求。"""

    return yf.download(
        tickers=tickers,
        period=period,
        interval="1d",
        auto_adjust=False,
        group_by="ticker",
        progress=False,
        threads=True,
        timeout=YFINANCE_TIMEOUT_SECONDS,
    )


def ticker_frame(history: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """从 yfinance 批量下载结果里取出单个 ticker 的 OHLCV。"""

    if history.empty:
        return pd.DataFrame()
    if isinstance(history.columns, pd.MultiIndex):
        if ticker in history.columns.get_level_values(0):
            frame = history[ticker].copy()
        elif ticker in history.columns.get_level_values(-1):
            frame = history.xs(ticker, axis=1, level=-1).copy()
        else:
            return pd.DataFrame()
    else:
        frame = history.copy()

    expected = [column for column in ["Close", "Volume"] if column in frame.columns]
    if len(expected) < 2:
        return pd.DataFrame()
    frame = frame[expected].copy()
    frame["Close"] = pd.to_numeric(frame["Close"], errors="coerce")
    frame["Volume"] = pd.to_numeric(frame["Volume"], errors="coerce")
    return frame.dropna(subset=["Close", "Volume"]).sort_index()


def available_trade_dates(history: pd.DataFrame, days: int, *, include_today: bool = False) -> list[pd.Timestamp]:
    """取最近 N 个可回填交易日，默认排除今天未完成日线。"""

    dates = pd.DatetimeIndex(history.index).dropna().sort_values().unique()
    if not include_today:
        today = pd.Timestamp.today().normalize()
        dates = dates[dates < today]
    if len(dates) <= 21:
        return []
    return list(dates[-days:])


def snapshot_on_date(frame: pd.DataFrame, trade_date: pd.Timestamp) -> TickerSnapshot | None:
    """重建某 ticker 在 D 日收盘后的快照。

    防 look-ahead 的关键在这里：先执行 frame.loc[:trade_date]，后续只从这个切片里取数据。
    RVOL 的 20 日均量只取 D 日之前的 20 根 Volume，不包含 D 日，也不包含 D 之后。
    """

    history_until_d = frame.loc[:trade_date].dropna(subset=["Close", "Volume"])
    if len(history_until_d) < 22:
        return None

    current = history_until_d.iloc[-1]
    previous = history_until_d.iloc[-2]
    prior_volumes = history_until_d["Volume"].iloc[-21:-1]
    close = float(current["Close"])
    previous_close = float(previous["Close"])
    volume = float(current["Volume"])
    avg_volume20 = float(prior_volumes.mean())
    if close <= 0 or previous_close <= 0 or volume < 0 or avg_volume20 <= 0:
        return None

    return TickerSnapshot(
        trade_date=pd.Timestamp(history_until_d.index[-1]).date().isoformat(),
        close=close,
        previous_close=previous_close,
        volume=volume,
        avg_volume20=avg_volume20,
    )


def build_snapshot_for_date(
    history_by_ticker: dict[str, pd.DataFrame],
    trade_date: pd.Timestamp,
    *,
    top_sector_count: int = DEFAULT_TOP_SECTOR_COUNT,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按 build_sector_radar 的同一板块定义和公式重建单日快照。"""

    sector_rows: list[dict[str, Any]] = []
    leader_rows: list[dict[str, Any]] = []

    for sector_name, sector_config in SECTOR_RADAR_CONFIG.items():
        etf = str(sector_config["etf"]).upper()
        etf_snapshot = snapshot_on_date(history_by_ticker.get(etf, pd.DataFrame()), trade_date)
        if etf_snapshot is None:
            continue
        heat_score = calculate_sector_heat_score(etf_snapshot.change_pct, etf_snapshot.rvol)
        sector_rows.append(
            {
                "snapshot_date": etf_snapshot.trade_date,
                "trade_date": etf_snapshot.trade_date,
                "session": SESSION,
                "data_source": DATA_SOURCE,
                "source": SOURCE,
                "板块": sector_name,
                "代表ETF": etf,
                "涨跌幅%": etf_snapshot.change_pct,
                "RVOL": etf_snapshot.rvol,
                "热度分": heat_score,
            }
        )

    if not sector_rows:
        return pd.DataFrame(), pd.DataFrame()

    sectors_df = pd.DataFrame(sector_rows).sort_values("热度分", ascending=False).reset_index(drop=True)
    sectors_df["热度排名"] = sectors_df.index + 1
    strong_sectors = sectors_df.head(top_sector_count)

    for _, sector in strong_sectors.iterrows():
        sector_name = str(sector["板块"])
        stock_rows: list[dict[str, Any]] = []
        for ticker in SECTOR_RADAR_CONFIG[sector_name]["tickers"]:
            ticker = str(ticker).upper()
            snapshot = snapshot_on_date(history_by_ticker.get(ticker, pd.DataFrame()), trade_date)
            if snapshot is None:
                continue
            leader_score = calculate_leader_score(snapshot.change_pct, snapshot.rvol)
            stock_rows.append(
                {
                    "snapshot_date": snapshot.trade_date,
                    "trade_date": snapshot.trade_date,
                    "session": SESSION,
                    "data_source": DATA_SOURCE,
                    "source": SOURCE,
                    "板块": sector_name,
                    "代码": ticker,
                    "涨跌幅%": snapshot.change_pct,
                    "成交量": snapshot.volume,
                    "RVOL": snapshot.rvol,
                    "成交额": snapshot.dollar_volume,
                    "龙头分": leader_score,
                }
            )

        if not stock_rows:
            continue
        stock_df = pd.DataFrame(stock_rows)
        leaders = stock_df.sort_values(["龙头分", "成交额"], ascending=False).head(3)
        leader_rows.extend(
            leaders[["snapshot_date", "trade_date", "session", "data_source", "source", "板块", "代码", "涨跌幅%", "成交量", "RVOL"]].to_dict("records")
        )

    return sectors_df, pd.DataFrame(leader_rows)


def build_backfill_payloads(
    history_by_ticker: dict[str, pd.DataFrame],
    dates: list[pd.Timestamp],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """生成两张表的回填 payload。"""

    sector_payloads: list[dict[str, Any]] = []
    leader_payloads: list[dict[str, Any]] = []
    for index, trade_date in enumerate(dates, start=1):
        sectors_df, leaders_df = build_snapshot_for_date(history_by_ticker, trade_date)
        sector_payloads.extend(sectors_df.to_dict("records"))
        leader_payloads.extend(leaders_df.to_dict("records"))
        if index % 50 == 0:
            print(f"已重建 {index}/{len(dates)} 个交易日...")
    return sector_payloads, leader_payloads


def chunked(items: list[dict[str, Any]], size: int = 500) -> list[list[dict[str, Any]]]:
    """把 payload 切成 Supabase REST 可接受的小批次。"""

    return [items[index : index + size] for index in range(0, len(items), size)]


def fetch_live_keys(
    *,
    base_url: str,
    headers: dict[str, str],
    table_name: str,
    dates: list[str],
    key_columns: tuple[str, ...],
) -> set[tuple[str, ...]]:
    """读取已有 source=live 的 key；回填绝不覆盖这些行。"""

    live_keys: set[tuple[str, ...]] = set()
    rest_url = f"{base_url}/rest/v1/{table_name}"
    select_columns = ",".join(key_columns + ("source",))
    for date_chunk in [dates[index : index + 80] for index in range(0, len(dates), 80)]:
        response = requests.get(
            rest_url,
            headers=headers,
            params={
                "select": select_columns,
                "source": "eq.live",
                "trade_date": f"in.({','.join(date_chunk)})",
            },
            timeout=SUPABASE_TIMEOUT_SECONDS,
        )
        if not response.ok:
            raise RuntimeError(f"读取 {table_name} live 行失败：{response.text[:300]}")
        rows = response.json()
        if not isinstance(rows, list):
            continue
        for row in rows:
            live_keys.add(tuple(str(row.get(column, "")) for column in key_columns))
    return live_keys


def filter_out_live_rows(
    sector_payloads: list[dict[str, Any]],
    leader_payloads: list[dict[str, Any]],
    *,
    base_url: str,
    headers: dict[str, str],
    dates: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """删除将会覆盖 live 的 backfill payload。"""

    live_sector_keys = fetch_live_keys(
        base_url=base_url,
        headers=headers,
        table_name="sector_snapshots",
        dates=dates,
        key_columns=("trade_date", "板块"),
    )
    live_leader_keys = fetch_live_keys(
        base_url=base_url,
        headers=headers,
        table_name="sector_leader_snapshots",
        dates=dates,
        key_columns=("trade_date", "板块", "代码"),
    )

    filtered_sectors = [row for row in sector_payloads if (str(row["trade_date"]), str(row["板块"])) not in live_sector_keys]
    filtered_leaders = [row for row in leader_payloads if (str(row["trade_date"]), str(row["板块"]), str(row["代码"])) not in live_leader_keys]
    return (
        filtered_sectors,
        filtered_leaders,
        len(sector_payloads) - len(filtered_sectors),
        len(leader_payloads) - len(filtered_leaders),
    )


def bulk_upsert(
    *,
    base_url: str,
    headers: dict[str, str],
    table_name: str,
    payloads: list[dict[str, Any]],
    on_conflict: str,
) -> int:
    """批量 upsert；幂等写入，重复跑不会堆积。"""

    if not payloads:
        return 0
    rest_url = f"{base_url}/rest/v1/{table_name}"
    written = 0
    upsert_headers = {**headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
    for batch in chunked(payloads):
        response = requests.post(
            rest_url,
            headers=upsert_headers,
            params={"on_conflict": on_conflict},
            json=batch,
            timeout=SUPABASE_TIMEOUT_SECONDS,
        )
        if not response.ok:
            raise RuntimeError(f"写入 {table_name} 失败：{response.text[:500]}")
        written += len(batch)
        print(f"{table_name} 已 upsert {written}/{len(payloads)} 行...")
    return written


def write_to_supabase(sector_payloads: list[dict[str, Any]], leader_payloads: list[dict[str, Any]]) -> BackfillResult:
    """正式写入 Supabase，live 行优先，backfill 只填空缺或覆盖 backfill。"""

    url, key = supabase_config()
    headers = supabase_headers(key)
    dates = sorted({str(row["trade_date"]) for row in sector_payloads + leader_payloads})
    sector_payloads, leader_payloads, skipped_sector, skipped_leader = filter_out_live_rows(
        sector_payloads,
        leader_payloads,
        base_url=url,
        headers=headers,
        dates=dates,
    )

    sector_count = bulk_upsert(
        base_url=url,
        headers=headers,
        table_name="sector_snapshots",
        payloads=sector_payloads,
        on_conflict="trade_date,板块",
    )
    leader_count = bulk_upsert(
        base_url=url,
        headers=headers,
        table_name="sector_leader_snapshots",
        payloads=leader_payloads,
        on_conflict="trade_date,板块,代码",
    )

    return BackfillResult(
        sector_upserts=sector_count,
        leader_upserts=leader_count,
        skipped_live_sector_rows=skipped_sector,
        skipped_live_leader_rows=skipped_leader,
        earliest_trade_date=min(dates) if dates else "",
        latest_trade_date=max(dates) if dates else "",
    )


def pick_sample_dates(dates: list[pd.Timestamp], count: int = 3) -> list[pd.Timestamp]:
    """从 N 个交易日里挑样本日期：靠前、中间、最近，方便人工检查。"""

    if len(dates) <= count:
        return dates
    indexes = sorted({0, len(dates) // 2, len(dates) - 1})
    return [dates[index] for index in indexes[:count]]


def print_table(title: str, frame: pd.DataFrame, columns: list[str]) -> None:
    """控制台打印简洁样本表。"""

    print(f"\n{title}")
    if frame.empty:
        print("  暂无可用数据")
        return

    display = frame[columns].copy()
    for column in ["涨跌幅%", "RVOL", "热度分"]:
        if column in display.columns:
            display[column] = display[column].map(lambda value: f"{float(value):.2f}")
    if "成交量" in display.columns:
        display["成交量"] = display["成交量"].map(lambda value: f"{float(value):.0f}")
    print(display.to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="重建板块雷达历史快照；默认只预览，--write-supabase 才写库。")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="回填候选交易日数量，默认 504。")
    parser.add_argument("--sample-count", type=int, default=3, help="预览几个样本交易日，默认 3。")
    parser.add_argument("--period", default="3y", help="yfinance 下载窗口，默认 3y，覆盖 504 日 + 20 日均量。")
    parser.add_argument("--include-today", action="store_true", help="默认排除今天，打开后才允许使用当天未完成日线。")
    parser.add_argument("--write-supabase", action="store_true", help="正式写入 Supabase；未传入时只打印样本。")
    args = parser.parse_args()

    tickers = collect_tickers()
    print("板块历史快照回填脚本")
    print(f"配置：交易日数量 N={args.days}，样本天数={args.sample_count}，ticker 数={len(tickers)}，top_sector_count={DEFAULT_TOP_SECTOR_COUNT}")
    print("防 look-ahead：每个 D 日只使用 <=D 的历史切片；RVOL 均量用 D 日之前 20 根成交量，不含 D 日。")
    print("日期对齐：每条 trade_date 来自该 ticker 在 yfinance 日线中的真实索引日期；默认排除今天未完成日线。")
    print("幸存者偏差：当前板块成分股来自 src.config.SECTOR_RADAR_CONFIG 的今日写死列表，历史回填存在幸存者偏差。")
    print("写库保护：source='live' 的已有前向行优先，backfill 不会覆盖 live；重复运行通过 upsert 幂等覆盖 backfill。")

    if args.write_supabase:
        supabase_config()
        print("Supabase 配置已检测到；后续日志不会打印密钥。")

    history = download_history(tickers, period=args.period)
    if history.empty:
        print("未下载到 yfinance 历史数据，请稍后重试。")
        return 1

    history_by_ticker = {ticker: ticker_frame(history, ticker) for ticker in tickers}
    dates = available_trade_dates(history, args.days, include_today=args.include_today)
    if not dates:
        print("可用交易日不足，无法生成回填数据。")
        return 1

    sample_dates = pick_sample_dates(dates, args.sample_count)
    print("样本 trade_date：", ", ".join(pd.Timestamp(day).date().isoformat() for day in sample_dates))
    for sample_date in sample_dates:
        sectors_df, leaders_df = build_snapshot_for_date(history_by_ticker, sample_date)
        label = pd.Timestamp(sample_date).date().isoformat()
        print_table(
            f"{label} 板块快照 sector_snapshots 样本",
            sectors_df.head(10),
            ["trade_date", "板块", "代表ETF", "涨跌幅%", "RVOL", "热度分", "热度排名", "source"],
        )
        print_table(
            f"{label} 龙头快照 sector_leader_snapshots 样本",
            leaders_df.head(12),
            ["trade_date", "板块", "代码", "涨跌幅%", "成交量", "RVOL", "source"],
        )

    sector_payloads, leader_payloads = build_backfill_payloads(history_by_ticker, dates)
    all_dates = sorted({str(row["trade_date"]) for row in sector_payloads + leader_payloads})
    print(f"\n待处理日期范围：{all_dates[0]} ~ {all_dates[-1]}，共 {len(all_dates)} 个交易日。")
    print(f"待生成 payload：sector_snapshots {len(sector_payloads)} 行；sector_leader_snapshots {len(leader_payloads)} 行。")

    if not args.write_supabase:
        print("预览模式完成：未写入 Supabase。正式灌库请加 --write-supabase。")
        return 0

    result = write_to_supabase(sector_payloads, leader_payloads)
    print("\nSupabase 灌库完成")
    print(f"sector_snapshots upsert 行数：{result.sector_upserts}")
    print(f"sector_leader_snapshots upsert 行数：{result.leader_upserts}")
    print(f"trade_date 范围：{result.earliest_trade_date} ~ {result.latest_trade_date}")
    print(f"跳过已有 live 行：sector_snapshots {result.skipped_live_sector_rows} 行；sector_leader_snapshots {result.skipped_live_leader_rows} 行。")
    print("本次没有跑回测，没有改回测逻辑，没有改选股/打分/总闸/现金闸。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
