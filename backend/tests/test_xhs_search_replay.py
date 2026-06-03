import asyncio
import json

import pytest

from app.api.v1.routes_analysis import check_cookie
from app.graph import workflow
from app.tools.xhs_search_replay import (
    XhsSearchReplayError,
    is_xhs_full_mock_cookie,
    is_xhs_search_replay_cookie,
    load_xhs_search_replay_fixture,
)


def run_async(coro):
    return asyncio.run(coro)


def _write_fixture(path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _collect_queue(queue: asyncio.Queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def test_replay_cookie_helpers_distinguish_full_mock_and_search_replay(monkeypatch):
    monkeypatch.delenv("XHS_COOKIES", raising=False)

    assert is_xhs_search_replay_cookie("-2") is True
    assert is_xhs_search_replay_cookie("-1") is False
    assert is_xhs_full_mock_cookie("-1") is True
    assert is_xhs_full_mock_cookie("-2") is False

    monkeypatch.setenv("XHS_COOKIES", "-2")
    assert is_xhs_search_replay_cookie(None) is True


def test_load_xhs_search_replay_fixture_normalizes_and_dedupes_posts(tmp_path):
    _write_fixture(
        tmp_path / "default.json",
        json.dumps(
            {
                "name": "default",
                "source_query": "Claude Opus",
                "posts": [
                    {
                        "note_url": "https://www.xiaohongshu.com/explore/note123?xsec_token=abc&xsec_source=pc_search",
                        "title": "真实帖子",
                        "like_count": "12",
                    },
                    {
                        "note_id": "note123",
                        "note_url": "https://www.xiaohongshu.com/explore/note123?xsec_token=abc&xsec_source=pc_search",
                    },
                ],
            },
            ensure_ascii=False,
        ),
    )

    fixture = load_xhs_search_replay_fixture(base_dir=tmp_path)

    assert fixture["fixture_name"] == "default"
    assert fixture["source_query"] == "Claude Opus"
    assert len(fixture["posts"]) == 1
    post = fixture["posts"][0]
    assert post["note_id"] == "note123"
    assert post["replay_mode"] is True
    assert post["replay_fixture"] == "default"
    assert post["sort_type_used"] == 0
    assert post["like_count"] == 12


@pytest.mark.parametrize(
    ("filename", "body", "code"),
    [
        ("default.json", "{", "REPLAY_FIXTURE_INVALID_JSON"),
        ("default.json", json.dumps({"posts": []}), "REPLAY_FIXTURE_EMPTY"),
        ("default.json", json.dumps({"posts": [{}]}), "REPLAY_FIXTURE_MISSING_NOTE_URL"),
    ],
)
def test_load_xhs_search_replay_fixture_rejects_invalid_fixture(tmp_path, filename, body, code):
    _write_fixture(tmp_path / filename, body)

    with pytest.raises(XhsSearchReplayError) as exc:
        load_xhs_search_replay_fixture(base_dir=tmp_path)

    assert exc.value.code == code


def test_load_xhs_search_replay_fixture_rejects_missing_file(tmp_path):
    with pytest.raises(XhsSearchReplayError) as exc:
        load_xhs_search_replay_fixture(base_dir=tmp_path)

    assert exc.value.code == "REPLAY_FIXTURE_NOT_FOUND"


def test_check_cookie_reports_replay_modes():
    replay = run_async(check_cookie(cookie="-2"))
    full_mock = run_async(check_cookie(cookie="-1"))

    replay_data = json.loads(replay.body)
    full_mock_data = json.loads(full_mock.body)

    assert replay_data == {"valid": True, "source": "param", "mode": "search_replay"}
    assert full_mock_data == {"valid": True, "source": "param", "mode": "full_mock"}


class _FakeApp:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def ainvoke(self, state, config=None):
        self.calls.append((state, config))
        result = self.handler(state, config)
        if asyncio.iscoroutine(result):
            return await result
        return result


async def _run_replay_workflow(monkeypatch, *, apihz_configured=True, apihz_details=None):
    fixture = {
        "fixture_name": "default",
        "path": "fixture/default.json",
        "source_query": "Claude Opus",
        "captured_at": "2026-05-30T10:28:21+08:00",
        "posts": [
            {
                "note_id": "note123",
                "note_url": "https://www.xiaohongshu.com/explore/note123?xsec_token=abc&xsec_source=pc_search",
                "title": "",
                "desc": "",
                "sort_type_used": 0,
                "replay_mode": True,
                "replay_fixture": "default",
            }
        ],
    }
    screen_states = []
    analyze_configs = []

    class ExplodingPool:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Replay mode must not create XhsMcpClientPool")

    async def fake_fetch_details(note_urls):
        return apihz_details if apihz_details is not None else [
            {"note_id": "note123", "title": "真实标题", "desc": "这是一段由 apihz 返回的真实帖子正文。"}
        ]

    monkeypatch.setattr(workflow, "XhsMcpClientPool", ExplodingPool)
    monkeypatch.setattr(workflow, "load_xhs_search_replay_fixture", lambda: fixture)
    monkeypatch.setattr(workflow, "is_apihz_configured", lambda: apihz_configured)
    monkeypatch.setattr(workflow, "fetch_posts_detail_batch", fake_fetch_details)
    monkeypatch.setattr(
        workflow,
        "_orchestrator_app",
        _FakeApp(lambda state, config: {
            "intent": "user_experience",
            "intent_confidence": 0.9,
            "product_entities": ["Claude Opus"],
            "aliases": [],
            "key_aspects": [{"aspect": "能力表现"}],
            "user_needs": ["了解真实评价"],
            "search_context": {},
            "temporal_context": {},
            "current_time": {},
            "intent_analysis_score": 1.0,
        }),
    )
    monkeypatch.setattr(
        workflow,
        "_screen_app",
        _FakeApp(lambda state, config: screen_states.append(state.copy()) or {
            "screened_items": state["retrieved_posts"],
            "screening_stats": {},
        }),
    )
    monkeypatch.setattr(
        workflow,
        "_analyze_app",
        _FakeApp(lambda state, config: analyze_configs.append(config) or {
            "retrieved_comments": [{"comment_id": "__post_body__note123", "content": state["screened_items"][0]["desc"]}],
            "clusters": [{
                "topic": "能力反馈",
                "sentiment": "中立",
                "count": 1,
                "evidence_quotes": [state["screened_items"][0]["desc"]],
            }],
            "sentiment_summary": {"中立": 1},
            "confidence_score": 0.8,
        }),
    )
    monkeypatch.setattr(
        workflow,
        "_synthesis_app",
        _FakeApp(lambda state, config: {
            "final_answer": "# 回放报告",
            "report_ir": {},
            "confidence_score": 0.8,
        }),
    )

    queue: asyncio.Queue = asyncio.Queue()
    await workflow.run_analysis(
        "大家对 Claude Opus 能力怎么看",
        "run-replay",
        queue,
        cookie="-2",
        enable_memory=None,
        session_id="s1",
    )
    return _collect_queue(queue), screen_states, analyze_configs


def test_replay_workflow_skips_mcp_forces_api_type_and_uses_apihz(monkeypatch):
    items, screen_states, analyze_configs = run_async(_run_replay_workflow(monkeypatch))

    progress_messages = [
        item["data"]["message"]
        for item in items
        if isinstance(item, dict) and item.get("event") == "progress"
    ]
    result_events = [item for item in items if isinstance(item, dict) and item.get("event") == "result"]

    assert any("历史搜索链接回放" in message for message in progress_messages)
    assert screen_states
    assert screen_states[0]["_api_type"] == 1
    assert screen_states[0]["retrieved_posts"][0]["desc"] == "这是一段由 apihz 返回的真实帖子正文。"
    assert analyze_configs[0]["configurable"]["api_type"] == 1
    assert result_events[0]["data"]["final_answer"] == "# 回放报告"


def test_replay_workflow_errors_when_apihz_is_not_configured(monkeypatch):
    items, screen_states, _ = run_async(
        _run_replay_workflow(monkeypatch, apihz_configured=False)
    )

    error_events = [item for item in items if isinstance(item, dict) and item.get("event") == "error"]

    assert not screen_states
    assert error_events[0]["data"]["code"] == "APIHZ_NOT_CONFIGURED_FOR_REPLAY"


def test_replay_workflow_errors_when_apihz_returns_no_details(monkeypatch):
    items, screen_states, _ = run_async(
        _run_replay_workflow(monkeypatch, apihz_details=[])
    )

    error_events = [item for item in items if isinstance(item, dict) and item.get("event") == "error"]

    assert not screen_states
    assert error_events[0]["data"]["code"] == "APIHZ_DETAIL_EMPTY_FOR_REPLAY"

