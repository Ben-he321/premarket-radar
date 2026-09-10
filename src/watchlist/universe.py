"""Official exchange directory classification; unknown histories remain explicit."""
import csv
import io
import requests
from src.data.alpaca_config import PROJECT_ROOT
from .runtime import root, write, read, digest, utc, universe_config, event

SOURCES = {
    "NASDAQ": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "OTHER": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
}


def classify(symbol, listing, override=None, special=False):
    override = override or {}
    name = listing.get("Security Name", "UNKNOWN") if listing else "UNKNOWN"
    lower = name.lower()
    record = {"symbol": symbol, "name": name, "exchange": listing.get("exchange", "UNKNOWN") if listing else "UNKNOWN",
              "currency": "USD", "currency_basis": "US listing / Alpaca stock bars USD request",
              "security_id": "UNKNOWN", "type": "UNKNOWN", "status": "IDENTITY_UNRESOLVED",
              "reason": "Not found in official current exchange directory", "observed_at": utc(),
              "listing_date": "UNKNOWN", "corporate_actions": "UNKNOWN",
              "historical_mapping": "UNKNOWN", "research_start": "2016-01-01",
              "sources": [listing["source"]] if listing else []}
    if listing:
        if listing.get("ETF") == "Y" or any(x in lower for x in ("warrant", " units", "preferred", " etn")):
            record.update(type="ETF_OR_EXCLUDED_SECURITY", status="EXCLUDED_BY_TYPE", reason="Official ETF flag or non-common security name")
        elif symbol == "DXYZ":
            record.update(type="CEF", status="INCLUDED", reason="Separate closed-end fund research category, not an operating-company model")
        elif any(x in lower for x in ("common", "ordinary", "depositary", "depositary shares")):
            record.update(type="ADR_ADS" if "depositary" in lower else "COMMON", status="INCLUDED",
                          reason="Official exchange security name and ETF=N")
        if special and record["status"] == "INCLUDED" and not override.get("research_start"):
            record.update(status="IDENTITY_UNRESOLVED", reason="Current listing identified; reused/special ticker historical boundary requires issuer evidence")
    if override:
        record.update({k: v for k, v in override.items() if k != "sources"})
        record["sources"] += override.get("sources", [])
    record["identity_version"] = digest({k: record[k] for k in ("symbol", "name", "type", "research_start", "historical_mapping", "sources")})[:16]
    return record


def validate(refresh=False):
    config = universe_config()
    overrides = read(PROJECT_ROOT / "config/identity_evidence.json", {})
    index, failures = {}, {}
    for market, url in SOURCES.items():
        cache = root() / "metadata" / f"{market}.txt"
        try:
            if refresh or not cache.exists():
                result = requests.get(url, timeout=30)
                result.raise_for_status()
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(result.text, encoding="utf-8")
            for row in csv.DictReader(io.StringIO(cache.read_text(encoding="utf-8")), delimiter="|"):
                symbol = row.get("Symbol") or row.get("ACT Symbol")
                if not symbol or row.get("Test Issue") == "Y":
                    continue
                row["source"] = url
                row["exchange"] = "NASDAQ" if market == "NASDAQ" else {"N":"NYSE", "A":"NYSE American", "P":"NYSE Arca", "Z":"Cboe", "V":"IEX"}.get(row.get("Exchange"), "UNKNOWN")
                index[symbol] = row
        except requests.RequestException:
            failures[market] = "DIRECTORY_REQUEST_FAILED"
    records = [classify(s, index.get(s), overrides.get(s), s in config["special_identity_review"])
               for s in config["candidates"]]
    refs = [classify(s, index.get(s)) for s in config["references"]]
    for r in refs:
        r.update(status="REFERENCE_ONLY", type="ETF", reason="Benchmark only; no virtual orders")
    result = {"universe_version": digest(config)[:16], "mapping_version": digest([r["identity_version"] for r in records + refs])[:16],
              "verified_at": utc(), "records": records, "references": refs, "source_failures": failures,
              "bias": "Current watchlist retrospective sample; not survivorship-bias-free"}
    write(root() / "universe.json", result)
    event("validate-universe", "COMPLETE", config, {"count": len(records), "failures": failures})
    return result
