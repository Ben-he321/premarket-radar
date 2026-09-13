# Ben B1 isolated research

Status: **DATA_GATED_PARTIAL_EXECUTION**. This branch contains rule, earnings,
quote and financing components plus data probes and a finite evidence-window
runner. It does not contain an accepted full historical trading replay or a Ben
forward service. All old M20/U modules and ledgers remain untouched.

The complete user task is `BEN_B1_TASK_20260913.txt`; `frozen_config.json`
records the fixed 15-path matrix. Neither engineering tests nor data access
implies profitable or executable research.

On the verified local host, use the existing virtual environment:
`../premarket-radar-ai-m1/.venv/Scripts/python.exe` (Python 3.13.1).
Required packages are already installed from the existing repository's
requirements. This turn installed no new dependency or service.

Run focused engineering checks with:

```text
python -B -m pytest tests/test_ben_b1_rules.py tests/test_ben_b1_ledger.py tests/test_ben_b1_pipeline.py
```

The read-only page is `pages/15_Ben_B1收盘突破.py`. It reads the local
`BEN_B1_OUTPUT_DIR` override or the default
`C:/Users/benhe/BenAITradingData/ben-b1-research-20260913`.

```text
python -B -m streamlit run pages/15_Ben_B1收盘突破.py --server.port=8521 --server.address=127.0.0.1
```

Check the actual process before starting another instance. The page does not
download, generate signals or submit orders. Its source and all report output
are separate from the original frontend.

Data probes explicitly read the existing project secrets via
`ALPACA_SECRETS_FILE`; no credential file is copied into this worktree. New
market objects go only into the B1 output directory. The original 120/min
quota store is shared to avoid competing with the old service. Do not run an
old `prepare()`/research writer in an attempt to resume B1.

The output contains `BEN_B1_RESULTS.md`, `RECOVERY.md`, input/output manifests
and `verification_ben_b1_research_bundle.zip`. Historical data remain local and
persist after restart. None of the raw market files or credentials are tracked
in Git. The ZIP uses an explicit allowlist and contains no Parquet database.

Full PIT earnings calendars, complete RTH/action warmup and quote paths, and
cross-session execution integration are still required before generating the
15 strategy-return paths and comparable SPY/QQQ/financed-index accounts.
