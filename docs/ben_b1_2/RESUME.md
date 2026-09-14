# 本轮本地恢复入口

工作目录：`C:/Users/benhe/OneDrive/Documentos/GITHUB/premarket-radar-ben-b1-2`。

证据和数据目录：`C:/Users/benhe/BenAITradingData/ben-b1-2-window-portfolio-20260914`。

Python：`C:/Users/benhe/OneDrive/Documentos/GITHUB/premarket-radar-ai-m1/.venv/Scripts/python.exe`。

先读输出目录的 `TASK_STATE.json`、`samples/WORKER_STATE.json` 和相应账户 `progress.json`，再核对实际进程的命令行和创建时间。文件记录的 PID 可能已退出或被系统重新分配，不能单凭数字认定运行正常。同一研究已有进程时不要再启动。

原样本采用 `samples/compact_v1/`；初版 `samples/initial_v1/` 与严格真实 MRVL 对等版本 `samples/compact_equivalence_v1/` 均保留。Q0 来源为旧 B1.1 `replay/final_v4/`，没有新的 Q0 样本重跑。

季度版本使用 `portfolio/compact_base_v1/`，四本分别是 Q0/A、Q1/A、Q0/B、Q1/B；每本独立只有 5500 美元。首次实际启动状态以 `TASK_STATE.json` 与该目录的 `RUN_SPEC.json` 为准，目录名称本身不是已运行证据。固定输入为 `earnings/PRIMARY_EARNINGS_B12_V2.json` 与 `data/HISTORY_INPUTS.json`。

研究命令在本工作目录调用已有 Python：

```text
-m src.ben_b1_2.samples --tiers A B --version compact_v1 --supplement-held-quotes
-m src.ben_b1_2.portfolio --earnings C:/Users/benhe/BenAITradingData/ben-b1-2-window-portfolio-20260914/earnings/PRIMARY_EARNINGS_B12_V2.json --version compact_base_v1
```

样本具体参数须与既有 `RUN_SPEC.json` 核对；不能以新版本名称绕过固定源代码或输入校验。共同账户已有完整收尾及恢复 PASS 时复用；恢复 FAIL 时保留失败证据并停止该账户，不能宣称通过。独立样本另核对每个 `RECOVERY_IDEMPOTENCY.json`，不以 `sample_run_complete` 单字段代替恢复证据。季度日内结果已落入 checkpoint、报告尚未落盘时，只补当日日终报告，不重复请求数据或创建意图。

每本的 `checkpoint.json` 必须配合同目录 `replay_archive.sqlite`，并通过 `RUN_SPEC.json`、`DYNAMIC_INPUT_HASHES.json` 校验。共同账户必须由 `SharedReplayEngine` 恢复，普通 Compact 类会拒绝不同检查点版本。仅有验收 ZIP 不能代替完整本地恢复存储；ZIP 有意排除完整行情和数据库。

报告与打包是只读汇总，不重新研究：

```text
-m src.ben_b1_2.delivery --sample-version compact_v1 --portfolio-version compact_base_v1 --package
```

执行入口仍遵守本轮冻结的 8 小时截止，不会自动延长实验或启动 Ben 前向账户。原 M20/U 服务在旧工作目录，与本轮上述模块无关，不重启、不替换、不重置其账本。
