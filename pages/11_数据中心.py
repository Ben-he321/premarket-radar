"""Additive AI-M1 page. Does not import or invoke shadow-account/strategy code."""
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import streamlit as st
from filelock import Timeout

from src.data.alpaca_calendar import finalized_day
from src.data.alpaca_client import AlpacaMarketData, DataAccessError
from src.data.alpaca_config import load_config, TRADE_UNIVERSE, REFERENCES, SYMBOLS
from src.data.alpaca_store import ParquetStore
from src.data.alpaca_sync import DataSync
from src.ui.theme import inject_global_styles

MESSAGES = {
    "MISSING_CREDENTIALS": "缺少凭证：请配置 ALPACA_API_KEY 和 ALPACA_SECRET_KEY。真实行情待配置凭证后验证。",
    "AUTH_OR_PERMISSION_DENIED": "凭证无效或权限不足（401/403），请核对凭证及 SIP 数据订阅。",
    "SIP_PERMISSION_DENIED": "SIP 权限不足，请检查数据订阅。没有切换到 IEX。",
    "RATE_LIMITED": "请求达到限速，有限重试后仍失败。稍后可断点续传。",
    "NETWORK_ERROR": "网络连接失败或超时。稍后可断点续传。",
    "REQUEST_FAILED": "行情请求失败；未用模拟数据替代。请稍后重试并核对服务状态。",
    "INCOMPLETE_RESPONSE": "检查返回不完整：NVDA、AMD、MSFT 未全部返回，不能确认接口检查通过。",
    "QUALITY_REJECTED": "检测到重复、无效或缺失数值：本批次保存在隔离记录中，未发布为研究历史。",
    "OUT_OF_RANGE_RESPONSE": "供应商返回区间外数据，已拒绝发布。",
    "UNEXPECTED_RESPONSE": "供应商响应结构异常，已拒绝发布。",
    "DATA_DIR_NOT_DEDICATED": "数据路径包含其他文件，请将 ALPACA_DATA_DIR 指向一个空的专用目录，或已有的 AI-M1 数据目录。",
}

st.set_page_config(page_title="数据中心 · Ben AI Trading", page_icon="🗄️", layout="wide")
inject_global_styles()
st.title("数据中心")
st.caption("AI-M1 · Alpaca SIP 美股日线 · 数据研究，不连接交易账户")
config = load_config(st.secrets)
store = ParquetStore(config.data_dir)
end = finalized_day(cutoff_hour=config.finalize_hour_ny)
st.write("固定股票池：" + "、".join(TRADE_UNIVERSE))
st.write("市场参考（不加入可交易名单）：" + "、".join(REFERENCES))
st.caption("未来回测与影子账户初始资金配置：5500 USD；现有账本未修改。")
st.info(f"历史请求从 2016-01-01 至 {end}（纽约交易日期）。仅纳入纽约次日 06:00 后的交易日；这是保守截止规则，不代表供应商承诺永不修订。")
st.write(f"实际保存目录：`{config.data_dir}`")
st.caption("本地文件在普通重启后保留；云端临时磁盘在重部署或回收后可能丢失。请配置持久卷路径或导出备份。不会自动上传至云端持久存储。")
if not config.has_credentials:
    st.warning(MESSAGES["MISSING_CREDENTIALS"])


def show_error(exc):
    if isinstance(exc, DataAccessError):
        st.error(MESSAGES.get(exc.code, "请求未完成：" + exc.code))
    elif isinstance(exc, Timeout):
        st.warning("另一个下载正在使用此数据目录，请等待其完成。")
    else:
        # Never display exception text that could carry a secret or HTTP payload.
        st.error("数据操作失败，请检查目录读写权限、磁盘空间和安装依赖。已有发布数据保留。")


