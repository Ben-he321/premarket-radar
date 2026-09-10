# Ben AI Trading 全池研究 V1

本目录是已有 AI-M1 的独立工作目录，不是旧看盘程序目录。
仓库 `Ben-he321/premarket-radar`，分支 `codex/watchlist-research-v1`，继承 PR #23 的 `69a8ecd`。
main、旧页面、旧策略结论、旧影子账本不改。没有券商下单模块。

## 实际入口与保存位置

- 新版全池页面：http://127.0.0.1:8512/ ，入口 `pages/12_全池研究.py`。
- 旧 AI-M1 数据页面仍在 http://127.0.0.1:8511/；不会因为打开页面启动研究。
- 项目虚拟环境：本目录 `.venv/Scripts/python.exe`，Python 3.13。
- 本机全池数据：`C:/Users/benhe/BenAITradingData/watchlist-research-v1`。
- 默认全池根目录为 `ALPACA_DATA_DIR` 的同级 `watchlist-research-v1`，可用 `WATCHLIST_RUN_DIR` 显式覆盖。
- 凭证从全局/本项目 `.streamlit/secrets.toml` 或环境读取。只需要 `ALPACA_API_KEY`、`ALPACA_SECRET_KEY`；不会自动加载 `.env`。不打印密钥。
- 数据、队列、异常、报告、前向账本和备份保存在本地磁盘。页面关闭不会删除；重启后文件保留，但电脑关机/休眠期间不执行任务。云端运行必须挂持久磁盘；GitHub 不保存行情。

## 恢复与停止

在此目录使用已有虚拟环境；下列是恢复入口，首次运行已经实际执行，不需要 Ben 再重复下载：

```powershell
.\.venv\Scripts\python.exe -m src.watchlist status
.\.venv\Scripts\python.exe -m src.watchlist run-all --resume
```

`run-all --resume` 依次复用身份缓存、续传缺失历史、读取公司行动、复用已冻结研究、生成报告、验证备份、在工程测试已通过时启动现金观察服务。单批上限8小时；文件锁阻止重复研究/同步。已经完成的最终保留检验不会因新行情自动重跑。若在最终检验中途崩溃，保留 `holdout_receipt.json` 和部分结果，明确报错；需审计恢复，不能声称修复重跑仍是首次独立检验。

```powershell
# 仅更新最近5个交易日，不重下所有历史
.\.venv\Scripts\python.exe -m src.watchlist incremental --resume
# 打开页面（检测端口，不覆盖其他应用）
powershell -ExecutionPolicy Bypass -File scripts/启动全池研究.ps1
# 停止现金观察；最迟30秒响应
.\.venv\Scripts\python.exe -m src.watchlist stop-paper
```

停止标记是数据目录 `paper/STOP`。有意恢复时删除该**单个文件**，再运行 `scripts/启动全池研究.ps1 -Mode Observer`。不删除账本。服务每次启动最多运行30天，纽约交易日06:30执行；时区用 America/New_York，跨夏令时自动换算。窗口关闭/页面刷新不会开启第二个观察服务。Windows 登录启动任务的实际注册结果见本地 `service_registration.json`；注册失败时通过上述入口恢复。

## 结果与检查点

