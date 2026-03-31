"""LangGraph 主图 — 14 个节点 + 条件边。

主路径:
  ingest_request
  → classify_intent
  → rewrite_and_plan
  → retrieve_memory_context
  → tool_route_search
  → evaluate_retrieval_coverage ──(不足且attempts<2)──→ expand_queries_fallback → tool_route_search
                                ──(足够 or 达最大)──→ content_review_filter
  → content_review_filter ──(无内容)──→ stream_output
                          ──(有内容)──→ fetch_comments_batch
  → dedupe_and_cluster → opinion_analysis → synthesize_answer → store_memory → stream_output → END

注意：含 MCP client 的节点（tool_route_search、fetch_comments_batch）
在 workflow.py 中直接调用 Agent 函数；main_graph 中同名节点为可覆盖的占位。
workflow.py 作为实际运行入口，main_graph 用于图结构可视化与导出。
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from langgraph.graph import StateGraph, END

from app.models.schemas import GraphState


# ---------------------------------------------------------------------------
# 同步包装：LangGraph 节点需要同步函数（异步节点需特殊处理）
# 这里保留轻量 stub，真实逻辑由 workflow.py 的 async 编排层调用
# ---------------------------------------------------------------------------

def ingest_request(state: GraphState) -> dict[str, Any]:
    """解析入参，初始化 state。"""
    return {
        "request_id": state.get("request_id") or str(uuid.uuid4()),
        "search_attempts": 0,
        "retrieved_posts": [],
        "screened_items": [],
        "retrieved_comments": [],
        "clusters": [],
        "tool_errors": [],
        "stream_events": [],
        "limitations": [],
        "memory_context": "",
    }


def classify_intent(state: GraphState) -> dict[str, Any]:
    """LLM 判断意图和产品实体（由 orchestrator_agent.classify_intent 实现）。"""
    return {
        "intent": state.get("intent", "general"),
        "product_entities": state.get("product_entities", []),
        "aliases": state.get("aliases", []),
        "user_query_rewritten": state.get("user_query_rewritten", state.get("user_query_raw", "")),
    }


def rewrite_and_plan(state: GraphState) -> dict[str, Any]:
    """生成扩展搜索词列表（由 orchestrator_agent.rewrite_and_plan 实现）。"""
    return {
        "query_plan": state.get("query_plan") or [state.get("user_query_raw", "")],
    }


def retrieve_memory_context(state: GraphState) -> dict[str, Any]:
    """查询历史记忆（早期返回空）。"""
    return {"memory_context": ""}


def tool_route_search(state: GraphState) -> dict[str, Any]:
    """调用 MCP search_posts（由 retrieve_agent.retrieve_posts 实现）。"""
    return {"retrieved_posts": state.get("retrieved_posts", [])}


def evaluate_retrieval_coverage(state: GraphState) -> dict[str, Any]:
    """判断数据是否足够（阈值 ≥3 条有效帖子）。"""
    posts = state.get("retrieved_posts", [])
    valid = [p for p in posts if int(p.get("like_count") or 0) > 0 or int(p.get("comment_count") or 0) > 0]
    score = min(len(valid) / 3.0, 1.0)
    return {"retrieval_coverage_score": score}


def expand_queries_fallback(state: GraphState) -> dict[str, Any]:
    """扩展词重搜（由 retrieve_agent.expand_queries_fallback 实现）。"""
    attempts = state.get("search_attempts", 0) + 1
    return {"search_attempts": attempts}


def content_review_filter(state: GraphState) -> dict[str, Any]:
    """广告/无关筛选（由 screen_agent.screen_posts 实现）。"""
    posts = state.get("retrieved_posts", [])
    screened = state.get("screened_items") or posts
    return {
        "screened_items": screened,
        "screening_stats": {
            "total": len(posts),
            "passed": len(screened),
            "rejected": len(posts) - len(screened),
            "reject_reasons": [],
        },
    }


def fetch_comments_batch(state: GraphState) -> dict[str, Any]:
    """批量拉取评论（由 analyze_agent.fetch_and_analyze 实现）。"""
    return {"retrieved_comments": state.get("retrieved_comments", [])}


def dedupe_and_cluster(state: GraphState) -> dict[str, Any]:
    """去重、观点聚类（由 analyze_agent.fetch_and_analyze 实现）。"""
    return {"clusters": state.get("clusters", [])}


def opinion_analysis(state: GraphState) -> dict[str, Any]:
    """情感/主题分析（由 analyze_agent.fetch_and_analyze 实现）。"""
    return {
        "sentiment_summary": state.get("sentiment_summary", {}),
        "evidence_ledger": state.get("evidence_ledger", []),
    }


def synthesize_answer(state: GraphState) -> dict[str, Any]:
    """生成最终报告（由 synthesis_agent.synthesize 实现）。"""
    return {
        "final_answer": state.get("final_answer", ""),
        "confidence_score": state.get("confidence_score", 0.0),
    }


def store_memory(state: GraphState) -> dict[str, Any]:
    """存储记忆（早期 no-op）。"""
    return {}


def stream_output(state: GraphState) -> dict[str, Any]:
    """标记流结束（由 workflow.py 的 queue 机制处理）。"""
    return {}


# ---------------------------------------------------------------------------
# 条件边路由
# ---------------------------------------------------------------------------

def _route_coverage(
    state: GraphState,
) -> Literal["expand_queries_fallback", "content_review_filter"]:
    score = state.get("retrieval_coverage_score", 0.0)
    attempts = state.get("search_attempts", 0)
    if score < 1.0 and attempts < 2:
        return "expand_queries_fallback"
    return "content_review_filter"


def _route_after_screen(
    state: GraphState,
) -> Literal["fetch_comments_batch", "stream_output"]:
    if not state.get("screened_items"):
        return "stream_output"
    return "fetch_comments_batch"


# ---------------------------------------------------------------------------
# 构建图
# ---------------------------------------------------------------------------

def build_graph() -> StateGraph:
    g = StateGraph(GraphState)

    for fn in [
        ingest_request,
        classify_intent,
        rewrite_and_plan,
        retrieve_memory_context,
        tool_route_search,
        evaluate_retrieval_coverage,
        expand_queries_fallback,
        content_review_filter,
        fetch_comments_batch,
        dedupe_and_cluster,
        opinion_analysis,
        synthesize_answer,
        store_memory,
        stream_output,
    ]:
        g.add_node(fn.__name__, fn)

    g.set_entry_point("ingest_request")
    g.add_edge("ingest_request", "classify_intent")
    g.add_edge("classify_intent", "rewrite_and_plan")
    g.add_edge("rewrite_and_plan", "retrieve_memory_context")
    g.add_edge("retrieve_memory_context", "tool_route_search")
    g.add_edge("tool_route_search", "evaluate_retrieval_coverage")

    g.add_conditional_edges(
        "evaluate_retrieval_coverage",
        _route_coverage,
        {
            "expand_queries_fallback": "expand_queries_fallback",
            "content_review_filter": "content_review_filter",
        },
    )
    g.add_edge("expand_queries_fallback", "tool_route_search")

    g.add_conditional_edges(
        "content_review_filter",
        _route_after_screen,
        {
            "fetch_comments_batch": "fetch_comments_batch",
            "stream_output": "stream_output",
        },
    )

    g.add_edge("fetch_comments_batch", "dedupe_and_cluster")
    g.add_edge("dedupe_and_cluster", "opinion_analysis")
    g.add_edge("opinion_analysis", "synthesize_answer")
    g.add_edge("synthesize_answer", "store_memory")
    g.add_edge("store_memory", "stream_output")
    g.add_edge("stream_output", END)

    return g


app_graph = build_graph().compile()
