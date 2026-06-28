"""Supabase 客户端封装：读取配置、创建连接、提供安全读写辅助函数。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any


@dataclass(frozen=True)
class SupabaseStatus:
    """Supabase 连接状态，页面只展示状态和原因，不展示密钥。"""

    ok: bool
    message: str


def _read_secret(secrets: object | None, key: str) -> str | None:
    """从 Streamlit Secrets 读取配置；不存在时返回 None。"""

    if secrets is None:
        return None
    try:
        value = secrets[key]  # type: ignore[index]
        return str(value).strip() if value else None
    except Exception:
        return None


def get_supabase_config(secrets: object | None = None) -> tuple[str | None, str | None]:
    """按 Streamlit Secrets -> 环境变量的顺序读取 Supabase 配置。"""

    url = _read_secret(secrets, "SUPABASE_URL") or os.getenv("SUPABASE_URL")
    key = _read_secret(secrets, "SUPABASE_KEY") or os.getenv("SUPABASE_KEY")
    return (url.strip() if url else None, key.strip() if key else None)


def get_supabase_client(secrets: object | None = None) -> tuple[Any | None, SupabaseStatus]:
    """创建 Supabase 客户端；缺配置或依赖时返回友好状态，不抛异常。"""

    url, key = get_supabase_config(secrets)
    if not url or not key:
        return None, SupabaseStatus(
            ok=False,
            message="未检测到 Supabase 配置，请在 Streamlit Secrets 配置 SUPABASE_URL 和 SUPABASE_KEY。",
        )

    try:
        from supabase import create_client

        return create_client(url, key), SupabaseStatus(ok=True, message="Supabase 连接配置已加载")
    except ImportError:
        return None, SupabaseStatus(ok=False, message="缺少 supabase 依赖，请先安装 requirements.txt。")
    except Exception as exc:
        return None, SupabaseStatus(ok=False, message=f"Supabase 客户端创建失败：{exc}")


def fetch_rows(
    client: Any | None,
    table_name: str,
    *,
    order_by: str | None = None,
    desc: bool = True,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], SupabaseStatus]:
    """安全读取表数据；失败时返回空列表和中文错误。"""

    if client is None:
        return [], SupabaseStatus(ok=False, message="Supabase 未连接，暂时无法读取数据。")

    try:
        query = client.table(table_name).select("*")
        if order_by:
            query = query.order(order_by, desc=desc)
        if limit:
            query = query.limit(limit)
        response = query.execute()
        data = response.data if isinstance(response.data, list) else []
        return data, SupabaseStatus(ok=True, message="读取成功")
    except Exception as exc:
        return [], SupabaseStatus(ok=False, message=f"读取 {table_name} 失败：{exc}")


def insert_row(client: Any | None, table_name: str, payload: dict[str, Any]) -> SupabaseStatus:
    """安全写入单行数据；失败时返回中文错误。"""

    if client is None:
        return SupabaseStatus(ok=False, message="Supabase 未连接，暂时无法写入数据。")

    try:
        client.table(table_name).insert(payload).execute()
        return SupabaseStatus(ok=True, message="写入成功")
    except Exception as exc:
        return SupabaseStatus(ok=False, message=f"写入 {table_name} 失败：{exc}")


def upsert_row(
    client: Any | None,
    table_name: str,
    payload: dict[str, Any],
    *,
    on_conflict: str | None = None,
) -> SupabaseStatus:
    """安全 upsert 单行数据；适合按日期更新每日复盘。"""

    if client is None:
        return SupabaseStatus(ok=False, message="Supabase 未连接，暂时无法写入数据。")

    try:
        query = client.table(table_name).upsert(payload, on_conflict=on_conflict)
        query.execute()
        return SupabaseStatus(ok=True, message="保存成功")
    except Exception as exc:
        return SupabaseStatus(ok=False, message=f"保存 {table_name} 失败：{exc}")
