"""Search-link replay support for XHS analysis tests.

`cookie=-2` means: skip live XHS search and replay previously captured
`search_posts` links, while keeping later detail/screen/analyze/report stages real.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPLAY_DIR = _BACKEND_ROOT / "data" / "replay" / "xhs_search_posts"
DEFAULT_FIXTURE_NAME = "default"
_FIXTURE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class XhsSearchReplayError(RuntimeError):
    """Raised when search-link replay cannot produce usable post links."""

    def __init__(self, code: str, message: str, *, fixture_name: str = "", path: Path | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fixture_name = fixture_name
        self.path = path


def _mode_value(cookie: str | None) -> str:
    return (cookie if cookie is not None else os.getenv("XHS_COOKIES", "")).strip()


def is_xhs_search_replay_cookie(cookie: str | None) -> bool:
    return _mode_value(cookie) == "-2"


def is_xhs_full_mock_cookie(cookie: str | None) -> bool:
    return _mode_value(cookie) == "-1"


def selected_replay_fixture_name(fixture_name: str | None = None) -> str:
    raw = (fixture_name or os.getenv("XHS_REPLAY_FIXTURE") or DEFAULT_FIXTURE_NAME).strip()
    if raw.endswith(".json"):
        raw = raw[:-5]
    if not raw or "/" in raw or "\\" in raw or not _FIXTURE_NAME_RE.match(raw):
        raise XhsSearchReplayError(
            "REPLAY_FIXTURE_INVALID_NAME",
            f"回放 fixture 名称无效: {raw!r}",
            fixture_name=raw,
        )
    return raw


def replay_fixture_path(
    fixture_name: str | None = None,
    *,
    base_dir: Path | str | None = None,
) -> Path:
    name = selected_replay_fixture_name(fixture_name)
    root = Path(base_dir) if base_dir is not None else DEFAULT_REPLAY_DIR
    return root / f"{name}.json"


def extract_note_id(note_url: str) -> str:
    parsed = urlparse(note_url)
    parts = [part for part in parsed.path.split("/") if part]
    if "explore" in parts:
        index = parts.index("explore")
        if index + 1 < len(parts):
            return parts[index + 1]
    return parts[-1] if parts else ""


def _coerce_count(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _normalize_post(raw_post: dict[str, Any], *, fixture_name: str, index: int) -> dict[str, Any]:
    note_url = str(raw_post.get("note_url") or "").strip()
    if not note_url:
        raise XhsSearchReplayError(
            "REPLAY_FIXTURE_MISSING_NOTE_URL",
            f"回放 fixture 第 {index + 1} 条缺少 note_url",
            fixture_name=fixture_name,
        )
    parsed = urlparse(note_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise XhsSearchReplayError(
            "REPLAY_FIXTURE_MISSING_NOTE_URL",
            f"回放 fixture 第 {index + 1} 条 note_url 不是完整 URL: {note_url!r}",
            fixture_name=fixture_name,
        )
    if "xiaohongshu.com" not in parsed.netloc:
        raise XhsSearchReplayError(
            "REPLAY_FIXTURE_MISSING_NOTE_URL",
            f"回放 fixture 第 {index + 1} 条 note_url 不是小红书链接: {note_url!r}",
            fixture_name=fixture_name,
        )

    note_id = str(raw_post.get("note_id") or "").strip() or extract_note_id(note_url)
    title = str(raw_post.get("title") or "").strip()
    desc = str(raw_post.get("desc") or "").strip()
    display_title = str(raw_post.get("display_title") or "").strip()
    if not display_title:
        display_title = title[:20] if title else (note_id or f"历史帖子 {index + 1}")

    return {
        "note_id": note_id or f"replay_{index:03d}",
        "note_url": note_url,
        "title": title,
        "desc": desc,
        "upload_time": str(raw_post.get("upload_time") or ""),
        "published_at": str(raw_post.get("published_at") or ""),
        "like_count": _coerce_count(raw_post.get("like_count")),
        "comment_count": _coerce_count(raw_post.get("comment_count")),
        "collected_count": _coerce_count(raw_post.get("collected_count")),
        "display_title": display_title,
        "sort_type_used": _coerce_count(raw_post.get("sort_type_used")),
        "replay_mode": True,
        "replay_fixture": fixture_name,
    }


def load_xhs_search_replay_fixture(
    fixture_name: str | None = None,
    *,
    base_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Load and normalize replay posts from a fixture JSON file."""
    name = selected_replay_fixture_name(fixture_name)
    path = replay_fixture_path(name, base_dir=base_dir)
    if not path.exists():
        raise XhsSearchReplayError(
            "REPLAY_FIXTURE_NOT_FOUND",
            f"找不到搜索链接回放 fixture: {path}",
            fixture_name=name,
            path=path,
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise XhsSearchReplayError(
            "REPLAY_FIXTURE_INVALID_JSON",
            f"搜索链接回放 fixture 不是合法 JSON: {path}; {exc}",
            fixture_name=name,
            path=path,
        ) from exc

    posts_raw = data.get("posts") if isinstance(data, dict) else None
    if not isinstance(posts_raw, list) or not posts_raw:
        raise XhsSearchReplayError(
            "REPLAY_FIXTURE_EMPTY",
            f"搜索链接回放 fixture 没有可用 posts: {path}",
            fixture_name=name,
            path=path,
        )

    posts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(posts_raw):
        if not isinstance(item, dict):
            raise XhsSearchReplayError(
                "REPLAY_FIXTURE_INVALID_JSON",
                f"搜索链接回放 fixture 第 {index + 1} 条不是对象",
                fixture_name=name,
                path=path,
            )
        post = _normalize_post(item, fixture_name=name, index=index)
        dedupe_key = post.get("note_id") or post["note_url"]
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        posts.append(post)

    if not posts:
        raise XhsSearchReplayError(
            "REPLAY_FIXTURE_EMPTY",
            f"搜索链接回放 fixture 去重后没有可用 posts: {path}",
            fixture_name=name,
            path=path,
        )

    return {
        "name": str(data.get("name") or name),
        "fixture_name": name,
        "path": str(path),
        "description": str(data.get("description") or ""),
        "source_query": str(data.get("source_query") or ""),
        "captured_at": str(data.get("captured_at") or ""),
        "posts": posts,
        "post_count": len(posts),
    }

