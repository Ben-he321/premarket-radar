"""Original twenty Q1 samples, kept as separate engineering diagnostics.

Q0 is reused from immutable B1.1 final_v4, never silently recomputed here.
Each Q1 sample has its original single entry date, original A/B earnings
evidence and its own 5500 dollars. Their cash or returns must not be combined
into a portfolio. Supplemental RTH quotes are requested only after a natural
position actually exists; missing exits retain positions and unknown marks.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import gc
import json
import os
from pathlib import Path
import traceback

import pandas as pd

from src.ben_b1.b11_research import Inputs, digest, event
from src.ben_b1.replay import ReplayEngine as LegacyReplayEngine
from .replay import ReplayConfig, Q1_HYPOTHESIS, calendar_events
from .compact import CompactReplayEngine as ReplayEngine
from .runtime import ROOT, B11, REPO, TAIL_END, read, write, sha, utc, deadline

NY = "America/New_York"
SCOPE = "FIXED_ORIGINAL20_INDEPENDENT_ENGINEERING_SAMPLE_NOT_SHARED_PORTFOLIO"


def quote_events(symbol, frame, receipt):
    """Translate real vendor L1, retaining current and unknown old receipts."""
    result = []
    for quote in frame.to_dict("records"):
        timestamp = pd.Timestamp(quote["t"])
        payload = {"bid": float(quote["bp"]), "ask": float(quote["ap"]),
            "bid_size": float(quote["bs"]), "ask_size": float(quote["as"]),
            "bid_exchange": quote.get("bx"), "ask_exchange": quote.get("ax"),
            "conditions": list(quote.get("c", [])), "timestamp": timestamp.isoformat(),
            "size_unit": quote.get("size_unit", "UNKNOWN"), "available_at": timestamp.isoformat(),
            "source_received_at": quote.get("source_received_at", receipt.get("last_source_received_at", "UNKNOWN")),
            "historical_network_received_at": "UNKNOWN",
            "availability_basis": "HISTORICAL_EVENT_CLOCK_NOT_LIVE_BASIC_RECEIPT"}
        identity = LegacyReplayEngine._quote_key(None, symbol, payload)
        result.append(event("QUOTE", symbol, timestamp, payload, "b12:holding:"+symbol+":"+identity))
    return result


class HoldingQuoteProvider:
    """A separate new cache namespace; old caches are only reused read-only."""
    def __init__(self, market=None):
        if market is None:
            from .data import B12Market
            market = B12Market(ROOT/"data"/"sample_quotes")
        self.market = market

    def __call__(self, symbol, day):
        frame, receipt = self.market.holding_quotes(symbol, day)
        path = Path(receipt["path"])/"data.parquet"
        return {"events": quote_events(symbol, frame, receipt),
            "source_files": {str(path): sha(path)} if path.exists() else {},
            "complete": bool(receipt.get("complete")), "receipt": receipt,
            "reason": None if receipt.get("complete") else "HOLDING_QUOTE_RESPONSE_INCOMPLETE"}


class PinnedHoldingQuoteProvider:
    """Replay exactly a previous run's held-input receipts with no API calls."""
    def __init__(self, manifest):
        self.manifest = Path(manifest)
        self.requests = read(self.manifest)
        self.pinned = read(self.manifest.parent/"INPUT_HASHES.json")["files"]

    def __call__(self, symbol, day):
        item = next((row for row in self.requests if row["symbol"] == symbol and row["day"] == day), None)
        if item is None or not item.get("complete"):
            return {"events": [], "complete": False, "reason": "PINNED_PRIOR_HOLDING_INPUT_MISSING", "source_files": {}}
        receipt = item["receipt"]
        path = Path(receipt["path"])/"data.parquet"
        actual = sha(path)
        if self.pinned.get(str(path)) != actual:
            raise ValueError("PINNED_PRIOR_HOLDING_QUOTE_HASH_CHANGED")
        frame = pd.read_parquet(path)
        stamp = pd.to_datetime(frame.t, utc=True)
        frame = frame[stamp.dt.tz_convert(NY).dt.strftime("%Y-%m-%d").eq(day)]
        return {"events": quote_events(symbol, frame, receipt), "complete": True,
            "reason": None, "receipt": receipt, "source_files": {str(path): actual,
                str(self.manifest): sha(self.manifest),
                str(self.manifest.parent/"INPUT_HASHES.json"): sha(self.manifest.parent/"INPUT_HASHES.json")}}


