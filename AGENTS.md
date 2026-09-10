# Ben AI Trading watchlist research V1

Work only in this repository. Inherit AI-M1 from 69a8ecd; never reset old work,
modify main, submit broker orders, change old shadow ledgers or buy services.
User specification: 66 candidates plus three reference ETFs; identities must be
verified independently of Alpaca us_equity. Isolate unknown identities and failures.
Use .venv/Scripts/python and the shared config loader. Never print credentials.
Do not call paid model APIs: optional budget is $25/100 calls but this implementation
uses zero calls. Keep mock data in tests' temporary directories.
Persist task status/checkpoints; never claim a stopped agent is still developing.
See RUNBOOK.md and TASK_STATE.json. Research limits and holdout must be preregistered.
