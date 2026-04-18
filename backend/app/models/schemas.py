"""LangGraph 共享状态定义。

使用 TypedDict + Annotated reducer（LangGraph 原生支持）。
累加型列表字段使用 operator.add reducer，节点只需返回增量；
其余字段使用默认 last-write-wins 语义。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# LangGraph 主状态（TypedDict）
# ---------------------------------------------------------------------------

class GraphState(TypedDict, total=False):
    # ── 输入
    request_id: str
    session_id: str
    user_query_raw: str

    # ── Orchestrator 输出（覆盖型）
    user_query_rewritten: str
    intent: str
    intent_confidence: float
    product_entities: list[str]
    aliases: list[str]
    entities_confidence: float
    key_aspects: list[dict[str, Any]]
    user_needs: list[str]
    search_context: dict[str, Any]
    intent_analysis_score: float
    missing_dimensions: list[str]

    # ── Retrieval 阶段（累加型用 Annotated）
    query_plan: list[str]
    search_attempts: int
    retrieved_posts: Annotated[list[dict[str, Any]], operator.add]
    retrieval_coverage_score: float

    # ── Screen 阶段（覆盖型）
    screened_items: list[dict[str, Any]]
    screening_stats: dict[str, Any]

    # ── Analyze 阶段（累加 + 覆盖混合）
    retrieved_comments: Annotated[list[dict[str, Any]], operator.add]
    clusters: list[dict[str, Any]]
    sentiment_summary: dict[str, Any]
    evidence_ledger: list[dict[str, Any]]

    # ── Memory
    memory_context: str

    # ── Synthesis 阶段（覆盖型）
    confidence_score: float
    limitations: list[str]
    final_answer: str
    references: list[dict[str, Any]]

    # ── 错误与流（累加型）
    tool_errors: Annotated[list[dict[str, Any]], operator.add]
    stream_events: Annotated[list[dict[str, Any]], operator.add]

    # ── 内部控制：orchestrator
    _intent_round: int
    _intent_done: bool

    # ── 内部控制：retrieve
    _retrieve_round: int
    _retrieve_done: bool
    _current_batch: list[str]
    _used_keywords: Annotated[list[str], operator.add]
    _target_posts: int
    _exclude_note_ids: list[str]

    # ── 内部控制：screen
    _screen_round: int
    _screen_done: bool
    _pre_filter_passed: list[dict[str, Any]]
    _ad_detect_passed: list[dict[str, Any]]
    _pre_filter_stats: dict[str, Any]
    _ad_detect_stats: dict[str, Any]

    # ── 内部控制：analyze
    _analyze_round: int
    _analyze_done: bool
    _posts_to_fetch: Annotated[list[str], operator.add]
    _fetched_comment_count: int
    _filtered_comment_count: int
    _raw_comments_for_clustering: list[dict[str, Any]]
    _need_refetch: bool

    # ── 内部控制：synthesis
    _synthesis_round: int
    _synthesis_done: bool
    _report_outline: dict[str, Any]
    _outline_feedback: str

    # ── 内部控制：memory/reuse
    _reuse_strategy: str
    _coverage_ratio: float
    _reusable_clusters: list[dict[str, Any]]
    _reuse_ratio: float
    _enable_memory: bool
    _api_type: int

    # ── 错误跟踪（累加型）
    _critical_errors: Annotated[list[dict[str, Any]], operator.add]
    _recoverable_errors: Annotated[list[dict[str, Any]], operator.add]

    # ── 关键错误终止标志
    _abort_analysis: bool


# ---------------------------------------------------------------------------
# Pydantic models（用于 FastAPI 请求/响应，不是 LangGraph 状态）
# ---------------------------------------------------------------------------

class AnalysisRequest(BaseModel):
    query: str = Field(..., description="产品舆情分析关键词")
    session_id: str | None = Field(None, description="可选会话 ID")


class AnalysisResult(BaseModel):
    summary: str
    pros: list[str]
    cons: list[str]
    controversies: list[str]
    confidence_score: float
    limitations: str
    final_answer: str  # Markdown
