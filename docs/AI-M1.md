# Ben AI Trading · AI-M1 Alpaca 数据底座

## 范围与接管

本次是 `Ben-he321/premarket-radar` 的增量升级。保留现有首页、Finnhub、yfinance、策略回测、日志及 Supabase 影子组合。本次新增数据层没有调用旧影子引擎或写入账本，也没有改变旧策略的收益结论。旧页面原有行为仍然存在。

- 固定研究股票池：NVDA、AMD、AVGO、MRVL、MU、WDC、STX、LITE、AAOI、QCOM、ALAB、TSLA、PLTR、MSFT、RKLB。
- 参考标的：SPY、QQQ、SOXX；不加入可交易名单。
- `FUTURE_INITIAL_CAPITAL_USD = 5500` 仅供未来模块使用。现有影子账户余额、持仓与成交均未清空或迁移。
- 只用 `alpaca-py` 的 `StockHistoricalDataClient`，强制 SIP、GET 与 `data.alpaca.markets`。不初始化交易客户端、不连接真实交易账户，不调用资产/下单/账户接口。
- 不训练模型、不挖因子、不生成买卖建议；不接 IBKR、期权或虚拟币。

接管基线是远程 main 的 `cb58963`。原本地目录位于 `premarket-radar`，分支为 `codex/backfill-sector-snapshot-sample`，存在已暂存、未暂存及未跟踪的旧工作。开发在独立工作树 `premarket-radar-ai-m1`、分支 `codex/ai-m1-alpaca-data` 进行，没有搬移旧改动。检查时 GitHub open PR API 返回空数组；远程 `codex/supabase-shadow-portfolio` 有旧的未并入提交，涉及 Supabase REST，不与新增数据模块冲突，没有合并它。

## 安装与运行

