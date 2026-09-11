# V1.2 独立动量研究

继承 PR #25 的 298a3cc，分支 `codex/watchlist-v1_2-momentum`。
使用现有 `.venv`，不安装新研究框架，不购买服务。

本轮固定实验为 `V1_2_MOMENTUM_001`，全部 66 个候选保留。
只计算五个因子和 5/10/20 日标签，共 990 项组合；DXYZ 单独作为 CEF，
参考 ETF 不交易。开发截止为 2026-03-10，后续旧保留集不参与本轮。
历史证据统一标记 `HISTORICAL_EXPLORATORY_REUSED_DATA`。

协议在首次绩效计算前冻结，不能编辑协议后用相同实验 ID 重跑。
详见 `MOMENTUM_PROTOCOL.json`。R0–R3 使用同一 V1.1 内核，
不同于原前向账户；本轮无策略自动晋级。

默认结果目录为 `C:\Users\benhe\BenAITradingData\watchlist-v1_2-momentum`。
可通过 `V12_RUN_DIR` 配置新目录，但更改实验设置需要新的明确任务。
只读复用 V1.1 固定的数据对象；读取 Parquet 时先限制开发截止日期。
缓存、统计中间文件和私有账本备份不进入 Git 或验收 ZIP。

恢复入口（在项目目录执行）：

```
.venv\Scripts\python.exe -m src.v12 status
.venv\Scripts\python.exe -m src.v12 run
.venv\Scripts\python.exe -m src.v12 report
```

`run` 有单实例锁、分证券检查点和冻结的八小时上限。它不会启动新的
前向交易策略。已完成实验不需要再次运行；`report` 只读既有结果并更新
运行状态及报告。任务状态、研究完成回执、日志和文件校验保存在结果目录。

原实验服务保留 `EXPERIMENTAL_PAPER_V1_1` 账本及 FIXED_LEGACY 规则。
修复仅涉及已有持仓 BLOCK 的证券级隔离，以及盘前逐证券有限重试。
新的 `decision-progress-日期.json` 保存各证券状态，最多三次请求尝试，
成功证券不重复，超过原 NY 09:25 截止时间不补造意图。
价格净利润、股息应收、总利润和收益率在事件导出中明确拆分并按同一精度对账。

原前向服务继续通过 Windows 任务 `BenAITrading-ExperimentalPaper-V1_1`
运行。它的启动/停止入口仍是 V1.1 手册；不要删除或重置其 SQLite 账本。
研究 UI 在原 `http://localhost:8513` 页面新增“动量研究”入口，刷新不触发计算或下单。
