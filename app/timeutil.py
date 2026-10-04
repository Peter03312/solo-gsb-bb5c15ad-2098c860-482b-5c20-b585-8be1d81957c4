"""UTC 时间解析与规整。

对外只接受带时区信息的 ISO 8601 时间（结尾可用 Z）；内部统一转成 UTC，
并格式化为定宽字符串，保证按字符串排序即按时间排序（SQLite 不依赖扩展）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from .errors import ApiError

CANONICAL_FMT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


def parse_utc(value: object, field: str = "time") -> datetime:
    """把输入解析为 UTC datetime；拒绝缺省时区（naive）的时间。"""
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "INVALID_REQUEST", f"字段 {field} 必须是非空 ISO 8601 时间字符串")
    text = value.strip()
    try:
        # Python 3.11 原生支持 Z，这里兼容更早版本
        dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        raise ApiError(400, "INVALID_REQUEST", f"字段 {field} 不是合法 ISO 8601 时间: {value!r}")
    if dt.tzinfo is None:
        raise ApiError(400, "INVALID_REQUEST", f"字段 {field} 必须携带时区信息（推荐 UTC，如 ...Z）")
    return dt.astimezone(timezone.utc)


def canonical(dt: datetime) -> str:
    """时区感知时间 -> 定宽 UTC 字符串。"""
    return dt.astimezone(timezone.utc).strftime(CANONICAL_FMT)


def now_canonical() -> str:
    """服务端当前 UTC 时间（发布时间只允许由这里产生）。"""
    return canonical(datetime.now(timezone.utc))