def deduplicate_events(events):
    """Prefer the first immutable source receipt for overlapping vendor quotes."""
    output, seen_quotes, seen_ids = [], set(), {}
    for item in events:
        if item["kind"] == "QUOTE":
            payload = dict(item["payload"])
            payload["timestamp"] = pd.Timestamp(payload.get("timestamp", item["at"])).isoformat()
            key = LegacyReplayEngine._quote_key(None, item["symbol"], payload)
            if key in seen_quotes:
                continue
            seen_quotes.add(key)
        identity = item["event_id"]
        fingerprint = digest(item)
        if identity in seen_ids:
            if seen_ids[identity] != fingerprint:
                raise ValueError("ADAPTER_EVENT_ID_CONTENT_CONFLICT")
            continue
        seen_ids[identity] = fingerprint
        output.append(item)
    return output


def _verify_files(files):
    for path, expected in files.items():
        if not Path(path).is_file() or sha(path) != expected:
            raise ValueError("PINNED_SAMPLE_SOURCE_CHANGED:"+str(path))


def _sample_quote_coverage(inputs, sample, quotes):
    day, symbol = sample["trade_date"], sample["symbol"]
    close = inputs.clocks[day].market_close
    fixed, end = close+pd.Timedelta(minutes=5), close+pd.Timedelta(minutes=15)
    rows = quotes.get(day, [])
    before = [q for q in rows if fixed-pd.Timedelta(seconds=5) <= pd.Timestamp(q["at"]) <= fixed]
    window = [q for q in rows if fixed <= pd.Timestamp(q["at"]) < end]
    return {"symbol": symbol, "sample_date": day, "fixed_time": fixed.isoformat(),
        "window_end_exclusive": end.isoformat(), "fixed_point_quote_records_age_le5s": len(before),
        "window_new_quote_records": len(window),
        "first_window_quote_time": min((q["at"] for q in window), key=pd.Timestamp) if window else None,
        "classification": "FIXED_POINT_NO_QUOTE_WINDOW_HAS_QUOTE" if not before and window else
            "NO_WINDOW_QUOTE" if not window else "FIXED_POINT_AND_WINDOW_HAVE_QUOTES",
        "quote_coverage_is_not_trade_qualification": True}