|文件/目录（相对数据目录）|内容|
|---|---|
|`RESULTS.md`, `candidate_results.csv/json`|全部66个处理结果、逐项缺口、隔离与 UNKNOWN|
|`universe.json`, `coverage.json`, `datasets/`|证券身份、138组覆盖、不可变Parquet、请求与数据版本|
|`quarantine/`, `sync_errors.json`|原始异常响应、具体日期、接口错误；不填补假行情|
|`sip_checks.json`, `incremental_verification.json`|真实权限与增量检查|
|`tasks.sqlite`, `status.json`|任务事件、输入版本、完成/错误与心跳；项目内镜像 `TASK_STATE.json`|
|`preregistration.json`|看绩效前冻结的22特征、12候选、滚动规则、成本与晋级门槛|
|`research/all_trials.json`, `per_stock_strategy_table.csv`|全部858个证券×配置诊断（含无样本）；不只记录胜者|
|`research/frozen_pipeline.json`, `selection_evidence.json`|训练/验证日期、共享/逐股/市场状态选择与外层区间|
|`research/holdout_receipt.json`|最终126日的一次检验记录；本轮起点2026-03-11|
|`research/*equity.parquet`, `*trades.parquet`, `*skips.json`|单一5500美元账户回放、成本压力与未成交原因|
|`research/promotion.json`, `improvements.json`|未晋级原因；本轮0次策略改进、0付费模型调用|
|`paper/ledger.sqlite`, `paper/status.json`|新现金观察账户、唯一事件、真实启动时间、PID、下次运行|
|`backup_verification.json`|备份位置、独立恢复目录、逐文件校验与可读性|

本机备份放在 `C:/Users/benhe/BenAITradingData/watchlist-backups`。恢复验证使用独立临时目录，不覆盖研究目录。备份含供应商数据，仅供用户本地备份，不上传 GitHub。

## 研究口径与没有完成的验收

66候选按当前官方交易所目录核验。SPCX/CCXI 等复用代码限定新证券的真实边界；ECHO 的 SATS 历史、META 的 FB 历史使用明确分段和 `asof=-`，不自动拼接旧同名公司。DXYZ 为 CEF，保留单独诊断，排除普通股票共享选择及组合。SPY/QQQ/SOXX 是参考，不交易。当前候选回看历史存在选择/生存偏差，不是无偏全市场实验。

Alpaca Basic 只使用 SIP 历史；最新 SIP 未开通不阻断历史下载。限速为跨进程、分页、重试共120请求/分钟。日线结束日期使用纽约次日06:00确认和至少20分钟历史滞后，不混入未完成当日。保留UTC原时间戳，以纽约交易日对齐；展示转换用时区库。

日线 OHLC 与成交量不能默认全部属于常规盘中时段（见官方 FAQ）。回放的 daily open 尚未核实为常规开盘可成交价，因此本轮 raw 账户曲线全部是诊断。公司行动 API 真实可访问，但完整性、部分股息付款日和部分重组处理仍 UNKNOWN；缺少付款日期的股息单列应收、不当成可用现金。

统一账户现金5500、最多4持仓、单股计划20%、整数股、计划止损风险0.5%、先前成交量参与上限0.1%、佣金1美元/边、摩擦10bp/边、25/50bp和双佣金压力、现金回用延迟1交易日。同根止盈止损按不利顺序，跳空止损按开盘加摩擦。all 复权仅做事件/特征诊断，不直接当原币值买价；参考曲线为首日收盘归一化的复权单位收益、无执行成本，不冒充整数股可执行账户。

最后126交易日不参与探索、筛选和更新周期选择；冻结后仅评估一次。外层按共同日历滚动，训练/验证之间及验证/外层之间各6日隔离，标签必须成熟。最大持有5日。每个窗口选股排序只用训练/验证分数；不能用当日最终量决定开盘成交。

当前三个账户结构的外层净期望及25bp压力结果均未达标；没有 Champion。共享结构的个股样本不算作该股独立验收。完整多层搜索不具备单一交换性统计矩阵，正式全搜索DSR标记不可用；13固定配置日期矩阵的DSR/PBO只作局部诊断。没有为提高分数重新调参。进入可交易前向状态还需独立验证公司行动/可执行价格口径、取得新的合格时间外证据并验证真实时间订单生命周期。本版本前向服务明确只观察现金，**没有实施或声称验证前向成交引擎**。

## 安装与测试

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

测试会移除环境凭证、禁用网络并改用临时目录。mock只在测试里。真实下载/历史回放不是mock测试，二者分别记入本地报告。第三方 Skills 的固定版本、依赖与许可证见 `docs/THIRD_PARTY_RESEARCH.md`；未安装时统计适配器明确不可用，不回退合成数据。