已实际验证 Windows / Python 3.13.1。其他 Python 版本（包括云端 3.14）尚未实测；建议部署使用 3.13。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m streamlit run app.py
```

在 Streamlit 侧栏打开 **数据中心**。也可以单独启动新页面，避免进入旧页面时触发现有外部查询：

```powershell
.\.venv\Scripts\python -m streamlit run pages/11_数据中心.py
```

Linux/macOS 将 `.\.venv\Scripts\python` 换成 `.venv/bin/python`。

凭证从 Streamlit Secrets 优先读取，再回退到同名环境变量。示例中的值始终为空：

```toml
ALPACA_API_KEY = ""
ALPACA_SECRET_KEY = ""
ALPACA_DATA_DIR = ""
```

本地可复制 `.streamlit/secrets.toml.example` 为 `.streamlit/secrets.toml` 后在本机填写。云端在应用 Secrets 中填写。也可设置进程环境变量。不会自动加载 `.env`，仅编辑 `.env.example` 或 `.env` 不会让本模块获得凭证。禁止提交真实 Secrets；UI、错误与元数据不输出凭证或 HTTP 响应正文。

最少操作：

1. 安装依赖并配置两个凭证；选择空的专用持久数据目录。
2. 打开数据中心，分别点击“检查历史 SIP”和“检查最新 SIP”。
3. 点击“首次下载 / 断点续传”，检查 36 行覆盖表（18 个标的 × 2 种调整方式）和质量详情。
4. 再点“增量更新”，确认更新窗口及覆盖区间；下载 ZIP 备份。

没有凭证时，页面仍能读取已有本地快照和导出；下载按钮禁用并显示明确提示。不会生成模拟行情，也不会借用旧页面的 yfinance/Finnhub 数据填充本数据集。

## SIP 权限与原始返回

历史检查对 NVDA、AMD、MSFT 查询最近已结束交易日前约十天的日线，三只均有结果才通过。每次完整/增量下载前都会执行此检查，然后处理全部 18 个标的。

最新检查独立调用三只股票的 latest trade SIP 端点，展示返回时间。历史通过不会使最新状态通过；最新成交较早时也不声称“实时成交正在更新”。历史下载不依赖最新权限通过。401/403 只能判为凭证或权限问题，不能武断认定是哪一种；SIP 订阅的 422 错误单独分类。任何错误均不转用 IEX。

SDK 0.44.0 内部持续跟随 `next_page_token`，按标的累计所有页。请求不设置总行数 `limit`；不能用 `limit=10000` 当作单页大小，否则 SDK 会截断总结果。测试使用真实 SDK 分页实现、mock HTTP 三页，让不同标的分布于不同页。下载单位为月；每个 HTTP 页最多尝试四次，对 429、5xx、连接错误和超时退避重试。默认每分钟最多约 120 次、连接/读取超时为 10/45 秒。权限错误不重试。失败月下次重试，不把失败当作空行情。

## 时间与价格/成交量口径

依据 [Alpaca Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)：日线按纽约日期聚合；价格、成交量及 VWAP 依各自的 SIP 成交条件更新。延长时段成交可能计入日线成交量，而不更新日线 OHLC；VWAP 的分母也不必等于该条日线的总成交量。因此本模块不标注所有字段为“仅常规交易时段”。

每行保留供应商时间戳文本 `timestamp_original`、UTC `timestamp` 和纽约 `trade_date`。展示更新时间时用 `Europe/Madrid` 的时区数据库转换，不硬编码欧美时差。

起始请求为 2016-01-01。交易日历使用 `pandas-market-calendars` 的 NYSE 美股会话（包含休市和特殊休市）；这些标的按同一美国股票交易日口径检查。最新截止日满足**整个纽约日期已经结束，且纽约次日 06:00 已过**，以涵盖延长时段和保守修订缓冲。周末/节假日不产生伪造日线，提前收盘日仍等待次日缓冲。越界返回一律拒绝。

**“定稿”是本项目的操作截止规则，不是供应商不可修订的认证。** 官方资料没有为本任务提供不可变数据的承诺；元数据写明 `OPERATIONAL_CUTOFF_NOT_PROVIDER_CERTIFIED`。与真实接口联调、观察修订和确认截止策略仍是验收项。

## 存储、版本、恢复与导出

默认位置为本项目绝对路径下的 `data/alpaca`，页面展示实际解析路径。`ALPACA_DATA_DIR` 可指定绝对路径；相对路径相对于项目根目录。必须使用空的专用目录或已有 AI-M1 数据目录，避免覆盖其他项目文件。

```text
data/alpaca/
  dataset.json
  .gitignore
  historical_check.json
  raw/ 或 all/
    objects/<id>.parquet
    objects/<id>.json
    manifest.json
    versions/<version>.json
    pending.json
    last_attempt.json
    quarantine.json    # 仅发生数值质量错误时存在
```

`raw` 是供应商未复权日线，不是逐笔成交；`all` 是供应商 all 调整日线。每月对象独立保存，两者不互相覆盖。对象元数据保存请求参数、下载 UTC 时间、SDK 版本、数据源、feed、调整方式、区间、原始记录数与 SHA-256。版本 manifest 保存证券映射、角色、覆盖范围、质量结果、日历版本和截止策略。依据 [Historical bars API](https://docs.alpaca.markets/us/reference/stockbars)，明确使用 `adjustment=raw/all` 和 `asof`；asof 使用本次目标截止日，证券更名链仍未独立核验。

只记录请求/返回标的代码，不虚构公司行动、稳定证券 ID 或上市日期。证券 ID、更名历史核验、上市日期、公司行动核验均为 `UNKNOWN`。第一条可得记录不是已核实上市日期；上市晚或供应商历史短的标的保留实际可得区间，无价格填补。

每完成一个月就写续传清单；只有一个调整方式的全部计划完成后才原子切换它的 manifest。中途失败时旧发布版本可查询；首次下载失败时只显示续传进度。raw 与 all 分别发布，因此失败后它们可能有不同截止日期，必须查看各自版本，不得假定两者同时完成。

增量更新从上次截止日前五个交易日所在月开始重新核对（实际窗口至少五个交易日，按整月请求），历史完整月份复用。若复权重叠区间的旧记录发生变化或删除，all 会重建全部历史，再一次性发布，避免新旧复权基准混用。可从中断月份续传；all 的未完成重建跨目标日期时重新开始，以避免跨日基准混用。raw 续传可复用已完成旧月份。供应商更早且未影响重叠区间的修订/更名无法自动证明已发现，需勾选“重新核对全部历史”。

不同进程/浏览器通过目录文件锁避免同时下载。数据对象不可变，查询只读发布 manifest 指向的 Parquet，不扫描旧版本和隔离对象。旧对象保留作审计，会占磁盘；本任务不自动清理。

DuckDB 查询示例（仅本地读取，无网络）：

```python
from src.data.alpaca_config import load_config
from src.data.alpaca_store import ParquetStore

