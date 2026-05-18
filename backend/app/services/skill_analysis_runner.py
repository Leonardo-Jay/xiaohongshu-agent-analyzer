"""Service-less runner for MCP Skill and CLI-style analysis entrypoints."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from dotenv import load_dotenv

    _BACKEND_DIR = Path(__file__).resolve().parents[2]
    load_dotenv(_BACKEND_DIR / ".env")
except Exception:
    pass

from app.graph.workflow import run_analysis

SkillEventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class SkillAnalysisError(RuntimeError):
    """Raised when the direct Skill analysis workflow reports an error."""

    def __init__(self, code: str, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.run_id = run_id


async def run_skill_analysis(
    *,
    query: str,
    cookie: str | None = None,
    enable_memory: bool | None = None,
    session_id: str | None = None,
    timeout: int = 300,
    on_event: SkillEventCallback | None = None,
) -> dict[str, Any]:
    """Run the full multi-agent workflow without FastAPI, HTTP, or SSE.

    The existing workflow already publishes events into an asyncio queue. This
    runner consumes those events directly and returns a structured result for
    MCP Skills or future CLI integrations.
    """

    clean_query = (query or "").strip()
    if not clean_query:
        raise SkillAnalysisError("INVALID_QUERY", "query cannot be empty")

    run_id = str(uuid.uuid4())
    queue: asyncio.Queue[Any] = asyncio.Queue()
    task = asyncio.create_task(
        run_analysis(
            clean_query,
            run_id,
            queue,
            cookie=cookie,
            enable_memory=enable_memory,
            session_id=session_id,
        )
    )

    progress_log: list[dict[str, Any]] = []
    report_chunks: list[str] = []
    result_data: dict[str, Any] = {}
    error_data: dict[str, Any] | None = None

    try:
        while True:
            item = await asyncio.wait_for(queue.get(), timeout=timeout)
            if item is None:
                break

            if not isinstance(item, dict):
                continue

            if on_event is not None:
                callback_result = on_event(item)
                if inspect.isawaitable(callback_result):
                    await callback_result

            event = item.get("event")
            data = item.get("data") or {}

            if event == "progress":
                progress_log.append(
                    {
                        "stage": data.get("stage", ""),
                        "message": data.get("message", ""),
                        "progress": data.get("progress", 0),
                    }
                )
            elif event == "report_chunk":
                text = data.get("text", "")
                if text:
                    report_chunks.append(text)
            elif event == "result":
                result_data = data
            elif event == "error":
                error_data = {
                    "code": data.get("code", "ANALYSIS_FAILED"),
                    "message": data.get("message", "Unknown analysis error"),
                }

        await task
    except asyncio.TimeoutError as exc:
        task.cancel()
        await _await_cancelled_task(task)
        raise SkillAnalysisError(
            "ANALYSIS_TIMEOUT",
            f"analysis timed out after {timeout} seconds",
            run_id=run_id,
        ) from exc
    except asyncio.CancelledError:
        task.cancel()
        raise
    finally:
        if task.done() and not task.cancelled():
            task_exc = task.exception()
            if task_exc is not None:
                logger.debug(f"[SkillRunner] workflow task ended with exception: {task_exc}")

    if error_data:
        raise SkillAnalysisError(
            error_data["code"],
            error_data["message"],
            run_id=run_id,
        )

    final_answer = result_data.get("final_answer") or "\n".join(report_chunks).strip()
    return {
        "ok": True,
        "run_id": run_id,
        "final_answer": final_answer,
        "report_ir": result_data.get("report_ir", {}),
        "confidence_score": result_data.get("confidence_score", 0.0),
        "clusters": result_data.get("clusters", []),
        "sentiment_summary": result_data.get("sentiment_summary", {}),
        "content_time_analysis": result_data.get("content_time_analysis", {}),
        "screened_count": result_data.get("screened_count", 0),
        "comment_count": result_data.get("comment_count", 0),
        "limitations": result_data.get("limitations", []),
        "intent": result_data.get("intent", "general"),
        "query_plan": result_data.get("query_plan", []),
        "references": result_data.get("references", []),
        "progress_log": progress_log,
    }


async def _await_cancelled_task(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.debug(f"[SkillRunner] cancelled workflow raised: {exc}")
