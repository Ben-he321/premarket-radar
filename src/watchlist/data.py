"""Per-security identity-aware immutable annual cache, isolated failures and audit."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import uuid
import time
import pandas as pd
import duckdb
from filelock import FileLock

from src.data.alpaca_client import AlpacaMarketData, DataAccessError, normalize_bars
from src.data.alpaca_config import load_config
from src.data.alpaca_calendar import finalized_day, sessions
from src.data.alpaca_quality import inspect_quality
from .runtime import root, read, write, utc, digest, state, event

SCHEMA = 2


def location(record, adj):
    return root() / "datasets" / record["symbol"] / record["identity_version"] / adj


def manifest(record, adj):
    m=read(location(record, adj) / "manifest.json", {"schema": SCHEMA, "symbol": record["symbol"], "identity_version": record["identity_version"], "adjustment": adj, "chunks": {}})
    if (m['schema'],m['identity_version'],m['adjustment'])!=(SCHEMA,record['identity_version'],adj):
        raise ValueError('CACHE_VERSION_MISMATCH')
    return m


def load(record, adj="all"):
    if adj=='all' and read(root()/'revision_pending'/(record['symbol']+'.json'),{}).get('status')=='REBUILDING_ALL':
        raise ValueError('ADJUSTMENT_REBUILD_PENDING:'+record['symbol'])
    m = manifest(record, adj)
    paths = [str(root() / c["path"]) for c in m["chunks"].values() if c["status"] == "OK"]
    if not paths:
        return normalize_bars({}, ())
    with duckdb.connect(":memory:") as db:
        return db.read_parquet(paths, union_by_name=True).order("trade_date").df()


def periods(record, end):
    for segment in record.get("segments", [{"symbol":record["symbol"], "start":record["research_start"], "end":str(end)}]):
        start = max(date(2016, 1, 1), date.fromisoformat(segment["start"]))
        finish = min(end, date.fromisoformat(segment["end"]))
        for year in range(start.year, finish.year + 1):
            a, b = max(start, date(year, 1, 1)), min(finish, date(year, 12, 31))
            if a <= b:
                yield f"{year}-{segment['symbol']}", segment["symbol"], a, b


def plan(universe, end, incremental=False, force_symbols=()):
    jobs = []
    for rec in universe["records"] + universe["references"]:
        if rec["status"] not in ("INCLUDED", "REFERENCE_ONLY", "INSUFFICIENT_HISTORY", "DATA_UNAVAILABLE"):
            continue
        for adj in ("raw", "all"):
            m = manifest(rec, adj)
            for key, source, start, finish in periods(rec, end):
                cached = m["chunks"].get(key)
                refresh = rec["symbol"] in force_symbols
                if cached and cached["status"] == "OK" and not refresh:
                    if cached["end"] == str(finish):
                        if not incremental or finish < end:
                            continue
                    last = date.fromisoformat(cached["end"])
                    days = sessions(last - timedelta(days=30), last)
                    start = max(start, days[-5] if len(days) >= 5 else start)
                signature = digest({"schema":SCHEMA, "universe_version":universe["universe_version"], "mapping_version":universe["mapping_version"],
                                    "symbol":rec["symbol"], "identity":rec["identity_version"], "source":source, "start":str(start), "end":str(finish), "adjustment":adj})
                jobs.append({"record":rec, "adj":adj, "key":key, "source":source, "start":start, "end":finish, "signature":signature})
    return jobs


def _publish(job, frame, universe):
    rec, adj = job["record"], job["adj"]
    frame = frame.copy()
    frame["provider_symbol"] = frame.symbol
    frame["symbol"] = rec["symbol"]
    source_quality = inspect_quality(frame, (rec["symbol"],), job["start"], job["end"])
    bad_dates = {d for issue in source_quality["issues"] if issue["severity"] == "ERROR" for d in issue["dates"]}
    quarantine_path = None
    if bad_dates:
        # Preserve every provider row. Only the explicitly clean subset is queryable;
        # no forward-fill, zero-volume fabrication, or whole-year collateral loss.
        quarantine_path = root()/"quarantine"/rec["symbol"]/adj/(uuid.uuid4().hex+".parquet")
        quarantine_path.parent.mkdir(parents=True,exist_ok=True)
        frame.to_parquet(quarantine_path,index=False)
        write(quarantine_path.with_suffix('.json'),{"quality":source_quality,"signature":job['signature'],"at":utc()})
        frame=frame[~frame.trade_date.isin(bad_dates)].copy()
    m = manifest(rec, adj)
    previous = m["chunks"].get(job["key"])
    revision = False
    start = job["start"]
    if previous and previous["status"] == "OK":
        old = pd.read_parquet(root() / previous["path"])
        overlap = old[old.trade_date >= str(start)]
        same = frame[frame.trade_date <= previous["end"]]
        cols = ["trade_date","open","high","low","close","volume"]
        revision = not overlap[cols].reset_index(drop=True).equals(same[cols].reset_index(drop=True))
        frame = pd.concat([old[old.trade_date < str(start)], frame], ignore_index=True)
        start = date.fromisoformat(previous["start"])
    quality = inspect_quality(frame, (rec["symbol"],), start, job["end"])
    status = "OK" if quality["passed_numeric_checks"] else "QUALITY_QUARANTINE"
    obj = location(rec, adj) / "objects" / (uuid.uuid4().hex + ".parquet")
    obj.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(obj, index=False)
    entry = {"path":obj.relative_to(root()).as_posix(), "start":str(start), "end":str(job["end"]), "rows":len(frame),
             "status":status, "quality":quality, "retrieved_at":utc(), "signature":job["signature"], "sha256":hashlib.sha256(obj.read_bytes()).hexdigest(),
             "source":"Alpaca", "feed":"sip", "asof":"-", "source_symbol":job["source"], "adjustment":adj,
             "request_start":str(job["start"]), "request_end":str(job["end"]), "revision_detected":revision}
    entry.update(source_quality=source_quality,quarantined_dates=sorted(bad_dates),quarantine_path=str(quarantine_path) if quarantine_path else None)
    write(obj.with_suffix(".json"), entry)
    if status == "OK":
        if revision and adj=='all':
            write(root()/"revision_pending"/(rec['symbol']+".json"),{"status":"REBUILDING_ALL","at":utc()})
        m["chunks"][job["key"]] = entry
        m.update(version=digest(m["chunks"]), universe_version=universe["universe_version"], mapping_version=universe["mapping_version"], updated_at=utc())
        write(location(rec, adj) / "manifest.json", m)
    else:
        write(location(rec, adj) / "quarantine.json", entry)
    event("sync:"+rec["symbol"]+":"+adj, status, job["signature"], {"rows":len(frame),"end":str(job["end"]),"revision":revision})
    return status, revision


def sync(universe, incremental=False, deadline=None, client=None):
    client = client or AlpacaMarketData(load_config())
    end = finalized_day()
    # Also enforce Basic's >=20 minute historical lag explicitly.
    if datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) > datetime.now(timezone.utc)-timedelta(minutes=20):
        raise DataAccessError("BASIC_END_TOO_RECENT")
    with FileLock(str(root()/"sync.lock"), timeout=0):
        checks = {kind:client.probe(kind,end) for kind in ("historical","latest")}
        if checks["latest"]["status"] == "SIP_PERMISSION_DENIED":
            checks["latest"]["status"] = "NOT_REQUIRED_BASIC"
        write(root()/"sip_checks.json", checks)
        if checks["historical"]["status"] != "ACCESS_OK":
            state("WAITING_FOR_DATA_ACCESS",checks=checks)
            return False
        client.asof = "-"  # explicit alias segments, never automatic ticker stitching
        jobs = plan(universe,end,incremental)
        groups = {}
        for job in jobs:
            groups.setdefault((job["adj"],job["start"],job["end"]),[]).append(job)
        completed, failures = 0, []
        revisions={p.stem for p in (root()/'revision_pending').glob('*.json') if read(p,{}).get('status')=='REBUILDING_ALL'}

        def execute(batch):
            nonlocal completed
            if deadline and time.time() >= deadline:
                raise TimeoutError("RESOURCE_DEADLINE")
            symbols = tuple(dict.fromkeys(j["source"] for j in batch))
            j = batch[0]
            try:
                frame = client.bars(symbols,j["start"],j["end"],j["adj"])
            except DataAccessError as exc:
                if len(batch)>1:
                    mid=len(batch)//2
                    execute(batch[:mid]);execute(batch[mid:])
                else:
                    failures.append({"symbol":j["record"]["symbol"],"adjustment":j["adj"],"start":str(j["start"]),"end":str(j["end"]),"error":exc.code})
                    event("sync:"+j["record"]["symbol"],"FAILED",j["signature"],error=exc.code)
                return
            for job in batch:
                result, changed = _publish(job,frame[frame.symbol==job["source"]],universe)
                if result != "OK": failures.append({"symbol":job["record"]["symbol"],"adjustment":job["adj"],"error":result})
                if changed and job["adj"] == "all": revisions.add(job["record"]["symbol"])
                completed += 1
            state("SYNC_RUNNING",completed_chunks=completed,total_chunks=len(jobs),current=str(j["end"]),errors=len(failures))

        try:
            for group in groups.values():
                for offset in range(0,len(group),6):
                    execute(group[offset:offset+6])
            # all revisions need a coherent complete replacement per affected security.
            # Mark as quarantined from research until every requested all chunk is refreshed.
            for symbol in sorted(revisions):
                write(root()/"revision_pending"/(symbol+".json"),{"status":"REBUILDING_ALL","at":utc()})
                refresh_jobs=[j for j in plan(universe,end,force_symbols=(symbol,)) if j["record"]["symbol"]==symbol and j["adj"]=="all"]
                before=len(failures)
                for job in refresh_jobs: execute([job])
                if len(failures)==before:
                    write(root()/"revision_pending"/(symbol+".json"),{"status":"RECONCILED","at":utc()})
        except TimeoutError:
            write(root()/"sync_errors.json",failures)
            state("RESOURCE_LIMIT_CHECKPOINT",completed_chunks=completed,total_chunks=len(jobs))
            return False
        write(root()/"sync_errors.json",failures)
        write(root()/"sync_last.json",{"at":utc(),"end":str(end),"jobs":len(jobs),"completed":completed,"failures":failures,"revisions":sorted(revisions),"incremental":incremental})
        coverage(universe)
        return True


def coverage(universe):
    output=[]
    for rec in universe["records"]+universe["references"]:
        for adj in ("raw","all"):
            m=manifest(rec,adj); frame=load(rec,adj)
            end=finalized_day(); start=date.fromisoformat(rec["research_start"])
            issues=inspect_quality(frame,(rec["symbol"],),start,end)
            expected=list(periods(rec,end))
            complete=all(k in m["chunks"] and m["chunks"][k]["end"]==str(b) for k,_,_,b in expected)
            status=rec["status"]
            if status=="INCLUDED":
                status="DATA_UNAVAILABLE" if frame.empty else "INSUFFICIENT_HISTORY" if len(frame)<378 else "INCLUDED"
            output.append({"symbol":rec["symbol"],"type":rec["type"],"adjustment":adj,"status":status,
                           "rows":len(frame),"first":None if frame.empty else frame.trade_date.min(),"last":None if frame.empty else frame.trade_date.max(),
                           "complete_requests":complete,"duplicates":int(frame.duplicated(["symbol","trade_date"]).sum()),
                           "quality":issues,"version":m.get("version"),"identity_version":rec["identity_version"],"company_actions":"UNKNOWN"})
    write(root()/"coverage.json",output)
    return output
