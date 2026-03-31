"""LangGraph 共享状态定义。

使用 TypedDict（LangGraph 原生支持），而非 Pydantic，
以便直接传入 StateGraph。
"""
from __future__ import annotations

from typing import Any, TypedDict

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# LangGraph 主状态（TypedDict）
# ---------------------------------------------------------------------------

class GraphState(TypedDict, total=False):
    # ── 输入
    request_id: str
    session_id: str
    user_query_raw: str

    # ── Orchestrator 阶段
    user_query_rewritten: str
    intent: str                        # product_quality | price_value | comparison | general
    product_entities: list[str]
    aliases: list[str]
    query_plan: list[str]              # 扩展后的搜索词列表

    # ── Retrieval 阶段
    search_attempts: int
    retrieved_posts: list[dict[str, Any]]
    retrieval_coverage_score: float

    # ── Screen 阶段
    screened_items: list[dict[str, Any]]
    screening_stats: dict[str, Any]    # {total, passed, rejected, reject_reasons}

    # ── Opinion 阶段
    retrieved_comments: list[dict[str, Any]]
    clusters: list[dict[str, Any]]
    sentiment_summary: dict[str, Any]
    evidence_ledger: list[dict[str, Any]]

    # ── Memory
    memory_context: str

    # ── Synthesis 阶段
    confidence_score: float
    limitations: list[str]
    final_answer: str

    # ── 错误与流
    tool_errors: list[dict[str, Any]]
    stream_events: list[dict[str, Any]]  # 供 SSE 推送的进度事件


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
