# F0 Databento normalization boundary

`src/futures_f0/vendor.py` inspects existing, receipt-backed GLBX.MDP3 CSVs and
prepares reviewable normalization candidates. It makes no API calls, reads no
credentials, buys no data, changes no frozen parameters and starts no research
account. Parsing success is not research qualification. The existing
`QualifiedInputs` gate remains in force; this adapter never writes
`VERIFIED_RAW_TO_NORMALIZED` or a manifest entry with `verified: true`.

## Immediate raw-sample interface

```python
from src.futures_f0.vendor import RawSource, inspect_sources

# Use real download CSV/receipt paths supplied by the acquisition stage.
sources = [
    RawSource.from_receipt(csv_path, receipt_path, price_encoding="fixed_1e9")
    for csv_path, receipt_path in acquired_pairs
]
report = inspect_sources(sources, new_output_directory, guard=guard)
```

The receipt must contain the original canonical `Request.parameters()`, its
hash, source URL, source content hash/byte count, and download start/completion
timestamps. Explicit `csv_format.pretty_px`, when present, must agree with the
requested decoding. Both raw content and receipt hashes appear in the report.
An engineering receipt must carry `mock: true` or `is_mock: true`; test fixtures
live exclusively in pytest temporary directories. Receipt hashes establish
local consistency, not cryptographic proof of vendor origin.

Outputs in a new directory:

| File | Meaning |
| --- | --- |
| `vendor_records.ndjson` | One parsed or quarantined source record per line; exact nanoseconds, raw-record hash, source line and file hash retained. |
| `VENDOR_SAMPLE_COVERAGE.csv` | Per source/publisher/instrument counts, requested/observed symbols, timestamp range, empty responses, parse failures, settlement flags and date hints. |
| `VENDOR_QUALIFICATION.json` | Source receipts and output hashes, explicit blockers, zero research-qualified rows and no futures backtest. |

Files stream sequentially; no full-table dataframe or all-record dictionary is
built. Each source is at most 128 MiB and 31 days under the existing `Request`
limits, with at most 18 instrument IDs per source and at most 128 source objects
per inspection. Only the 13 frozen roots' explicit outright contract symbols
are accepted. Duplicate requests, oversized rows and changed input hashes fail
closed. A failed inspection can leave partial output for review; use a new
directory after resolving the failure. UTC daily bars, root/parent requests,
continuous prices, spreads and options are outside the adapter's scope.

## Documented vendor semantics

