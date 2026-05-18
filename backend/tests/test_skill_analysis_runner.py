import asyncio

import pytest

from app.services import skill_analysis_runner as runner


def test_run_skill_analysis_collects_progress_and_result(monkeypatch):
    async def fake_run_analysis(query, run_id, queue, cookie=None, enable_memory=None, session_id=None):
        queue.put_nowait(
            {
                "event": "progress",
                "data": {"stage": "retrieve", "message": "retrieving", "progress": 40},
            }
        )
        queue.put_nowait(
            {
                "event": "result",
                "data": {
                    "final_answer": "# Report",
                    "report_ir": {"title": "Report"},
                    "confidence_score": 0.8,
                    "references": [{"id": "ref-1"}],
                },
            }
        )
        queue.put_nowait(None)

    monkeypatch.setattr(runner, "run_analysis", fake_run_analysis)

    result = asyncio.run(runner.run_skill_analysis(query="nova6", cookie="-1", timeout=1))

    assert result["ok"] is True
    assert result["final_answer"] == "# Report"
    assert result["report_ir"] == {"title": "Report"}
    assert result["confidence_score"] == 0.8
    assert result["references"] == [{"id": "ref-1"}]
    assert result["progress_log"] == [
        {"stage": "retrieve", "message": "retrieving", "progress": 40}
    ]


def test_run_skill_analysis_turns_error_event_into_exception(monkeypatch):
    async def fake_run_analysis(query, run_id, queue, cookie=None, enable_memory=None, session_id=None):
        queue.put_nowait(
            {
                "event": "error",
                "data": {"code": "COOKIE_EXPIRED", "message": "expired"},
            }
        )
        queue.put_nowait(None)

    monkeypatch.setattr(runner, "run_analysis", fake_run_analysis)

    with pytest.raises(runner.SkillAnalysisError) as exc_info:
        asyncio.run(runner.run_skill_analysis(query="nova6", cookie="bad", timeout=1))

    assert exc_info.value.code == "COOKIE_EXPIRED"
    assert exc_info.value.message == "expired"
