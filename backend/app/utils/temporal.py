"""Small temporal helpers shared across agents."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    _TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    _TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

_DEFAULT_TEMPORAL_CONTEXT = {
    "mode": "evergreen",
    "window": {
        "kind": "none",
        "start_date": "",
        "end_date": "",
        "label": "不限时间",
    },
    "retrieval_policy": "balanced",
    "content_time_analysis": "auto",
    "reason": "默认日常口碑分析",
}

_VALID_MODES = {"evergreen", "recent", "specific_range", "historical", "change_check"}
_VALID_WINDOW_KINDS = {"none", "relative", "absolute"}
_VALID_RETRIEVAL_POLICIES = {"balanced", "latest_first", "comment_hot", "like_hot"}
_VALID_CONTENT_TIME = {"auto", "required", "skip"}


def default_temporal_context(reason: str | None = None) -> dict[str, Any]:
    context = {
        "mode": _DEFAULT_TEMPORAL_CONTEXT["mode"],
        "window": dict(_DEFAULT_TEMPORAL_CONTEXT["window"]),
        "retrieval_policy": _DEFAULT_TEMPORAL_CONTEXT["retrieval_policy"],
        "content_time_analysis": _DEFAULT_TEMPORAL_CONTEXT["content_time_analysis"],
        "reason": reason or _DEFAULT_TEMPORAL_CONTEXT["reason"],
    }
    return context


def normalize_temporal_context(raw: Any, query: str = "", current_time: dict[str, Any] | None = None) -> dict[str, Any]:
    inferred = infer_temporal_context(query, current_time=current_time)
    if not isinstance(raw, dict):
        return inferred

    mode = str(raw.get("mode") or inferred["mode"]).strip()
    if mode not in _VALID_MODES:
        mode = inferred["mode"]

    raw_window = raw.get("window") if isinstance(raw.get("window"), dict) else {}
    window = {
        "kind": str(raw_window.get("kind") or inferred["window"]["kind"]).strip(),
        "start_date": str(raw_window.get("start_date") or inferred["window"]["start_date"]).strip(),
        "end_date": str(raw_window.get("end_date") or inferred["window"]["end_date"]).strip(),
        "label": str(raw_window.get("label") or inferred["window"]["label"]).strip() or "不限时间",
    }
    if window["kind"] not in _VALID_WINDOW_KINDS:
        window["kind"] = inferred["window"]["kind"]
    if window["kind"] == "none" and inferred["window"]["kind"] != "none" and mode != "evergreen":
        window = dict(inferred["window"])
    if window["kind"] == "none":
        window["start_date"] = ""
        window["end_date"] = ""
        window["label"] = window["label"] or "不限时间"

    retrieval_policy = str(raw.get("retrieval_policy") or inferred["retrieval_policy"]).strip()
    if retrieval_policy not in _VALID_RETRIEVAL_POLICIES:
        retrieval_policy = inferred["retrieval_policy"]

    content_time_analysis = str(raw.get("content_time_analysis") or inferred["content_time_analysis"]).strip()
    if content_time_analysis not in _VALID_CONTENT_TIME:
        content_time_analysis = inferred["content_time_analysis"]

    reason = str(raw.get("reason") or inferred["reason"]).strip()

    return {
        "mode": mode,
        "window": window,
        "retrieval_policy": retrieval_policy,
        "content_time_analysis": content_time_analysis,
        "reason": reason,
    }


def infer_temporal_context(query: str, current_time: dict[str, Any] | None = None) -> dict[str, Any]:
    query = str(query or "")
    today = _current_date(current_time)

    if any(word in query for word in ("今天", "今日")):
        return _relative_context(today, 0, "今天", "recent", "latest_first")
    if "昨天" in query:
        yesterday = today - timedelta(days=1)
        return _absolute_context(yesterday, yesterday, "昨天", "recent", "latest_first")
    if any(word in query for word in ("变化", "变了", "对比以前", "现在还", "还值得", "还好吗")):
        return _relative_context(today, 90, "近90天", "change_check", "latest_first")
    if any(word in query for word in ("近半年", "半年内", "最近半年")):
        return _relative_context(today, 180, "近半年", "recent", "latest_first")
    if any(word in query for word in ("近一年", "一年内", "最近一年")):
        return _relative_context(today, 365, "近一年", "recent", "balanced")
    if any(word in query for word in ("最近", "近期", "近来", "目前", "这段时间")):
        return _relative_context(today, 30, "近30天", "recent", "latest_first")
    if any(word in query for word in ("以前", "过去", "历史", "早期", "发布初期", "上市初期")):
        return {
            "mode": "historical",
            "window": {"kind": "none", "start_date": "", "end_date": "", "label": "历史内容"},
            "retrieval_policy": "comment_hot",
            "content_time_analysis": "auto",
            "reason": "用户关注历史或早期评价，优先检索高讨论内容并保留内容顺序",
        }
    year_match = re.search(r"(20\d{2})\s*年", query)
    if year_match:
        year = int(year_match.group(1))
        return _absolute_context(date(year, 1, 1), date(year, 12, 31), f"{year}年", "specific_range", "comment_hot")

    return default_temporal_context()


def parse_xhs_time(value: Any, current_time: dict[str, Any] | None = None) -> tuple[str, str, bool]:
    """Return (date_iso, raw_text, parsed)."""
    raw = str(value or "").strip()
    if not raw:
        return "", "", False
    today = _current_date(current_time)

    if isinstance(value, (int, float)) or raw.isdigit():
        num = int(float(value))
        if num > 10_000_000_000:
            num = num // 1000
        try:
            dt = datetime.fromtimestamp(num, tz=_TZ)
            return dt.date().isoformat(), raw, True
        except Exception:
            pass

    normalized = raw.replace("/", "-").replace(".", "-")
    match = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", normalized)
    if match:
        parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return parsed.isoformat(), raw, True

    match = re.search(r"(\d{1,2})-(\d{1,2})", normalized)
    if match:
        parsed = date(today.year, int(match.group(1)), int(match.group(2)))
        return parsed.isoformat(), raw, True

    if "刚刚" in raw or "分钟前" in raw or "小时前" in raw or "今天" in raw:
        return today.isoformat(), raw, True
    if "昨天" in raw:
        return (today - timedelta(days=1)).isoformat(), raw, True

    match = re.search(r"(\d+)\s*天前", raw)
    if match:
        return (today - timedelta(days=int(match.group(1)))).isoformat(), raw, True

    match = re.search(r"(\d+)\s*月前", raw)
    if match:
        return (today - timedelta(days=int(match.group(1)) * 30)).isoformat(), raw, True

    return "", raw, False


def within_window(date_text: str, temporal_context: dict[str, Any]) -> bool:
    if not date_text:
        return False
    window = temporal_context.get("window") or {}
    if window.get("kind") == "none":
        return True
    start = window.get("start_date") or ""
    end = window.get("end_date") or ""
    try:
        value = date.fromisoformat(date_text[:10])
        if start and value < date.fromisoformat(start):
            return False
        if end and value > date.fromisoformat(end):
            return False
        return True
    except Exception:
        return False


def _current_date(current_time: dict[str, Any] | None) -> date:
    raw = (current_time or {}).get("date") or (current_time or {}).get("now_iso", "")[:10]
    try:
        return date.fromisoformat(str(raw))
    except Exception:
        return datetime.now(_TZ).date()


def _relative_context(today: date, days: int, label: str, mode: str, policy: str) -> dict[str, Any]:
    start = today if days == 0 else today - timedelta(days=days)
    return _absolute_context(start, today, label, mode, policy, kind="relative")


def _absolute_context(start: date, end: date, label: str, mode: str, policy: str, kind: str = "absolute") -> dict[str, Any]:
    return {
        "mode": mode,
        "window": {
            "kind": kind,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "label": label,
        },
        "retrieval_policy": policy,
        "content_time_analysis": "auto",
        "reason": f"用户关注{label}内容，需要保留时间顺序并调整检索排序",
    }