store = ParquetStore(load_config().data_dir)
nvda = store.read("all", symbols=("NVDA",))
```

数据中心导出 ZIP 包含发布版本的 raw/all Parquet、每对象元数据、manifest 和数据目录标识，排除凭证与未完成/隔离对象。恢复到空目录后，将 `ALPACA_DATA_DIR` 指向该目录即可继续。不要直接对 `objects/*.parquet` 全量扫描，否则会读到重复历史版本。

本地普通重启后文件仍在；删除目录或更换机器不会自动恢复。当前工作树位于用户 OneDrive 目录中，但没有验证 OneDrive 同步或备份成功。Streamlit Cloud 临时文件系统不能保证重部署/回收后保留；云端需要自行挂载持久卷或导出到外部备份。此模块未接入对象存储或自动云备份。

`.gitignore` 忽略默认行情目录、Parquet、DuckDB 和 ZIP；每个新数据目录另写 `*` 保护其元数据。不要使用 `git add -f` 提交数据。

## 数据质量与诚实状态

- 重复日期/记录、缺失字段、无效/非有限价格、OHLC 关系、负值/非有限/缺失量：标为错误，原始响应保存在隔离对象中，阻止本次版本发布，不静默去重或补数。
- 零量、相对前 20 个观测值中位数大于 20 倍或小于 1/100 的量：标记人工复核，不当作已证实错误或信号。缺失量始终保持缺失。
- 交易所休市：由日历单独统计，非交易日出现日线则报错。
- `UNKNOWN_PREHISTORY`：首条可得数据之前的未知区间，可能涉及上市时间或覆盖不足，不声称“已核实上市前”。
- `UNKNOWN_MISSING_SESSION`：交易日有缺口；停牌、供应商遗漏等原因尚未核实，不根据空数据猜测停牌。
- `UNKNOWN_NO_DATA`：请求成功但无记录，不能认定是停牌、上市前或接口成功验收。
- 接口失败：单独保存错误分类，不写成缺失证券行情，也不改写已有版本。

数值检查通过不等于证券资料、公司行动和全部缺口原因已核验。首次下载完成也不等于 AI-M1 真实数据验收通过。

## 测试与当前验收状态

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m pip check
.\.venv\Scripts\python -m compileall -q src pages app.py
```

测试文件明确标记 MOCK，使用 pytest 临时目录；自动禁止真实 requests HTTP。覆盖分页/多标的、权限分离、重试上限、时区与节假日、质量隔离、断点续传、增量幂等、全历史复权重建、并发锁、Parquet/DuckDB/ZIP 和无凭证页面渲染。测试不是行情验收。

交付时环境没有可用 Alpaca 凭证；以下均**待配置凭证后验证**：历史 SIP、最新 SIP、三标的短区间、18 标的 2016 至截止日真实下载、实际数据质量、真实分页/限速、供应商修订、云端持久保存。证券资料和公司行动为 UNKNOWN。离线测试和依赖安装结果见 PR 与 `docs/AI-M1-validation.md`。

数据验收还需：配置凭证，分别取得 SIP 检查结果；完成真实下载并审核每标的实际覆盖、质量异常与未知缺口；确认 NY 截止策略符合实际修订情况；验证第二次增量及导出恢复；核实所需证券映射/公司行动或明确接受 UNKNOWN 限制；若云端部署，验证重启/重部署后的持久性。本次不为达到“通过”而伪造任何证据。