def run_sample(inputs, rank, tier, version="frozen_q1_v1", day_quote_provider=None, output_root=None):
    deadline()
    if tier not in ("A", "B") or rank not in range(1, 21):
        raise ValueError("ORIGINAL_TWENTY_AND_ORIGINAL_A_B_ONLY")
    sample = inputs.first[rank-1]
    symbol, first, last = sample["symbol"], sample["trade_date"], TAIL_END
    name = f"sample_{rank:02d}_{symbol}_{first}_{tier}_{version}"
    output = (Path(output_root) if output_root else ROOT/"samples")/version/f"tier_{tier}"/name
    output.mkdir(parents=True, exist_ok=True)
    cfg = ReplayConfig(run_id=name, quote_mode="Q1", earnings_tier=tier,
        data_basis="VENDOR_STANDARD_FIELDS", start=first, end=last, checkpoint_every=0,
        entry_dates=(first,), research_scope=SCOPE)
    universe = {symbol: {"security_id": sample["local_security_id"],
        "scope": "KEEP" if sample["scope_policy"] == "KEEP" else "EXCLUDE",
        "identity_sort_hash": sample["identity_sort_hash"]}}
    daily, minutes, quotes = inputs.symbol_data(symbol)
    static_files = dict(inputs.pinned)
    spec = {"config": asdict(cfg), "sample": sample, "entry_date": first,
        "exit_only_tail_end": last, "original_earnings_no_b12_enrichment": True,
        "source_files": static_files, "code_sha256": {
            str(path): sha(path) for path in [Path(__file__), REPO/"src/ben_b1_2/replay.py",
                REPO/"src/ben_b1_2/compact.py",
                REPO/"src/ben_b1/replay.py", REPO/"src/ben_b1/ledger.py", REPO/"src/ben_b1/b11_research.py"]}}
    spec_path, pin_path = output/"RUN_SPEC.json", output/"INPUT_HASHES.json"
    if spec_path.exists():
        old = read(spec_path)
        if old["code_sha256"] != spec["code_sha256"] or old["config"] != json.loads(json.dumps(spec["config"])):
            raise ValueError("IMMUTABLE_SAMPLE_CODE_OR_CONFIG_CHANGED_USE_NEW_VERSION")
        _verify_files(old["source_files"])
    else:
        write(spec_path, spec)
    source_files = dict(static_files)
    if pin_path.exists():
        pinned = read(pin_path)["files"]
        _verify_files(pinned)
        source_files.update(pinned)
    if (output/"summary.json").exists() and read(output/"summary.json").get("sample_run_complete"):
        return read(output/"summary.json")
    write(output/"WINDOW_QUOTE_COVERAGE.json", _sample_quote_coverage(inputs, sample, quotes))
    checkpoint = output/"checkpoint.json"
    engine = ReplayEngine.restore(checkpoint, inputs.schedule) if checkpoint.exists() else ReplayEngine(inputs.schedule, cfg, universe, checkpoint)
    extras = inputs.earnings_events(symbol, tier, first)+inputs.corporate_events(symbol, first, last)
    start = pd.Timestamp(first, tz=NY)
    if not checkpoint.exists():
        warmup = [inputs.daily_event(symbol, row) for row in daily[daily.trade_date < first].to_dict("records")] if len(daily) else []
        engine.run(warmup+[item for item in extras if pd.Timestamp(item["at"]) < start])
    extras_by_day = {}
    for item in extras:
        extras_by_day.setdefault(str(pd.Timestamp(item["at"]).tz_convert(NY).date()), []).append(item)
    progress_path = output/"progress.json"
    completed = read(progress_path).get("processed_through") if progress_path.exists() else None
    resumeday = str((pd.Timestamp(completed)+pd.Timedelta(days=1)).date()) if completed else first
    stop_reason, events, processed_day = "FIXED_TAIL_END_STATE_RETAINED", [], completed
    provider_log = read(output/"HOLDING_QUOTE_REQUESTS.json") if (output/"HOLDING_QUOTE_REQUESTS.json").exists() else []
    for stamp in pd.date_range(resumeday, last):
        deadline()
        day = str(stamp.date())
        events = calendar_events(inputs.schedule, day, day)+extras_by_day.get(day, [])
        events += inputs.day_events(symbol, day, daily, minutes, quotes)
        # Acquire by actual entering state. No future price is read to decide
        # whether the already-created campaign should have been entered.
        if symbol in engine.ledger.positions and day in inputs.clocks:
            if day_quote_provider is not None:
                try:
                    supplied = day_quote_provider(symbol, day)
                    if isinstance(supplied, list):
                        supplied = {"events": supplied, "complete": False, "reason": "PROVIDER_COVERAGE_NOT_DECLARED"}
                    events += supplied.get("events", [])
                    source_files.update(supplied.get("source_files", {}))
                    provider_log.append({"symbol": symbol, "day": day, "requested_at": utc(),
                        "basis": "POSITION_ACTUALLY_EXISTS_AT_SESSION_START", "rows": len(supplied.get("events", [])),
                        "complete": bool(supplied.get("complete")), "reason": supplied.get("reason"),
                        "receipt": supplied.get("receipt")})
                    if not supplied.get("complete"):
                        events.append(event("DATA_GAP", symbol, inputs.clocks[day].market_open,
                            {"reason": supplied.get("reason") or "HOLDING_QUOTES_COVERAGE_INCOMPLETE", "category": "MARKET_DATA"}))
                except Exception as exc:
                    provider_log.append({"symbol": symbol, "day": day, "requested_at": utc(),
                        "complete": False, "reason": "HOLDING_QUOTE_PROVIDER_FAILED", "exception": type(exc).__name__})
                    events.append(event("DATA_GAP", symbol, inputs.clocks[day].market_open,
                        {"reason": "HOLDING_QUOTE_PROVIDER_FAILED", "category": "MARKET_DATA"}))
            else:
                engine.mark_coverage_limited("SUPPLEMENTAL_HOLDING_QUOTES_NOT_REQUESTED_OLD_TARGETED_CACHE_ONLY", symbol, day, day)
        events = deduplicate_events(events)
        engine.run(events)
        processed_day = day
        write(pin_path, {"created_at": utc(), "files": source_files})
        write(output/"HOLDING_QUOTE_REQUESTS.json", provider_log)
        write(progress_path, {"at": utc(), "sample": rank, "tier": tier, "processed_through": day,
            "events_processed": engine.state["events_processed"], "open_positions": list(engine.ledger.positions),
            "cash": engine.ledger.cash, "errors": len(engine.state["errors"])})
        if engine.state["errors"]:
            stop_reason = "ENGINE_ERROR_STATE_RETAINED_NOT_ZERO_TRADE_SUCCESS"
            break
        unpaid = any(value["status"] != "PAID" and value["amount"] > 0 for value in engine.ledger.dividend_receivables.values())
        if not engine.ledger.positions and not engine.ledger.settlements and not unpaid and not any(order["status"] == "OPEN" for order in engine.orders.values()):
            stop_reason = "NATURAL_CAMPAIGN_SETTLED" if engine.ledger.fills else "ENTRY_WINDOW_EXPIRED_WITHOUT_NATURAL_FILL"
            break
    engine.export(output)
    summary = engine.summary()
    q0_path = B11/"replay/final_v4"/f"tier_{tier}"/f"sample_{rank:02d}_{symbol}_{first}_{tier}_final_v4"/"summary.json"
    summary.update(sample_rank=rank, symbol=symbol, sample_date=first, processed_through=processed_day,
        stop_reason=stop_reason, output=str(output), sample_run_complete=True,
        full_pool_account=False, independent_5500_engineering_only=True,
        not_strategy_performance_validation=True, execution_hypothesis=Q1_HYPOTHESIS,
        original_q0_result={"path": str(q0_path), "sha256": sha(q0_path)} if q0_path.exists() else {"status": "Q0_RESULT_MISSING"},
        original_earnings_version=digest(inputs.earnings), source_files=source_files)
    write(output/"summary.json", summary)
    restored = ReplayEngine.restore(output/"checkpoint.json", inputs.schedule)
    before = restored.state_digest()
    restored.run(events)
    after = restored.state_digest()
    write(output/"RECOVERY_IDEMPOTENCY.json", {"tested_at": utc(), "before": before, "after": after,
        "same_state": before == after, "last_complete_day_events_replayed": len(events),
        "status": "PASS" if before == after else "FAIL", "synthetic": False,
        "legacy_results_and_inputs_read_only": True})
    restored.close()
    engine.close()
    print(json.dumps({key: summary.get(key) for key in ("sample_rank", "symbol", "earnings_tier", "status", "buy_fills", "sell_fills", "error_count", "processed_through")}), flush=True)
    return summary


