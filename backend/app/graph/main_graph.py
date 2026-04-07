"""LangGraph 主图 — 固定外层流水线的结构镜像。

主路径:
  ingest_request
  → orchestrator (意图识别 ReAct 子图)
  → retrieve (检索 ReAct 子图)
  → screen (筛选子图：pre_filter → detect_ads → rank_and_select)
  → analyze (评论分析子图：select_posts → fetch_comments → cluster_opinions → check_quality)
  → synthesize_answer → store_memory → stream_output → END

注意：
- `workflow.py` 是实际运行入口
- `main_graph.py` 仅用于表达运行时架构与图结构导出
- orchestrator 是意图识别子图，负责理解任务并输出高质量的意图分析结果
- retrieve 是检索子图，负责基于 orchestrator 的 search_context 进行检索，直到帖子数量>=10 篇
- screen 是筛选子图，负责过滤广告/软广并基于相关性排序输出 8~10 篇帖子
- analyze 是评论分析子图，负责爬取评论、过滤无效内容、观点聚类、情感分析
"""
from __future__ import annotations

import uuid
from typing import Any

from langgraph.graph import StateGraph, END
from app.agents.analyze_agent import build_analyze_graph
from app.agents.synthesis_agent import build_synthesis_graph
from app.agents.orchestrator_agent import build_orchestrator_graph
from app.agents.retrieve_agent import build_retrieve_graph
from app.agents.screen_agent import build_screen_graph
from app.models.schemas import GraphState


# ---------------------------------------------------------------------------
# 同步包装：LangGraph 节点需要同步函数（异步节点需特殊处理）
# 这里保留轻量 stub，真实逻辑由 workflow.py 的 async 编排层调用
# ---------------------------------------------------------------------------

def ingest_request(state: GraphState) -> dict[str, Any]:
    """解析入参，初始化 state。"""
    query = state.get("user_query_raw", "")
    return {
        "request_id": state.get("request_id") or str(uuid.uuid4()),
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
        "screened_items": [],
        "retrieved_comments": [],
        "clusters": [],
        "tool_errors": [],
        "stream_events": [],
        "limitations": [],
        "memory_context": "",
        # 初始化意图识别循环控制字段
        "_intent_round": 0,
        "_intent_done": False,
        # 初始化检索循环控制字段
        "_retrieve_round": 0,
        "_retrieve_done": False,
        "_current_batch": [],
        "_used_keywords": [],
        # 初始化分析循环控制字段
        "_analyze_round": 0,
        "_analyze_done": False,
        "_posts_to_fetch": [],
        "_fetched_comment_count": 0,
    }


def synthesize_node(state: GraphState, config: dict) -> dict[str, Any]:
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
# 构建主图
# ---------------------------------------------------------------------------

