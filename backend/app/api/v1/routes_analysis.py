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
import time
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from app.graph.workflow import run_analysis
from app.models.schemas import AnalysisRequest
from app.utils.daily_audit_log import append_audit_log

router = APIRouter(prefix="/api/v1/analysis", tags=["analysis"])

# run_id -> {"queue": asyncio.Queue, "status": str, "query": str, "task": asyncio.Task, "ip": str, "started_at": float}
_tasks: dict[str, dict] = {}

_QUEUE_TTL = 300  # 任务结果保留秒数


class AnalysisRequestV2(BaseModel):
    query: str = Field(..., min_length=1, max_length=200, description="产品舆情分析关键词")
    session_id: str | None = Field(None, description="可选会话 ID，用于复用或幂等")
    cookie: str | None = Field(None, description="用户提供的小红书 Cookie，覆盖 .env")
    enable_memory: bool | None = Field(None, description="是否开启记忆功能，覆盖环境变量 ENABLE_MEMORY")

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
async def start_analysis(req: AnalysisRequestV2, request: Request):
    """
    发起一次产品舆情分析。
    - 返回 `run_id`，用于后续 SSE 流接入。
    - 若传入相同 `session_id` 且任务仍在运行，返回 409。
    """
    run_id = req.session_id or str(uuid.uuid4())
    client_ip = request.client.host if request.client else "unknown"

    if run_id in _tasks and _tasks[run_id]["status"] == "running":
        raise HTTPException(status_code=409, detail="该 session_id 的任务正在执行中")

    q: asyncio.Queue = asyncio.Queue()
    _tasks[run_id] = {
        "queue": q,
        "status": "running",
        "query": req.query,
        "task": None,
        "ip": client_ip,
        "started_at": time.time(),
    }
    task = asyncio.create_task(_run_and_cleanup(
        run_id,
        req.query,
        q,
        cookie=req.cookie,
        enable_memory=req.enable_memory
    ))
    _tasks[run_id]["task"] = task
    logger.info(f"[Routes] 任务启动 run_id={run_id} query={req.query}")
    append_audit_log(
        "analysis_started",
        run_id=run_id,
        ip=client_ip,
        query=req.query,
        session_id=req.session_id,
        has_cookie_override=bool(req.cookie and req.cookie.strip()),
    )
    return {"run_id": run_id, "query": req.query}


async def _run_and_cleanup(
    run_id: str,
    query: str,
    q: asyncio.Queue,
    cookie: str | None = None,
    enable_memory: bool | None = None
) -> None:
    cancelled = False
    try:
        await run_analysis(query, run_id, q, cookie=cookie, enable_memory=enable_memory)
        if run_id in _tasks:
            _tasks[run_id]["status"] = "done"
            append_audit_log(
                "analysis_finished",
                run_id=run_id,
                ip=_tasks[run_id].get("ip", "unknown"),
                query=query,
                status="done",
                duration_seconds=round(time.time() - _tasks[run_id].get("started_at", time.time()), 2),
            )
    except BaseException as e:
        if isinstance(e, asyncio.CancelledError):
            cancelled = True
            logger.info(f"[Routes] 任务被取消 run_id={run_id}")
            q.put_nowait({"event": "error", "data": {"code": "CANCELLED", "message": "任务已取消"}})
            q.put_nowait(None)
            if run_id in _tasks:
                _tasks[run_id]["status"] = "cancelled"
                append_audit_log(
                    "analysis_cancelled",
                    run_id=run_id,
                    ip=_tasks[run_id].get("ip", "unknown"),
                    query=query,
                    status="cancelled",
                    duration_seconds=round(time.time() - _tasks[run_id].get("started_at", time.time()), 2),
                )
            raise
        exc_repr = f"[{type(e).__name__}] {e!r}"
        logger.error(f"[Routes] 未捕获异常 run_id={run_id}: {exc_repr}")
        q.put_nowait({"event": "error", "data": {"code": "INTERNAL_ERROR", "message": exc_repr}})
        q.put_nowait(None)
        if run_id in _tasks:
            _tasks[run_id]["status"] = "error"
            append_audit_log(
                "analysis_failed",
                run_id=run_id,
                ip=_tasks[run_id].get("ip", "unknown"),
                query=query,
                status="error",
                duration_seconds=round(time.time() - _tasks[run_id].get("started_at", time.time()), 2),
                error_message=exc_repr,
            )
        if not isinstance(e, Exception):
            raise
    finally:
        # 如果是取消，立即清理；否则等待 TTL
        if cancelled:
            _tasks.pop(run_id, None)
            logger.info(f"[Routes] 任务已取消并清理 run_id={run_id}")
        else:
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
# DELETE /cancel/{run_id}  — 取消任务
# ---------------------------------------------------------------------------

@router.delete("/cancel/{run_id}", summary="取消分析任务")
async def cancel_analysis(run_id: str, request: Request):
    if run_id not in _tasks:
        return {"cancelled": False}
    entry = _tasks[run_id]
    t = entry.get("task")
    if t and not t.done():
        t.cancel()
    entry["status"] = "cancelled"
    entry["queue"].put_nowait(None)
    append_audit_log(
        "analysis_cancelled",
        run_id=run_id,
        ip=(request.client.host if request.client else entry.get("ip", "unknown")),
        query=entry.get("query", ""),
        status="cancelled",
        duration_seconds=round(time.time() - entry.get("started_at", time.time()), 2),
    )
    return {"cancelled": True}