def run(tiers=("A", "B"), version="frozen_q1_v1", ranks=None, day_quote_provider=None, output_root=None):
    inputs, results = Inputs(), []
    root = Path(output_root) if output_root else ROOT/"samples"
    for tier in tiers:
        tier_results = []
        for rank in ranks or range(1, 21):
            write(root/"WORKER_STATE.json", {"stage": "Q1_ORIGINAL20_REAL_REPLAY", "pid": os.getpid(),
                "started_or_resumed_at": utc(), "status": "RUNNING", "tier": tier, "sample_rank": rank,
                "persistent_ben_forward_service": False})
            try:
                result = run_sample(inputs, rank, tier, version, day_quote_provider, root)
            except Exception as exc:
                result = {"sample_rank": rank, "earnings_tier": tier, "status": "REPLAY_ADAPTER_FAILED",
                    "reason": str(exc), "exception": type(exc).__name__, "actually_completed": False}
                write(root/version/f"tier_{tier}"/f"adapter_error_{rank:02d}.json", {
                    **result, "recorded_at": utc(), "traceback": traceback.format_exc()})
                print(json.dumps(result), flush=True)
            results.append(result)
            tier_results.append(result)
            write(root/version/f"tier_{tier}"/"RUN_SUMMARY.json", tier_results)
            # Input objects remain immutable on disk. Release bulky per-symbol
            # frames between independent samples rather than retaining all15.
            inputs.cache.clear()
            gc.collect()
    write(root/version/"RUN_SUMMARY.json", results)
    write(root/"WORKER_STATE.json", {"stage": "Q1_ORIGINAL20_REAL_REPLAY", "pid": os.getpid(),
        "finished_at": utc(), "status": "FINISHED", "samples": len(results),
        "persistent_research_worker_running": False, "persistent_ben_forward_service": False})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tiers", nargs="+", choices=("A", "B"), default=("A", "B"))
    parser.add_argument("--version", default="frozen_q1_v1")
    parser.add_argument("--ranks", nargs="+", type=int)
    parser.add_argument("--supplement-held-quotes", action="store_true")
    parser.add_argument("--cached-holding-manifest")
    args = parser.parse_args()
    if args.cached_holding_manifest and args.supplement_held_quotes:
        parser.error("Choose prior pinned receipts or real supplemental acquisition, not both")
    provider = PinnedHoldingQuoteProvider(args.cached_holding_manifest) if args.cached_holding_manifest else HoldingQuoteProvider() if args.supplement_held_quotes else None
    run(args.tiers, args.version, args.ranks, provider)
