# F0 continuation 2026-09-16: resource-stop checkpoint

This is an incomplete engineering checkpoint, not a qualified research release.
The same `codex/futures-f0-pilot` worktree and data root are retained. No frozen
rules, research periods, costs, account balances or original artifacts changed.

The independent round is `continuations/20260916T014732Z`. Its immutable
`ROUND_STATE.json` and `AUTHORIZATION.json` pin a maximum four-hour authorization
ending 2026-09-16T05:47:32.457120Z. Dynamic status lives in `ROUND_PROGRESS.json`.
The first real-input integration guard stopped at 2026-09-16T01:59:07.692236Z:
available system RAM was 1,841,192,960 bytes, below the 2 GiB boundary. The parent
independently hit the same boundary 20 seconds later. No retry or backtest ran.

Completed: actual append-only budget renewal preserved the original OPEN and
12 reservations/completions; cumulative listed reservation remains
0.004445128142 USD under the same 10 USD ceiling. New account evidence was read
from the authenticated portal, with source timestamps and account/key binding.
No new billable data request or cash-payment action occurred.

Official documentation now supports a bounded capture-time-prefix model for
GLBX OHLCV, including the published policy of dropping trade busts/corrections.
This is a documented interpretation, not a per-file vendor certificate. Actual
supplier publication and historical customer receipt remain unknown. Internal
cutoff/compute clock fields are distinct; no synthetic delay is added.

The budget changes have isolated test evidence. The last acquisition/clock/
normalization integration edits have NOT had final regression or a successful
real-data run. In particular, the policy JSON and importer evidence schemas
still need reconciliation, and a derived session hash must be validated against
its actual raw-source chain. Dated calendars, concrete notice/expiry boundaries,
settlement pricing-reference versions and status qualification remain unfinished.
The normalized importer must remain closed until these checks pass.

Resume from the round RUNBOOK and evidence/integration/INTEGRATION_HANDOFF.md,
including CLOCK_HANDOFF_CORRECTION.json. Keep the old reports and caches. Check
available RAM and current authorization before any execution; expired browser
credit evidence requires a genuinely new capture and an appended EVIDENCE event,
not an edited timestamp. Missing broker margin history alone does not block the
frozen explicitly assumed 10%/20% margin and 2 USD per-side commission layers.

F0/F1 real accounts run: zero. All research performance remains NA. Do not use
the existing SPY/QQQ benchmark or passed fixture tests as futures results.
