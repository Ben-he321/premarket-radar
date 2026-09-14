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

## 2026-09-14 实际停止与已保存边界

本轮历史工作进程已于 14:16:42 UTC 停止。实际可用内存降至 352.19 MiB，触发 800 MiB 保护边界；并非已耗尽 15:35 UTC 的八小时截止。只停止身份核对后的 B1.2 回放 PID 32940，未操作原 M20/U 服务。此后的财务提取、隔离恢复检查和打包均不执行新研究事件。

40 个 Q1 A/B 样本已完成；共同资金 Q0/A、Q1/A、Q0/B 已运行至 4 月 30 日尾段并通过恢复。Q1/B 只完成 1 月 2 日至 1 月 22 日的 14 个交易日，保存 5,602,986 个事件、BE 27 股、NVDA 14 股和现金 87.92 美元。下一未完成交易日为 1 月 23 日；1 月 22 日已保存日终权益 6,607.69 美元不是完整季度结果。没有强制平仓、补资或倒填意图。

原始 Q1/B 恢复入口是 `portfolio/compact_base_v1/B12_Q1_P50_B_compact_base_v1/`。保留 `checkpoint.json`、`replay_archive.sqlite` 及当前同目录 `-wal`、`-shm` 文件，不应单独复制数据库主文件。各输入与执行源码仍须匹配 RUN_SPEC；本轮冻结执行提交为 `a37080102623d05b2682055cbc0868732f80a847`。

只读财务导出位于 `partial_evidence/q1_b_ram_stop_20260914T141642Z/`，由 `PARTIAL_EXPORTS.json` 指定。独立副本位于 `recovery_validation/q1_b_ram_stop_20260914T141642Z/`；其实际检查状态以 `engineering/PARTIAL_ISOLATED_RESTORE.json` 为准，不将检查已启动当作通过。复制检查不重启研究，不证明完整季度或重复事件回放已经验收；私有数据库副本不进入 ZIP。

本轮不自动续跑。后续恢复首先需要解决密集报价处理的本机内存容量边界，并保留相同信号、规则、输入和已保存资金状态；不得通过抽稀报价、改变排序、另建 5500 美元账户或扩大参数搜索来绕过问题。验收交付从上述只读报告命令恢复即可，不应先执行研究命令。
