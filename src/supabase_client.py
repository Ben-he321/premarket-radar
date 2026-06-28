"""Supabase REST 客户端封装：轻量读写数据库，避免 supabase-py 重依赖。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

import requests


@dataclass(frozen=True)
class SupabaseStatus:
    """Supabase 连接状态，页面只展示状态和原因，不展示密钥。"""

    ok: bool
    message: str


@dataclass(frozen=True)
class SupabaseRestClient:
    """只保存 Supabase REST 调用所需配置，不引入 supabase-py。"""

    url: str
    key: str

    @property
    def rest_url(self) -> str:
        """Supabase PostgREST 基础地址。"""

        return f"{self.url.rstrip('/')}/rest/v1"

    @property
    def headers(self) -> dict[str, str]:
        """统一请求头；不要在页面或日志中打印这些内容。"""

        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }


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


def get_supabase_client(secrets: object | None = None) -> tuple[SupabaseRestClient | None, SupabaseStatus]:
    """创建轻量 Supabase REST 客户端；缺配置时友好降级。"""

    url, key = get_supabase_config(secrets)
    if not url or not key:
        return None, SupabaseStatus(
            ok=False,
            message="未检测到 Supabase 配置，请在 Streamlit Secrets 配置 SUPABASE_URL 和 SUPABASE_KEY。",
        )

    return SupabaseRestClient(url=url, key=key), SupabaseStatus(ok=True, message="Supabase REST 配置已加载")


def _parse_error(response: requests.Response) -> str:
    """尽量提取 Supabase 返回的错误原因。"""

    try:
        payload = response.json()
    except ValueError:
        return response.text[:300] or f"HTTP {response.status_code}"

    if isinstance(payload, dict):
        for key in ("message", "details", "hint", "code"):
            value = payload.get(key)
            if value:
                return str(value)
    return str(payload)[:300]


def fetch_rows(
    client: SupabaseRestClient | None,
    table_name: str,
    *,
    order_by: str | None = None,
    desc: bool = True,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], SupabaseStatus]:
    """安全读取表数据；失败时返回空列表和中文错误。"""

    if client is None:
        return [], SupabaseStatus(ok=False, message="Supabase 未连接，暂时无法读取数据。")

    params: dict[str, str | int] = {"select": "*"}
    if order_by:
        direction = "desc" if desc else "asc"
        params["order"] = f"{order_by}.{direction}.nullslast"
    if limit:
        params["limit"] = limit

    try:
        response = requests.get(
            f"{client.rest_url}/{table_name}",
            headers=client.headers,
            params=params,
            timeout=15,
        )
        if not response.ok:
            return [], SupabaseStatus(ok=False, message=f"读取 {table_name} 失败：{_parse_error(response)}")
        data = response.json()
        return (data if isinstance(data, list) else []), SupabaseStatus(ok=True, message="读取成功")
    except requests.RequestException as exc:
        return [], SupabaseStatus(ok=False, message=f"读取 {table_name} 失败：{exc}")
    except ValueError as exc:
        return [], SupabaseStatus(ok=False, message=f"读取 {table_name} 失败：返回数据格式异常：{exc}")


def insert_row(client: SupabaseRestClient | None, table_name: str, payload: dict[str, Any]) -> SupabaseStatus:
    """安全写入单行数据；失败时返回中文错误。"""

    if client is None:
        return SupabaseStatus(ok=False, message="Supabase 未连接，暂时无法写入数据。")

    headers = {**client.headers, "Prefer": "return=representation"}
    try:
        response = requests.post(
            f"{client.rest_url}/{table_name}",
            headers=headers,
            json=payload,
            timeout=15,
        )
        if not response.ok:
            return SupabaseStatus(ok=False, message=f"写入 {table_name} 失败：{_parse_error(response)}")
        return SupabaseStatus(ok=True, message="写入成功")
    except requests.RequestException as exc:
        return SupabaseStatus(ok=False, message=f"写入 {table_name} 失败：{exc}")


def upsert_row(
    client: SupabaseRestClient | None,
    table_name: str,
    payload: dict[str, Any],
    *,
    on_conflict: str | None = None,
) -> SupabaseStatus:
    """安全 upsert 单行数据；适合按日期更新每日复盘。"""

    if client is None:
        return SupabaseStatus(ok=False, message="Supabase 未连接，暂时无法写入数据。")

    headers = {**client.headers, "Prefer": "resolution=merge-duplicates,return=representation"}
    params = {"on_conflict": on_conflict} if on_conflict else None
    try:
        response = requests.post(
            f"{client.rest_url}/{table_name}",
            headers=headers,
            params=params,
            json=payload,
            timeout=15,
        )
        if not response.ok:
            return SupabaseStatus(ok=False, message=f"保存 {table_name} 失败：{_parse_error(response)}")
        return SupabaseStatus(ok=True, message="保存成功")
    except requests.RequestException as exc:
        return SupabaseStatus(ok=False, message=f"保存 {table_name} 失败：{exc}")