# ---------------------------------------------------------------------------
# GET /stream/{run_id}  — SSE 流
# ---------------------------------------------------------------------------

@router.get("/stream/{run_id}", summary="消费分析结果 SSE 流")
async def stream_result(run_id: str, request: Request):
    """
    Server-Sent Events 流。

    事件类型:
    - `progress`: `{stage, message, progress}`
    - `result`:   `{final_answer, confidence_score, clusters, sentiment_summary, ...}`
    - `error`:    `{code, message}`
    """
    if run_id not in _tasks:
        raise HTTPException(status_code=404, detail="run_id 不存在或已过期")

    append_audit_log(
        "analysis_stream_opened",
        run_id=run_id,
        ip=(request.client.host if request.client else _tasks[run_id].get("ip", "unknown")),
        query=_tasks[run_id].get("query", ""),
        status=_tasks[run_id].get("status", "unknown"),
    )

    q: asyncio.Queue = _tasks[run_id]["queue"]

    async def _generator() -> AsyncGenerator[dict, None]:
        # 先发一个 ping 确认连接
        yield {"event": "ping", "data": json.dumps({"run_id": run_id}, ensure_ascii=False)}
        try:
            while True:
                # 检查客户端是否断开连接
                if await request.is_disconnected():
                    logger.warning(f"[SSE] 客户端断开连接 run_id={run_id}")
                    # 取消后台任务
                    if run_id in _tasks:
                        task = _tasks[run_id].get("task")
                        if task and not task.done():
                            logger.info(f"[SSE] 取消后台任务 run_id={run_id}")
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                    break

                item = await asyncio.wait_for(q.get(), timeout=600)
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
    通过解析 Cookie 字段判断有效性（检查 web_session 和 a1 是否存在）。
    返回 {"valid": bool, "source": "param"|"env"|"none"}
    """
    xhs_cookie = cookie or os.getenv("XHS_COOKIES", "")
    if not xhs_cookie or not xhs_cookie.strip():
        return JSONResponse({"valid": False, "source": "none"})
    source = "param" if cookie else "env"
    # 解析 cookie 字段，检查关键字段是否存在
    sep = "; " if "; " in xhs_cookie else ";"
    fields = {k.strip(): v for k, v in
              (part.split("=", 1) if "=" in part else (part, "") for part in xhs_cookie.split(sep))}
    valid = bool(fields.get("web_session") and fields.get("a1"))
    return JSONResponse({"valid": valid, "source": source})


# ---------------------------------------------------------------------------
# GET /debug/tasks  — 查看所有 asyncio 任务
# ---------------------------------------------------------------------------

@router.get("/debug/tasks", summary="查看所有 asyncio 任务")
async def debug_all_tasks():
    """
    诊断端点：检查是否有泄漏的 asyncio 任务
    """
    import asyncio
    tasks = asyncio.all_tasks()
    task_info = []
    for task in tasks:
        task_info.append({
            "name": task.get_name(),
            "done": task.done(),
            "cancelled": task.cancelled(),
            "coro": str(task.get_coro())[:200],
        })
    return {"count": len(tasks), "tasks": task_info}


# ---------------------------------------------------------------------------
# GET /debug/threads  — 查看所有线程
# ---------------------------------------------------------------------------

@router.get("/debug/threads", summary="查看所有线程")
async def debug_threads():
    """
    诊断端点：检查线程状态
    """
    import threading
    threads = []
    for thread in threading.enumerate():
        threads.append({
            "name": thread.name,
            "daemon": thread.daemon,
            "alive": thread.is_alive(),
            "ident": thread.ident,
        })
    return {"count": len(threads), "threads": threads}


@router.get("/debug/tasks", summary="查看所有 asyncio 任务")
async def debug_all_tasks():
    """
    诊断端点：检查是否有泄漏的 asyncio 任务
    """
    import asyncio
    tasks = asyncio.all_tasks()
    task_info = []
    for task in tasks:
        task_info.append({
            "name": task.get_name(),
            "done": task.done(),
            "cancelled": task.cancelled(),
            "coro": str(task.get_coro())[:300],
        })
    return {"count": len(tasks), "tasks": task_info}


@router.get("/debug/fds", summary="查看文件描述符")
async def debug_fds():
    """
    诊断端点：检查是否有文件描述符泄漏
    """
    import os
    import psutil

    proc = psutil.Process(os.getpid())

    open_files = []
    for fd in proc.open_files():
        open_files.append({
            "fd": fd.fd,
            "path": fd.path,
        })

    connections = []
    for conn in proc.connections():
        connections.append({
            "fd": conn.fd,
            "status": conn.status,
            "laddr": str(conn.laddr) if conn.laddr else None,
            "raddr": str(conn.raddr) if conn.raddr else None,
        })

    return {
        "open_files_count": len(open_files),
        "connections_count": len(connections),
        "open_files": open_files[:20],
        "connections": connections[:20],
    }
