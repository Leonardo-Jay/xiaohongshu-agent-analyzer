"""Report IR v1 models.

ReportIR is the structured source of truth for rendering and validation.
The user-facing Markdown remains a derived artifact for frontend compatibility.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ReportMetadata(BaseModel):
    query: str
    intent: str = "general"
    post_count: int = 0
    comment_count: int = 0
    confidence_score: float = 0.0
    limitations: list[str] = Field(default_factory=list)


class SummaryCard(BaseModel):
    label: str
    value: str
    supporting_cluster_ids: list[str] = Field(default_factory=list)


class Citation(BaseModel):
    id: str
    cluster_id: str
    topic: str = ""
    sentiment: str = "中立"
    source_title: str = ""
    source_url: str = ""
    quote: str


class ChartSpec(BaseModel):
    id: str
    type: Literal["bar", "pie", "table"] = "bar"
    title: str
    data: list[dict[str, Any]] = Field(default_factory=list)


class ReportBlock(BaseModel):
    type: Literal["paragraph", "subheading", "list"] = "paragraph"
    text: str = ""
    items: list[str] = Field(default_factory=list)
    citation_ids: list[str] = Field(default_factory=list)


class ReportSection(BaseModel):
    id: str
    title: str
    type: Literal["overview", "analysis", "recommendation", "risk", "appendix"] = "analysis"
    cluster_ids: list[str] = Field(default_factory=list)
    blocks: list[ReportBlock] = Field(default_factory=list)


class ReportIR(BaseModel):
    version: str = "1.0"
    title: str
    metadata: ReportMetadata
    summary_cards: list[SummaryCard] = Field(default_factory=list)
    sections: list[ReportSection]
    charts: list[ChartSpec] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
