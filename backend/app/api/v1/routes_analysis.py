"""FastAPI 路由：
  POST /api/v1/analysis/product     — 启动分析任务，返回 run_id
  GET  /api/v1/analysis/stream/{run_id} — SSE 流（进度 + 结果）
  GET  /api/v1/analysis/status/{run_id} — 查询任务状态
  GET  /api/v1/analysis/check-cookie — 检测小红书 Cookie 是否有效
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from app.graph.workflow import run_analysis
from app.models.schemas import AnalysisRequest

router = APIRouter(prefix="/api/v1/analysis", tags=["analysis"])

# run_id -> {"queue": asyncio.Queue, "status": str, "query": str}
_tasks: dict[str, dict] = {}

_QUEUE_TTL = 300  # 任务结果保留秒数


class AnalysisRequestV2(BaseModel):
    query: str = Field(..., min_length=1, max_length=200, description="产品舆情分析关键词")
    session_id: str | None = Field(None, description="可选会话 ID，用于复用或幂等")
    cookie: str | None = Field(None, description="用户提供的小红书 Cookie，覆盖 .env")

    @field_validator("query")
    @classmethod
    def strip_query(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query 不能为空")
        return v


# ---------------------------------------------------------------------------
# POST /product  — 启动分析任务
# ---------------------------------------------------------------------------

@router.post("/product", summary="启动产品舆情分析")
async def start_analysis(req: AnalysisRequestV2):
    """
    发起一次产品舆情分析。
    - 返回 `run_id`，用于后续 SSE 流接入。
    - 若传入相同 `session_id` 且任务仍在运行，返回 409。
    """
    run_id = req.session_id or str(uuid.uuid4())

    if run_id in _tasks and _tasks[run_id]["status"] == "running":
        raise HTTPException(status_code=409, detail="该 session_id 的任务正在执行中")

    q: asyncio.Queue = asyncio.Queue()
    _tasks[run_id] = {"queue": q, "status": "running", "query": req.query}

    asyncio.create_task(_run_and_cleanup(run_id, req.query, q, cookie=req.cookie))
    logger.info(f"[Routes] 任务启动 run_id={run_id} query={req.query}")
    return {"run_id": run_id, "query": req.query}


async def _run_and_cleanup(run_id: str, query: str, q: asyncio.Queue, cookie: str | None = None) -> None:
    try:
        await run_analysis(query, run_id, q, cookie=cookie)
        if run_id in _tasks:
            _tasks[run_id]["status"] = "done"
    except BaseException as e:
        exc_repr = f"[{type(e).__name__}] {e!r}"
        logger.error(f"[Routes] 未捕获异常 run_id={run_id}: {exc_repr}")
        q.put_nowait({"event": "error", "data": {"code": "INTERNAL_ERROR", "message": exc_repr}})
        q.put_nowait(None)
        if run_id in _tasks:
            _tasks[run_id]["status"] = "error"
        if not isinstance(e, Exception):
            raise
    finally:
        await asyncio.sleep(_QUEUE_TTL)
        _tasks.pop(run_id, None)


# ---------------------------------------------------------------------------
# GET /status/{run_id}  — 任务状态查询
# ---------------------------------------------------------------------------

@router.get("/status/{run_id}", summary="查询分析任务状态")
async def get_status(run_id: str):
    if run_id not in _tasks:
        raise HTTPException(status_code=404, detail="run_id 不存在或已过期")
    task = _tasks[run_id]
    return {"run_id": run_id, "status": task["status"], "query": task["query"]}


# ---------------------------------------------------------------------------
# GET /stream/{run_id}  — SSE 流
# ---------------------------------------------------------------------------

@router.get("/stream/{run_id}", summary="消费分析结果 SSE 流")
async def stream_result(run_id: str):
    """
    Server-Sent Events 流。

    事件类型:
    - `progress`: `{stage, message, progress}`
    - `result`:   `{final_answer, confidence_score, clusters, sentiment_summary, ...}`
    - `error`:    `{code, message}`
    """
    if run_id not in _tasks:
        raise HTTPException(status_code=404, detail="run_id 不存在或已过期")

    q: asyncio.Queue = _tasks[run_id]["queue"]

    async def _generator() -> AsyncGenerator[dict, None]:
        # 先发一个 ping 确认连接
        yield {"event": "ping", "data": json.dumps({"run_id": run_id}, ensure_ascii=False)}
        try:
            while True:
                item = await asyncio.wait_for(q.get(), timeout=180)
                if item is None:
                    # 哨兵：流正常结束
                    yield {"event": "done", "data": json.dumps({"run_id": run_id}, ensure_ascii=False)}
                    break
                yield {
                    "event": item["event"],
                    "data": json.dumps(item["data"], ensure_ascii=False),
                }
        except asyncio.TimeoutError:
            logger.warning(f"[SSE] timeout run_id={run_id}")
            yield {
                "event": "error",
                "data": json.dumps({"code": "TIMEOUT", "message": "分析超时，请重试"}, ensure_ascii=False),
            }
        except Exception as e:
            exc_repr = f"[{type(e).__name__}] {e!r}"
            logger.error(f"[SSE] generator error run_id={run_id}: {exc_repr}")
            yield {
                "event": "error",
                "data": json.dumps({"code": "STREAM_ERROR", "message": exc_repr}, ensure_ascii=False),
            }

    return EventSourceResponse(
        _generator(),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
        },
    )


# ---------------------------------------------------------------------------
# GET /check-cookie  — 检测小红书 Cookie 有效性
# ---------------------------------------------------------------------------

@router.get("/check-cookie", summary="检测小红书 Cookie 是否有效")
async def check_cookie(cookie: str | None = Query(None)):
    """
    检测 XHS Cookie 是否有效。
    优先使用 query param `cookie`，否则读 .env XHS_COOKIES。
    返回 {"valid": bool, "source": "param"|"env"|"none"}
    """
    import httpx
    xhs_cookie = cookie or os.getenv("XHS_COOKIES", "")
    if not xhs_cookie:
        return JSONResponse({"valid": False, "source": "none"})
    source = "param" if cookie else "env"
    headers = {
        "cookie": xhs_cookie,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "referer": "https://www.xiaohongshu.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(
                "https://edith.xiaohongshu.com/api/sns/web/v1/user/me",
                headers=headers,
            )
        if resp.status_code == 461:
            return JSONResponse({"valid": False, "source": source})
        data = resp.json()
        valid = data.get("code") == 0
        return JSONResponse({"valid": valid, "source": source})
    except Exception as e:
        logger.warning(f"[check-cookie] 请求失败: {e}")
        return JSONResponse({"valid": False, "source": source})
