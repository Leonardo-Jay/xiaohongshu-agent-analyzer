"""Workflow runner — 固定外层流水线，编排各 Agent/子图并通过 asyncio.Queue 推送 SSE 进度。

执行顺序:
  1. orchestrator_subgraph（内部 ReAct: reasoning → action → observation）
     - 意图识别，输出 intent, key_aspects, user_needs, search_context 等
  2. retrieve_subgraph（内部 ReAct: plan_keywords → fetch_posts → check_coverage）
     - 基于 orchestrator 的 search_context 生成关键词，搜索帖子，直到数量>=10 篇
  3. screen_subgraph（内部流水线：pre_filter → detect_ads → rank_and_select）
     - 使用 orchestrator 的 key_aspects、user_needs 进行相关性筛选，过滤广告/软广
  4. analyze_subgraph（内部流水线：select_posts → fetch_comments → check_quality）
     - 选择 top 帖子爬取评论（40 秒超时），过滤无效评论，观点聚类，情感分析
  5. synthesize (Synthesis Agent)
"""
from __future__ import annotations

import asyncio
import os
import sys
import traceback
from typing import Any

from loguru import logger

from app.agents.synthesis_agent import build_synthesis_graph
from app.agents.analyze_agent import build_analyze_graph
from app.agents.orchestrator_agent import build_orchestrator_graph
from app.agents.retrieve_agent import build_retrieve_graph
from app.agents.screen_agent import build_screen_graph
from app.models.schemas import GraphState
from app.tools.mcp_client import XhsMcpClient, XhsMcpClientPool
from app.utils.daily_audit_log import append_audit_log

# 编译 orchestrator 子图（ReAct 循环）
_orchestrator_app = build_orchestrator_graph()
# 编译 retrieve 子图（ReAct 循环）
_retrieve_app = build_retrieve_graph()
# 编译 screen 子图（三阶段流水线）
_screen_app = build_screen_graph()
# 编译 analyze 子图（三节点 ReAct 流水线）
_analyze_app = build_analyze_graph()
# 编译 synthesis 子图（Plan and Execute）
_synthesis_app = build_synthesis_graph()


def _progress(queue: asyncio.Queue, stage: str, message: str, progress: int) -> None:
    queue.put_nowait({"event": "progress", "data": {"stage": stage, "message": message, "progress": progress}})