st.subheader("SIP 连接检查")
st.caption("两项权限独立验证。最新成交时间会原样展示；休市时最新成交可能较早，端点访问成功不等于成交持续更新。")
for column, kind, label in zip(st.columns(2), ("historical", "latest"), ("历史 SIP", "最新 SIP")):
    with column:
        if st.button(f"检查{label}", key=kind):
            try:
                result = AlpacaMarketData(config).probe(kind, end)
                st.session_state[f"alpaca_{kind}"] = result
            except Exception as exc:
                show_error(exc)
        result = st.session_state.get(f"alpaca_{kind}")
        if not result:
            st.write(f"{label}：待验证")
        elif result["status"] == "ACCESS_OK":
            st.success(f"{label}：端点访问通过（feed=sip）")
            st.json(result)
        else:
            st.error(MESSAGES.get(result["status"], result["status"]))

st.subheader("下载与增量更新")
st.caption("每次下载先用 NVDA、AMD、MSFT 检查短区间，再下载全部 18 个标的的 raw 与 all。重复点击首次下载也会续传，不清空历史。")
full = st.checkbox("重新核对全部历史（用于较早修订或证券映射变化，会增加下载量）")
first, incremental = st.columns(2)
run = first.button("首次下载 / 断点续传", disabled=not config.has_credentials)
run = incremental.button("增量更新", disabled=not config.has_credentials) or run
if run:
    status = st.empty()
    try:
        result = DataSync(AlpacaMarketData(config), store, config).update(
            full_refresh=full, progress=status.info)
        st.session_state.pop("alpaca_export", None)
        status.success("下载请求完成。请查看下方覆盖区间及质量问题；完成下载不代表数据验收通过。")
        st.json(result)
    except Exception as exc:
        status.empty()
        show_error(exc)

st.subheader("数据覆盖与质量")
try:
    inventory = store.inventory(SYMBOLS)
    if not inventory.empty:
        inventory["updated_at_Madrid"] = pd.to_datetime(inventory.updated_at, utc=True).dt.tz_convert("Europe/Madrid")
    st.dataframe(inventory, use_container_width=True, hide_index=True)
    for adjustment in ("raw", "all"):
        with st.expander(f"{adjustment} · 请求记录、证券映射及质量详情"):
            attempt = store.read_json(f"{adjustment}/last_attempt.json", {})
            if attempt:
                st.json(attempt)
                if attempt["status"] != "COMPLETE":
                    st.warning(MESSAGES.get(attempt["status"], "上次请求失败；不能归因为停牌或休市。"))
            pending = store.read_json(f"{adjustment}/pending.json", {})
            if pending:
                st.write(f"待续传：已有 {len(pending.get('completed', {}))} 个月完成；发布版本仍为上次完整版本。")
            quarantine = store.read_json(f"{adjustment}/quarantine.json", {})
            if quarantine:
                st.warning("存在历史隔离批次；以下记录用于审计，不包含在有效研究快照中。")
                st.json(quarantine)
            st.json(store.active(adjustment))
    st.caption("UNKNOWN_PREHISTORY 表示首条可得数据之前的未知区间，不是核实的上市日期。UNKNOWN_MISSING_SESSION 需要核对停牌或供应商缺口。休市由交易日历判定；接口失败单独记录。缺失量不会填成 0。")

    st.subheader("数据导出")
    if st.button("准备 Parquet 与元数据 ZIP", disabled=not inventory.rows.sum()):
        st.session_state["alpaca_export"] = store.export_zip()
    if "alpaca_export" in st.session_state:
        st.download_button("下载数据备份", st.session_state["alpaca_export"],
                           file_name="alpaca-sip-history.zip", mime="application/zip")
except Exception as exc:
    show_error(exc)

st.caption("口径：Alpaca 日线按纽约日期聚合，各字段依 SIP 成交条件更新；成交量可能包含延长时段，VWAP 的统计量也不必等于总成交量。raw 为未复权，all 为供应商复权，不代表公司行动已独立核验。")
st.markdown("[安装、时间口径与验收说明](https://github.com/Ben-he321/premarket-radar/blob/codex/ai-m1-alpaca-data/docs/AI-M1.md)")
