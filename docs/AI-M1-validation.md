# AI-M1 验证记录

日期：2026-09-10。目标仓库：`Ben-he321/premarket-radar`。开发分支：`codex/ai-m1-alpaca-data`；基线：`cb58963`。

## 实际执行

| 检查 | 结果 |
| --- | --- |
| GitHub 插件读取目标仓库 | 成功，核对 full_name、默认分支与仓库权限信息 |
| Git 读取与 fetch | 成功，remote 为目标仓库；README、requirements、app 已实读 |
| 分支与未合并 PR | open PR API 返回 `[]`；检查旧分支，不合并旧工作 |
| 隔离工作树 | 成功，原目录的已暂存/未暂存/未跟踪改动保留 |
| Python | 3.13.1，实际执行成功 |
| 项目及开发依赖安装 | 独立 `.venv` 安装成功 |
| `python -m pip check` | No broken requirements found |
| `python -m pytest -q` | **31 passed**，全部 OFFLINE MOCK，10.84 秒 |
| `python -m compileall -q src pages app.py` | 通过 |
| `git diff --check` | 通过；Windows Git 的 LF/CRLF 提示不构成空白错误 |
| Streamlit AppTest | 新页面无凭证渲染、36 行覆盖表、禁用下载、点击检查提示均通过 |
| 实际本地 Streamlit + 浏览器 | 启动成功；核对 DOM 与截图，点击最新 SIP 显示缺少凭证，无崩溃 |

测试有 114 条第三方弃用警告，来自 NumPy timedelta / pandas-market-calendars / exchange-calendars，以及 websockets legacy；无失败。未为消除警告修改旧依赖行为。

## 测试内容

真实 alpaca-py SDK 配合 mock HTTP：三页跨标的分页、SIP 强制参数、401/403/422 权限分类、429/5xx/超时重试及上限、历史/最新独立状态。其余 mock 测试：夏令时错位、周末/休市/提前收盘、未结束当日拒绝、重复与 OHLC/缺失价格/成交量、异常量、晚起历史与空月份、增量幂等、扩展部分月份、失败月续传、all 复权整段重建与失败保留旧版本、锁、防覆盖专用目录、DuckDB 查询、ZIP 导出恢复后增量。

测试自动禁止 requests 真实 HTTP，全部行情写入 pytest 临时目录；研究目录没有生成模拟数据。浏览器使用没有凭证的真实 Streamlit 进程，仅验证页面，不验证行情。

## 待验证

- 当前工作环境两个 Alpaca 环境凭证未齐备，工作树无 Secrets 文件；**未进行真实行情请求**。
- 历史 SIP 权限、最新 SIP 权限、三股票短区间真实响应、18 标的完整下载及真实质量：待配置凭证后验证。
- 真实供应商分页、实际限速/修订以及纽约次日缓冲的适用性：待真实联调。
- 上市日期、证券 ID、更名映射和公司行动独立核验：UNKNOWN，未虚构已核实。
- 云端部署、Python 3.14、持久卷与重部署恢复、OneDrive 同步：未验证。
- 未访问或修改真实交易账户，未执行旧影子组合操作，未重跑或改写旧策略收益结论。

代码推送与 PR 的最终结果以本分支 Git 提交记录及交付 PR 为准；本记录不以仓库权限标志代替实际推送证据。