async def run_analysis(query: str, run_id: str, queue: asyncio.Queue, cookie: str | None = None) -> None:
    """在后台 task 中执行全流程，结果/错误通过 queue 发送。"""
    state: GraphState = {
        "request_id": run_id,
        "session_id": "",
        "user_query_raw": query,
        # Orchestrator 初始化
        "user_query_rewritten": query,
        "intent": "general",
        "intent_confidence": 0.0,
        "product_entities": [],
        "aliases": [],
        "entities_confidence": 0.0,
        "key_aspects": [],
        "user_needs": [],
        "search_context": {},
        "intent_analysis_score": 0.0,
        "missing_dimensions": [],
        # Retrieve 初始化
        "query_plan": [],
        "search_attempts": 0,
        "retrieved_posts": [],
        "retrieval_coverage_score": 0.0,
        # Retrieve 内部控制字段
        "_retrieve_round": 0,
        "_retrieve_done": False,
        "_current_batch": [],
        "_used_keywords": [],
        # 其他阶段
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
        # Orchestrator 内部控制字段
        "_intent_round": 0,
        "_intent_done": False,
    }

    _progress(queue, "start", "分析任务已启动...", 3)

    try:
        async with XhsMcpClient(cookie=cookie) as client:
            # ── 1. Orchestrator Subgraph (ReAct: reasoning → action → observation)
            #     负责意图识别，输出高质量的意图分析结果
            _progress(queue, "orchestrator", "正在分析查询意图...", 8)
            config = {"client": client, "queue": queue}
            orchestrator_output = await _orchestrator_app.ainvoke(state, config=config)
            state = {**state, **orchestrator_output}
            logger.info(
                f"[Workflow][Orchestrator] finished: intent={state.get('intent')}, "
                f"confidence={state.get('intent_confidence', 0.0):.2f}, "
                f"score={state.get('intent_analysis_score', 0.0):.2f}"
            )
            _progress(
                queue,
                "orchestrator",
                f"意图: {state.get('intent')}，实体: {state.get('product_entities')}，"
                f"质量分数: {state.get('intent_analysis_score', 0.0):.2f}",
                20,
            )

            # ── 2. Retrieve Subgraph (ReAct: plan_keywords → fetch_posts → check_coverage)
            #     接收 orchestrator 的 search_context，生成检索关键词并执行搜索
            #     直到帖子数量 >= 10 篇或达到 3 轮上限
            _progress(queue, "retrieve", "正在检索相关帖子...", 25)
            config = {"configurable": {"client": client, "queue": queue}}
            retrieve_output = await _retrieve_app.ainvoke(state, config=config)
            state = {**state, **retrieve_output}
            logger.info(
                f"[Workflow][Retrieve] finished: posts={len(state.get('retrieved_posts', []))}, "
                f"attempts={state.get('search_attempts', 0)}, "
                f"coverage={state.get('retrieval_coverage_score', 0.0):.2f}"
            )
            _progress(queue, "retrieve", f"检索到 {len(state.get('retrieved_posts', []))} 篇帖子", 30)

            # ── 3. Screen Subgraph (流水线：pre_filter → detect_ads → rank_and_select)
            #     使用 orchestrator 的 key_aspects、user_needs 进行相关性筛选，过滤广告/软广
            #     输出 8~10 篇最相关的帖子
            _progress(queue, "screen", "正在筛选相关帖子（过滤广告/软广）...", 35)
            screen_output = await _screen_app.ainvoke(state)
            state = {**state, **screen_output}

            screened = state.get("screened_items", [])
            if not screened:
                raise RuntimeError("筛选后无相关帖子，请尝试更换关键词")
            _progress(queue, "screen", f"筛选出 {len(screened)} 篇相关帖子", 38)

        # ── 4. Analyze（连接池并发：只启动3个 MCP 子进程，所有帖子共享）
        _progress(queue, "analyze", "正在并发获取评论并分析舆情（最长约1分钟）...", 56)
        pool_size = int(os.getenv("MCP_POOL_SIZE", "2"))
        pool_size = max(1, min(pool_size, len(state.get("screened_items", [])) or 1))
        async with XhsMcpClientPool(size=pool_size, cookie=cookie) as pool:
            config = {"configurable": {"pool": pool, "queue": queue}}
            analyze_output = await _analyze_app.ainvoke(state, config=config)
        state = {**state, **analyze_output}

        comment_count = len(state.get("retrieved_comments", []))
        cluster_count = len(state.get("clusters", []))
        _progress(queue, "analyze", f"已分析 {comment_count} 条评论，生成 {cluster_count} 个观点簇", 78)

        # ── 5. Synthesize (Plan and Execute 架构)
        _progress(queue, "synthesize", "正在制定报告大纲与生成分析报告...", 82)
        config = {"configurable": {"queue": queue}}
        synthesis_output = await _synthesis_app.ainvoke(state, config=config)
        
        # 防御性确保合并数据字典不出错
        for k, v in synthesis_output.items():
            state[k] = v
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
        append_audit_log(
            "analysis_result",
            run_id=run_id,
            query=query,
            status="success",
            intent=state.get("intent", "general"),
            query_plan=state.get("query_plan", []),
            retrieved_post_count=len(state.get("retrieved_posts", [])),
            screened_count=len(state.get("screened_items", [])),
            comment_count=len(state.get("retrieved_comments", [])),
            cluster_count=len(state.get("clusters", [])),
            confidence_score=state.get("confidence_score", 0.0),
            limitations=state.get("limitations", []),
        )

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
            append_audit_log(
                "analysis_workflow_failed",
                run_id=run_id,
                query=query,
                status="failed",
                error_message=message,
                retrieved_post_count=len(state.get("retrieved_posts", [])),
                screened_count=len(state.get("screened_items", [])),
                comment_count=len(state.get("retrieved_comments", [])),
                cluster_count=len(state.get("clusters", [])),
            )
            queue.put_nowait({
                "event": "error",
                "data": {"code": "ANALYSIS_FAILED", "message": message},
            })
        if not isinstance(e, Exception):
            raise
    finally:
        queue.put_nowait(None)  # 哨兵：通知 SSE 生成器流结束
