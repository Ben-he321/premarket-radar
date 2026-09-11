# V1.1 local execution

Use the existing `.venv/Scripts/python.exe`. Run from this repository, on
`codex/watchlist-v1_1-execution`. No .env loading, broker integration or paid model.
Credentials continue to come from the existing Streamlit Secrets/environment.

`V11_SOURCE_DIR` defaults to the existing `watchlist-research-v1` beside ALPACA_DATA_DIR.
`V11_RUN_DIR` defaults to the separate `watchlist-v1_1-execution` directory.
The program rejects equal roots. Original data object references and result hashes
are frozen in `v1_snapshot.json`; V1 research and its observer are not reset.

The following commands are recovery entry points, not evidence they have run:

```
.venv\Scripts\python.exe -m src.v11 status
.venv\Scripts\python.exe -m src.v11 analysis
.venv\Scripts\python.exe -m src.v11 paper-service
.venv\Scripts\python.exe -m src.v11 stop-paper
.venv\Scripts\python.exe -m src.v11 report
.venv\Scripts\python.exe -m streamlit run pages/13_V1_1执行.py --server.port 8513
```

`analysis` reuses immutable restatement versions and cached candidate events;
it does not select on the old final holdout. Do not revise frozen parameters or
replace old candidate artifacts: a changed execution protocol needs a new version.

The paper service is independent of the page and uses a Windows single-instance
file lock and transactional SQLite ledger. It stops after 30 days or a STOP file.
To resume after an intentional stop, remove only this version's `experimental_paper/STOP`,
then start the service. Never remove or reset its ledger. A missed decision window
cannot create a past intent. Intent updates/deletes are prohibited by SQLite triggers.

Decision window: NY 06:30–09:25 on an NYSE session, using finalized daily data.
All 66 candidates remain in coverage; the three reference ETFs and DXYZ cannot
create orders. FIXED_LEGACY only, four holdings at most, integer new buys,
0.5% account risk, 20% weight, previous known daily volume participation 0.1%,
$1 per side and 10bp friction. Five-percent entry budgeting collar is a reserved
cash estimate, not a future price observation. Fills may be downsized, never exceed
the original quantity or reserved budget. Buy and contingent sell intents are
recorded together before the open.

Entry price is the first valid positive-volume RTH minute open within 09:30–09:35,
retrieved after at least 20 minutes. It is a paper proxy, not a broker fill guarantee.
Stops take priority if a bar touches both barriers; overnight gaps use the first
available open. Time exits use the final regular minute of the third session;
if its quote is absent, the position remains unresolved until a later valid quote.
Sells settle one NYSE session later. Splits are idempotent; unknown dividend payment
dates remain receivables. Unresolved complex corporate actions block that security.

The historical candidate subexperiment uses a fixed $5500 reference state, raw
execution paths and all-adjusted feature snapshots. Portfolio replay uses its actual
cash/equity. Both call the same quantity and fill routines. Current adjusted data is
not a point-in-time vintage and cannot establish a historically tradable signal.
No factor selection or new economic validation is implied by engineering parity.

Local data survives process/page restarts. It is not automatically cloud backed up.
The verification ZIP is a report-only allowlist, not a full data/ledger backup.
Minute data, raw/all bars, complete databases and credentials are never bundled.
