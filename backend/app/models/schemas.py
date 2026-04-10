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

    # ── Orchestrator输出（意图分析结果）
    user_query_rewritten: str
    intent: str                        # product_comparison | quality_issue | price_value | user_experience | general
    intent_confidence: float           # 意图识别置信度
    product_entities: list[str]
    aliases: list[str]
    entities_confidence: float         # 实体识别置信度
    key_aspects: list[dict[str, Any]]  # [{aspect, priority, user_sentiment}]
    user_needs: list[str]
    search_context: dict[str, Any]     # 给Retrieve Agent的搜索提示
    intent_analysis_score: float       # 综合质量分数（0-1）
    missing_dimensions: list[str]      # 识别出的缺失分析维度

    # ── Retrieval 阶段（Retrieve Agent负责）
    query_plan: list[str]
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
    references: list[dict[str, Any]]

    # ── 错误与流
    tool_errors: list[dict[str, Any]]
    stream_events: list[dict[str, Any]]  # 供 SSE 推送的进度事件

    # ── 内部控制字段（orchestrator 子图内部）
    _intent_round: int
    _intent_done: bool

    # ── 内部控制字段（retrieve 子图内部）
    _retrieve_round: int
    _retrieve_done: bool
    _current_batch: list[str]        # 本轮要搜索的关键词
    _used_keywords: list[str]        # 已使用过的所有关键词（防重复）

    # ── 内部控制字段（screen 子图内部）
    _screen_round: int
    _screen_done: bool
    _pre_filter_passed: list[dict[str, Any]]  # 初筛通过的帖子列表
    _ad_detect_passed: list[dict[str, Any]]   # 广告检测通过的帖子列表（真实分享）
    _pre_filter_stats: dict[str, Any]         # 初筛统计：{rejected_ad, rejected_brand, rejected_contact}
    _ad_detect_stats: dict[str, Any]          # 广告检测统计：{ad_detected, genuine}

    # ── 内部控制字段（analyze 子图内部）
    _analyze_round: int
    _analyze_done: bool
    _posts_to_fetch: list[str]               # 本轮要爬取的帖子 note_id 列表
    _fetched_comment_count: int              # 已获取的评论总数
    _filtered_comment_count: int             # 已过滤的无效评论总数
    _raw_comments_for_clustering: list[dict[str, Any]]  # 原始评论列表（供聚类使用）
    _need_refetch: bool  # 是否需要重新爬取评论（观点簇相关性不足）

    # ── 内部控制字段（synthesis 子图内部）
    _synthesis_round: int
    _synthesis_done: bool
    _report_outline: dict[str, Any]          # 大纲本体
    _outline_feedback: str                   # Observation的修改意见


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
