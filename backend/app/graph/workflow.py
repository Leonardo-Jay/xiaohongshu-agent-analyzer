"""Workflow runner — 编排各 Agent，通过 asyncio.Queue 向 SSE 层推送进度事件。

执行顺序:
  1. classify_intent (Orchestrator)
  2. rewrite_and_plan (Orchestrator)
  3. retrieve_posts (Retrieval) — 若覆盖率不足且 attempts<2，调用 expand_queries_fallback 再重搜
  4. screen_posts (Screen)
  5. fetch_and_analyze (Opinion)
  6. synthesize (Synthesis)
"""
from __future__ import annotations

import asyncio
import os
import sys
import traceback
from typing import Any

from loguru import logger

from app.agents.orchestrator_agent import classify_intent, rewrite_and_plan
from app.agents.retrieve_agent import retrieve_posts, expand_queries_fallback
from app.agents.screen_agent import screen_posts
from app.agents.analyze_agent import fetch_and_analyze
from app.agents.synthesis_agent import synthesize
from app.models.schemas import GraphState
from app.tools.mcp_client import XhsMcpClient, XhsMcpClientPool

_MAX_RETRIEVAL_ATTEMPTS = 2
_COVERAGE_THRESHOLD = 3  # 有效帖子数量阈值


def _progress(queue: asyncio.Queue, stage: str, message: str, progress: int) -> None:
    queue.put_nowait({"event": "progress", "data": {"stage": stage, "message": message, "progress": progress}})


def _coverage_ok(state: GraphState) -> bool:
    posts = state.get("retrieved_posts", [])
    valid = [p for p in posts if int(p.get("like_count") or 0) > 0 or int(p.get("comment_count") or 0) > 0]
    return len(valid) >= _COVERAGE_THRESHOLD


async def run_analysis(query: str, run_id: str, queue: asyncio.Queue, cookie: str | None = None) -> None:
    """在后台 task 中执行全流程，结果/错误通过 queue 发送。"""
    state: GraphState = {
        "request_id": run_id,
        "session_id": "",
        "user_query_raw": query,
        "user_query_rewritten": query,
        "intent": "general",
        "product_entities": [],
        "aliases": [],
        "query_plan": [query],
        "search_attempts": 0,
        "retrieved_posts": [],
        "retrieval_coverage_score": 0.0,
        "screened_items": [],
        "screening_stats": {},
        "retrieved_comments": [],
        "clusters": [],
        "sentiment_summary": {},
        "evidence_ledger": [],
        "memory_context": "",
        "confidence_score": 0.0,
        "limitations": [],
        "final_answer": "",
        "tool_errors": [],
        "stream_events": [],
    }

    _progress(queue, "start", "分析任务已启动...", 3)

    try:
        # ── 1. Classify intent
        _progress(queue, "classify", "正在识别查询意图...", 8)
        updates = await classify_intent(state)
        state = {**state, **updates}
        _progress(queue, "classify", f"意图: {state.get('intent')}，实体: {state.get('product_entities')}", 13)

        # ── 2. Rewrite & plan
        _progress(queue, "plan", "正在生成搜索计划...", 15)
        updates = await rewrite_and_plan(state)
        state = {**state, **updates}
        plan = state.get("query_plan", [])
        _progress(queue, "plan", f"搜索词: {', '.join(plan)}", 20)

        async with XhsMcpClient() as client:
            # ── 3. Retrieve（带 fallback 回环）
            _progress(queue, "retrieve", "正在搜索小红书帖子...", 22)
            updates = await retrieve_posts(state, client, queue)
            state = {**state, **updates}

            attempts = 0
            while not _coverage_ok(state) and attempts < _MAX_RETRIEVAL_ATTEMPTS:
                _progress(queue, "expand", f"搜索结果不足，正在扩展关键词（第 {attempts+1} 次）...", 28 + attempts * 4)
                updates = await expand_queries_fallback(state)
                state = {**state, **updates}
                updates = await retrieve_posts(state, client, queue)
                state = {**state, **updates}
                attempts += 1

            posts = state.get("retrieved_posts", [])
            if not posts:
                raise RuntimeError("未搜索到任何相关帖子，请尝试更换关键词")
            _progress(queue, "retrieve", f"已获取 {len(posts)} 篇帖子", 38)

            # ── 4. Screen
            _progress(queue, "screen", "正在筛选相关帖子...", 42)
            updates = await screen_posts(state)
            state = {**state, **updates}

            screened = state.get("screened_items", [])
            if not screened:
                raise RuntimeError("筛选后无相关帖子，请尝试更换关键词")
            _progress(queue, "screen", f"筛选出 {len(screened)} 篇相关帖子", 52)

        # ── 5. Analyze（连接池并发：只启动3个 MCP 子进程，所有帖子共享）
        _progress(queue, "analyze", "正在并发获取评论并分析舆情...", 56)
        pool_size = int(os.getenv("MCP_POOL_SIZE", "2"))
        pool_size = max(1, min(pool_size, len(state.get("screened_items", [])) or 1))
        async with XhsMcpClientPool(size=pool_size, cookie=cookie) as pool:
            updates = await fetch_and_analyze(state, pool)
        state = {**state, **updates}

        comment_count = len(state.get("retrieved_comments", []))
        cluster_count = len(state.get("clusters", []))
        _progress(queue, "analyze", f"已分析 {comment_count} 条评论，生成 {cluster_count} 个观点簇", 78)

        # ── 6. Synthesize
        _progress(queue, "synthesize", "正在生成分析报告...", 82)
        updates = await synthesize(state)
        state = {**state, **updates}
        _progress(queue, "synthesize", "报告生成完毕", 97)

        # ── 推送最终结果（references 由 synthesis_agent 生成）
        queue.put_nowait({
            "event": "result",
            "data": {
                "final_answer": state.get("final_answer", ""),
                "confidence_score": state.get("confidence_score", 0.0),
                "clusters": state.get("clusters", []),
                "sentiment_summary": state.get("sentiment_summary", {}),
                "screened_count": len(state.get("screened_items", [])),
                "comment_count": len(state.get("retrieved_comments", [])),
                "limitations": state.get("limitations", []),
                "intent": state.get("intent", "general"),
                "query_plan": state.get("query_plan", []),
                "references": state.get("references", []),
            },
        })

    except BaseException as e:
        exc_type = type(e).__name__
        exc_msg = repr(e)
        if "COOKIE_EXPIRED" in str(e):
            queue.put_nowait({
                "event": "error",
                "data": {"code": "COOKIE_EXPIRED", "message": "小红书 Cookie 已过期，请重新配置"},
            })
        else:
            try:
                tb_text = traceback.format_exc() or ""
                if tb_text.strip() and "NoneType: None" not in tb_text:
                    message = tb_text.strip()
                else:
                    message = f"[{exc_type}] {exc_msg}"
            except Exception:
                message = f"[{exc_type}] {exc_msg}"
            if not message:
                message = f"[{exc_type}] (no details)"
            print(f"[WORKFLOW EXCEPT] {message}", file=sys.stderr, flush=True)
            logger.error(f"[Workflow] run_id={run_id} FAILED: {message}")
            queue.put_nowait({
                "event": "error",
                "data": {"code": "ANALYSIS_FAILED", "message": message},
            })
        if not isinstance(e, Exception):
            raise
    finally:
        queue.put_nowait(None)  # 哨兵：通知 SSE 生成器流结束