def build_graph() -> StateGraph:
    synthesis_subgraph = build_synthesis_graph()
    orchestrator_subgraph = build_orchestrator_graph()
    retrieve_subgraph = build_retrieve_graph()
    screen_subgraph = build_screen_graph()
    analyze_subgraph = build_analyze_graph()

    def orchestrator_input_mapper(state: GraphState) -> dict[str, Any]:
        return {
            "request_id": state.get("request_id"),
            "session_id": state.get("session_id", ""),
            "user_query_raw": state.get("user_query_raw", ""),
            "user_query_rewritten": state.get("user_query_rewritten", state.get("user_query_raw", "")),
            "intent": state.get("intent", "general"),
            "intent_confidence": state.get("intent_confidence", 0.0),
            "product_entities": state.get("product_entities", []),
            "aliases": state.get("aliases", []),
            "entities_confidence": state.get("entities_confidence", 0.0),
            "key_aspects": state.get("key_aspects", []),
            "user_needs": state.get("user_needs", []),
            "search_context": state.get("search_context", {}),
            "intent_analysis_score": state.get("intent_analysis_score", 0.0),
            "missing_dimensions": state.get("missing_dimensions", []),
            "_intent_round": state.get("_intent_round", 0),
            "_intent_done": state.get("_intent_done", False),
        }

    def orchestrator_output_mapper(state: GraphState, subgraph_output: dict[str, Any]) -> dict[str, Any]:
        orchestrator_owned_fields = {
            "intent",
            "intent_confidence",
            "product_entities",
            "aliases",
            "entities_confidence",
            "key_aspects",
            "user_needs",
            "user_query_rewritten",
            "search_context",
            "intent_analysis_score",
            "missing_dimensions",
            "_intent_round",
            "_intent_done",
        }
        preserved_parent_fields = {
            key: value
            for key, value in state.items()
            if key not in orchestrator_owned_fields and key not in subgraph_output
        }
        return {
            **preserved_parent_fields,
            **subgraph_output,
        }

    def orchestrator_node(state: GraphState, config: dict) -> dict[str, Any]:
        """将 orchestrator 子图作为节点函数。"""
        subgraph_input = orchestrator_input_mapper(state)
        subgraph_output = orchestrator_subgraph.invoke(subgraph_input, config)
        return orchestrator_output_mapper(state, subgraph_output)

    def retrieve_input_mapper(state: GraphState) -> dict[str, Any]:
        return {
            "request_id": state.get("request_id"),
            "user_query_raw": state.get("user_query_raw", ""),
            "intent": state.get("intent", "general"),
            "product_entities": state.get("product_entities", []),
            "aliases": state.get("aliases", []),
            "search_context": state.get("search_context", {}),
            "query_plan": state.get("query_plan", []),
            "search_attempts": state.get("search_attempts", 0),
            "retrieved_posts": state.get("retrieved_posts", []),
            "retrieval_coverage_score": state.get("retrieval_coverage_score", 0.0),
            "_retrieve_round": state.get("_retrieve_round", 0),
            "_retrieve_done": state.get("_retrieve_done", False),
            "_current_batch": state.get("_current_batch", []),
            "_used_keywords": state.get("_used_keywords", []),
        }

    def retrieve_output_mapper(state: GraphState, subgraph_output: dict[str, Any]) -> dict[str, Any]:
        retrieve_owned_fields = {
            "query_plan",
            "search_attempts",
            "retrieved_posts",
            "retrieval_coverage_score",
            "_retrieve_round",
            "_retrieve_done",
            "_current_batch",
            "_used_keywords",
        }
        preserved_parent_fields = {
            key: value
            for key, value in state.items()
            if key not in retrieve_owned_fields and key not in subgraph_output
        }
        return {
            **preserved_parent_fields,
            **subgraph_output,
        }

    def retrieve_node(state: GraphState, config: dict) -> dict[str, Any]:
        """将 retrieve 子图作为节点函数。"""
        subgraph_input = retrieve_input_mapper(state)
        subgraph_output = retrieve_subgraph.invoke(subgraph_input, config)
        return retrieve_output_mapper(state, subgraph_output)

    def screen_input_mapper(state: GraphState) -> dict[str, Any]:
        return {
            "request_id": state.get("request_id"),
            "user_query_raw": state.get("user_query_raw", ""),
            "intent": state.get("intent", "general"),
            "key_aspects": state.get("key_aspects", []),
            "user_needs": state.get("user_needs", []),
            "retrieved_posts": state.get("retrieved_posts", []),
            "screened_items": state.get("screened_items", []),
            "screening_stats": state.get("screening_stats", {}),
            "_screen_round": state.get("_screen_round", 0),
            "_screen_done": state.get("_screen_done", False),
        }

    def screen_output_mapper(state: GraphState, subgraph_output: dict[str, Any]) -> dict[str, Any]:
        screen_owned_fields = {
            "screened_items",
            "screening_stats",
            "_screen_round",
            "_screen_done",
        }
        preserved_parent_fields = {
            key: value
            for key, value in state.items()
            if key not in screen_owned_fields and key not in subgraph_output
        }
        return {
            **preserved_parent_fields,
            **subgraph_output,
        }

    def screen_node(state: GraphState, config: dict) -> dict[str, Any]:
        """将 screen 子图作为节点函数。"""
        subgraph_input = screen_input_mapper(state)
        subgraph_output = screen_subgraph.invoke(subgraph_input, config)
        return screen_output_mapper(state, subgraph_output)

    def analyze_input_mapper(state: GraphState) -> dict[str, Any]:
        return {
            "request_id": state.get("request_id"),
            "user_query_raw": state.get("user_query_raw", ""),
            "screened_items": state.get("screened_items", []),
            "retrieved_comments": state.get("retrieved_comments", []),
            "clusters": state.get("clusters", []),
            "sentiment_summary": state.get("sentiment_summary", {}),
            "evidence_ledger": state.get("evidence_ledger", []),
            "_analyze_round": state.get("_analyze_round", 0),
            "_analyze_done": state.get("_analyze_done", False),
            "_posts_to_fetch": state.get("_posts_to_fetch", []),
            "_fetched_comment_count": state.get("_fetched_comment_count", 0),
            "_raw_comments_for_clustering": state.get("_raw_comments_for_clustering", []),
        }

    def analyze_output_mapper(state: GraphState, subgraph_output: dict[str, Any]) -> dict[str, Any]:
        analyze_owned_fields = {
            "retrieved_comments",
            "clusters",
            "sentiment_summary",
            "evidence_ledger",
            "_analyze_round",
            "_analyze_done",
            "_posts_to_fetch",
            "_fetched_comment_count",
            "_raw_comments_for_clustering",
        }
        preserved_parent_fields = {
            key: value
            for key, value in state.items()
            if key not in analyze_owned_fields and key not in subgraph_output
        }
        return {
            **preserved_parent_fields,
            **subgraph_output,
        }

    def analyze_node(state: GraphState, config: dict) -> dict[str, Any]:
        """将 analyze 子图作为节点函数。"""
        subgraph_input = analyze_input_mapper(state)
        subgraph_output = analyze_subgraph.invoke(subgraph_input, config)
        return analyze_output_mapper(state, subgraph_output)

    def synthesis_input_mapper(state: GraphState) -> dict[str, Any]:
        return {
            "request_id": state.get("request_id"),
            "user_query_raw": state.get("user_query_raw", ""),
            "screened_items": state.get("screened_items", []),
            "retrieved_comments": state.get("retrieved_comments", []),
            "clusters": state.get("clusters", []),
            "sentiment_summary": state.get("sentiment_summary", {}),
            "_synthesis_round": state.get("_synthesis_round", 0),
            "_synthesis_done": state.get("_synthesis_done", False),
            "_report_outline": state.get("_report_outline", {}),
            "_outline_feedback": state.get("_outline_feedback", ""),
        }

    def synthesis_output_mapper(state: GraphState, subgraph_output: dict[str, Any]) -> dict[str, Any]:
        synthesis_owned_fields = {
            "final_answer",
            "confidence_score",
            "limitations",
            "references",
            "_synthesis_round",
            "_synthesis_done",
            "_report_outline",
            "_outline_feedback",
        }
        preserved_parent_fields = {
            key: value
            for key, value in state.items()
            if key not in synthesis_owned_fields and key not in subgraph_output
        }
        return {
            **preserved_parent_fields,
            **subgraph_output,
        }

    def synthesis_node(state: GraphState, config: dict) -> dict[str, Any]:
        """将 synthesis 子图作为节点函数。"""
        subgraph_input = synthesis_input_mapper(state)
        subgraph_output = synthesis_subgraph.invoke(subgraph_input, config)
        return synthesis_output_mapper(state, subgraph_output)

    g = StateGraph(GraphState)
    g.add_node("orchestrator", orchestrator_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("screen", screen_node)
    g.add_node("analyze", analyze_node)
    g.add_node("synthesis", synthesis_node)

    for fn in [
        ingest_request,
        store_memory,
        stream_output,
    ]:
        g.add_node(fn.__name__, fn)

    g.set_entry_point("ingest_request")
    g.add_edge("ingest_request", "orchestrator")
    g.add_edge("orchestrator", "retrieve")
    g.add_edge("retrieve", "screen")
    g.add_edge("screen", "analyze")
    g.add_edge("analyze", "synthesis")
    g.add_edge("synthesis", "store_memory")
    g.add_edge("store_memory", "stream_output")
    g.add_edge("stream_output", END)

    return g


app_graph = build_graph().compile()