The HTTP CSV endpoint defaults `pretty_px` and `pretty_ts` to false; SDK
`DBNStore.to_csv` defaults them to true. Declare the encoding; do not infer a
scale from observed price magnitudes. The HTTP request interval filters
`ts_recv` where the schema has it, otherwise `ts_event`.
[HTTP API](https://databento.com/docs/api-reference-historical?historical=http),
[SDK CSV export](https://databento.com/docs/api-reference-historical/helpers/dbn-store-to-csv).

Raw fixed prices use a 1e-9 scale. `INT64_MAX` is an undefined price; decimal CSV
represents an undefined price with an empty field. The decoder preserves
negative prices, rejects nonfinite values and never replaces unknown volume
with zero. Nanoseconds remain integers; availability is rounded upward only
when adapting to the engine's microsecond datetime representation.
[Price conventions](https://databento.com/docs/standards-and-conventions/common-fields-enums-types).

For OHLCV, `ts_event` is the bucket start, derived from trade capture timestamps.
A no-trade bucket may be absent. It does not prove a missing-data outage or a
zero-volume bar, and the adapter performs no fill. `ohlcv-1d` uses UTC dates;
exchange sessions require finer bars and dated break/holiday windows. Hour
buckets crossing a partial session boundary require the necessary minute data.
OHLCV does not include its historical publication timestamp, so closing time
alone is insufficient publication evidence.
[OHLCV schema](https://databento.com/docs/schemas-and-data-formats/ohlcv).

Definitions are versioned by observed `ts_recv`. Activation and expiration can
have only date precision. `instrument_class=F` and `security_type=FUT` are
checked together with symbol/leg information; FUT alone also describes future
spreads. The decoder retains `display_factor`, tick, quantity, exchange, currency
and security update action. It does not mistake `contract_multiplier` (a
deliverables field) for USD P&L per quote unit or apply `display_factor` a second
time without evidence.
[Definition schema](https://databento.com/docs/schemas-and-data-formats/instrument-definitions).

`statistics.stat_type=3` denotes settlement; close is not settlement. The
decoder retains `ts_recv` (capture time), `ts_event` (event time), `ts_ref`
(reference time), actions, sequence and flags. Missing values remain unknown.
[Statistics schema](https://databento.com/docs/schemas-and-data-formats/statistics).

GLBX daily `ts_ref` encodes CME TradingReferenceDate: read the UTC date label
without timezone conversion. Settlement flag bits are 1 final, 2 actual,
4 trading tick and 8 intraday. Publication times vary; records can revise the
same reference date. `settlement_at` uses only updates visible at its `as_of`,
requires final/actual/end-of-day flags, and invalidates a deletion or a later
nonqualifying replacement. It never backfills a later final value into earlier
accounting. Group status may appear outside an instrument's active lifetime.
[GLBX normalization](https://databento.com/docs/venues-and-datasets/glbx-mdp3).

Status `is_trading` is best-efforts Y/N/unknown. A Y event alone proves neither
complete status coverage nor executable stop prices. Raw status actions and
unknowns are retained; the adapter never infers a tradable session from the
absence of halt messages.
[Status schema](https://databento.com/docs/schemas-and-data-formats/status).

ZC's 5,000 bushels and cents-per-bushel convention imply USD 50 per one cent of
quote change and USD 12.50 per 0.25-cent tick. MZC is a separate frozen root.
The raw-to-registry factor must be reconciled per actual vendor definition;
neither commodity quantity nor a plausible price justifies an assumed factor.
[CME corn specification](https://www.cmegroup.com/trading/agricultural/files/grain-and-oilseed-futures-options-fact-card.pdf).

## Candidate and evidence interfaces

`definition_at(source, contract_id=..., instrument_id=..., publisher_id=...,
as_of=...)` selects a previously observed active version, rejects deletion,
non-outrights and unknown lifetimes, and does not backdate a newly observed
definition to activation. It is still a candidate: dated validity changes,
publisher mapping, listing date and expiration precision require review.

`settlement_at(..., session=..., as_of=..., reference_evidence=EvidenceFile(...))`
requires a local hash-bound JSON evidence object with
`kind: CME_TRADING_REFERENCE_DATE`, `mock: false` and
`scope: {dataset: GLBX.MDP3}`. This should cite the acquired official source and
document its date interpretation. Its result includes raw and rounded capture
times, original flags, revision count and source record hash. It returns
`research_qualified: false`; capture time is not a proof of customer delivery.

`session_candidate(...)` accepts one hourly or minute source, a definition,
`SessionWindow`, publication timestamp, registry row, next session and three
`EvidenceFile(path, sha256, source)` references. Evidence JSON has `kind`,
`mock: false`, a `scope` object and these contents:

| Evidence kind | Required scope/content |
| --- | --- |
| `DATED_EXCHANGE_CALENDAR` | Contract and session; explicit UTC `segments`, verified `next_session`; its file hash must match `SessionWindow.evidence_sha256`. |
| `OHLCV_PUBLICATION` | Contract, session and raw source hash; `available_at` matching the supplied historical publication time. |
| `CONTRACT_PRICE_CONVENTION` | Contract and raw definition-record hash; `vendor_decimal_to_quote_units`, `quote_unit`, USD multiplier, vendor/exchange labels and vendor/normalized currency. |

The candidate checks vendor tick and quantity against the frozen registry;
publication against evidence; session timestamps, activation and expiration;
and actual bucket coverage. It returns `{record, qualification}`. `record`
has existing `SessionBar` field names, status
`UNQUALIFIED_VENDOR_SESSION_CANDIDATE`, false tradability and no substituted
settlement. `qualification` retains bucket diagnostics and unresolved review
items. This is an integration foundation, not an end-to-end qualifying import.

`manifest_candidate_entry(path, manifest_directory=..., source=...)` constructs
the existing relative path/hash/source shape with `verified: false`. It cannot
pass the existing reader. Changing a boolean or integration-review string by
hand is not the missing validation process.

## Evidence still required before research

1. Actual definition versions covering the session, ID/publisher/symbol mapping,
   exchange specifications, activation/listing and expiry precision. A one-day
   raw request or root template cannot establish the full contract lifetime.
2. Dated historical exchange calendars, breaks, holidays and next-session
   mapping. Calendar dates must remain in the frozen 2021 warmup/2022–25 range.
3. Historical OHLCV availability and correction handling. Download receipt time
   is acquisition evidence only.
4. Settlement reference-session mapping, flag interpretation, publication and
   corrections/deletions through the relevant accounting horizon. A final flag
   in one response does not prove that no subsequent correction exists.
5. Status coverage with starting state and the relevant halt/price-limit
   messages. Candlestick prices cannot prove intraday execution liquidity.
6. Specific first-notice, last-trade and broker liquidation dates, or dated
   source evidence that a boundary is inapplicable; then a verified five-session
   safe-exit calculation. Missing first notice/broker dates remain UNKNOWN.
7. Standard/execution contract mapping, causal roll overlap and independent
   raw-to-normalized integration review. Do not scale one product into another.

The small raw coverage report establishes which vendor rows were actually
obtained and decoded. It does not establish trading coverage, historical broker
margins, eligible contracts, strategy results or completion of the full research
matrix. Tests validate engineering behavior only.

## Real-sample reconstruction and later decoder checks

`src/futures_f0/reconstruction.py` provides `CandidateSession` and
`reconstruct_equity_session` for an explicitly bounded ES/MES session. It folds
hourly sources incrementally and independently folds at most 60 minutes of the
last hour. Minute volume is checked against the hour, never added to the daily
volume twice. At most 48 hourly record references are retained per contract.
Missing buckets and mismatched OHLCV remain visible. Outputs always have
`available_at: null`, `settlement_reference_at: null` and
`research_qualified: false`; matching prices do not open the research gate.

The bounds use a hash-bound `DATED_EXCHANGE_SESSION_CANDIDATE` JSON object with
contract/session scope, `timezone: America/Chicago`, and one continuous
hour-aligned pair in `segments`, formatted as UTC ISO strings with nine decimal
digits. This candidate evidence does not use a `verified` boolean. Settlement
remains a separate causal selection including its original record hash.

For the inspected 2024 ES/MES day, continuous previous 17:00 to current 16:00 CT
is supported by CME's June 2021 notice: the former afternoon pause ended from
trade date June 28, 2021. The observed minute trades must not be removed using
the superseded 2020 template.
[CME change notice](https://www.cmegroup.com/notices/electronic-trading/2021/06/20210621.html).

Definition integer nulls now follow the vendor's declared dtypes and defaults:
for example `contract_multiplier` uses `INT32_MAX`, while fractional display
fields use `UINT8_MAX`. Decoded nulls become `None`; `definition_integer_raw`
retains original strings and zero leg counts remain zero. Activation is labelled
as the instrument-enabled timestamp and is never converted into a first traded
session or `listed` date.
[Official DBN field types](https://raw.githubusercontent.com/databento/dbn/main/rust/dbn/src/record.rs),
[Official DBN default values](https://raw.githubusercontent.com/databento/dbn/main/rust/dbn/src/record/impl_default.rs).

When supplied, `ts_in_delta_raw` is retained and an unclamped signed delta gives
`publisher_send_at = ts_recv - ts_in_delta` in exact nanoseconds. Both INT32
extremes indicate clipping and do not establish an exact sending time. Negative
deltas are preserved because publisher and capture clocks can differ. This
derived publisher sending time is a separate field, not OHLCV availability or
proof of customer receipt. OHLCV has no such source field.
[Vendor timestamp conventions](https://databento.com/docs/standards-and-conventions/common-fields-enums-types).

Earlier inspection artifacts remain unchanged when decoder diagnostics are
extended. Their recorded hashes bind the output of their original inspection;
additional source interpretation is written as a new evidence sidecar.
