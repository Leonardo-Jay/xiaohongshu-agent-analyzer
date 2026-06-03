"""Synthesis Subgraph — 报告生成与质量把控的 Plan and Execute 架构
职责：接收观点聚类等数据，先规划大纲，经过自我审查合格后，严格依大纲流式输出报告，最后根据加权规则进行总评得分。

核心流程：
  1. plan_outline: 分析数据体量，利用 LLM 制定细化的撰写格式和章节大纲 (JSON)。如果数据太空则跳过大纲。
  2. observe_outline: (护栏节点) 扫描大纲，防止大纲漏掉重要簇，或幻觉造出不存在的簇。不合格则退回重写（最多2次）。
  3. evaluate_and_score: 纯规则计算置信度（数据量、多样性、情绪对抗、引用情况）40/30/15/15 权重，汇编 references 清单。
  4. execute_report: 主笔节点。生成 Report IR v1，后端确定性渲染 Markdown 并向队列推送 `report_chunk`。
"""
from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from typing import Any, Literal

import httpx
from loguru import logger
from pydantic import ValidationError

from app.models.report_ir import Citation, ChartSpec, ReportIR, ReportMetadata
from app.models.schemas import GraphState
from app.prompts.templates import (
    SYNTHESIS_MODIFY_OUTLINE_PROMPT,
    SYNTHESIS_PLAN_OUTLINE_PROMPT,
    SYNTHESIS_REPORT_IR_PROMPT,
    SYNTHESIS_REPORT_IR_RAW_REPAIR_PROMPT,
    SYNTHESIS_REPORT_IR_REPAIR_PROMPT,
    SYNTHESIS_REPORT_PROMPT,
)
from app.reports.renderer import render_markdown
from app.tools.llm import create_llm

# 规划与 Report IR JSON 生成更容易出现长响应，给非流式调用更宽松的超时。
_llm_plan = create_llm(temperature=0.1, timeout=180.0, max_tokens=8192)
_llm_report = create_llm(temperature=0.3)

_MAX_SYNTHESIS_ROUNDS = 3
_MAX_IR_CLUSTERS = 10
_MAX_QUOTES_PER_CLUSTER = 2
_MAX_QUOTE_CHARS = 120
_MIN_ANALYSIS_PARAGRAPH_CHARS = 120
_MIN_SUMMARY_CARDS = 4
_MIN_SUMMARY_VALUE_CHARS = 32

_GENERIC_ANALYSIS_TITLES = {
    "续航表现",
    "品控问题",
    "发热问题",
    "购买建议",
    "使用体验",
    "核心问题",
    "核心问题分析",
    "问题分析",
    "性能表现",
    "价格评价",
    "外观设计",
    "系统流畅度",
    "用户反馈",
    "用户评价",
    "负面反馈",
    "正面反馈",
    "综合体验",
}

_GENERIC_SUMMARY_VALUES = {
    "负面偏多",
    "正面偏多",
    "中立",
    "分化明显",
    "整体偏负面",
    "整体偏正面",
    "暂无",
    "无",
}

_BANNED_TEMPORAL_REPORT_TERMS = ("爆发期", "扩散期", "沉淀期", "复燃期", "传播生命周期", "较早样本", "中后段样本")

_SECTION_TYPE_ALIASES = {
    "overview": "overview",
    "summary": "recommendation",
    "conclusion": "recommendation",
    "recommendation": "recommendation",
    "recommendations": "recommendation",
    "advice": "recommendation",
    "suggestion": "recommendation",
    "suggestions": "recommendation",
    "analysis": "analysis",
    "risk": "risk",
    "risks": "risk",
    "appendix": "appendix",
    "整体印象": "overview",
    "概览": "overview",
    "总览": "overview",
    "总结": "recommendation",
    "结论": "recommendation",
    "综合建议": "recommendation",
    "建议": "recommendation",
    "分析": "analysis",
    "风险": "risk",
    "附录": "appendix",
}

_BLOCK_TYPE_ALIASES = {
    "paragraph": "paragraph",
    "text": "paragraph",
    "body": "paragraph",
    "subheading": "subheading",
    "subtitle": "subheading",
    "heading": "subheading",
    "list": "list",
    "bullets": "list",
    "bullet_list": "list",
}

_CHART_TYPE_ALIASES = {
    "bar": "bar",
    "bar_chart": "bar",
    "column": "bar",
    "pie": "pie",
    "pie_chart": "pie",
    "table": "table",
}

def _strip_fences(text: str) -> str:
    """去除 LLM 报告输出中可能加的 markdown/代码围栏。"""
    text = re.sub(r'^```(?:markdown)?\s*', '', text.strip(), flags=re.MULTILINE)
    return re.sub(r'```\s*$', '', text.strip(), flags=re.MULTILINE).strip()

def _parse_json_response(text: str) -> dict:
    """安全地解析可能会带有包裹的 LLM JSON 输出"""
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    text = re.sub(r'```\s*$', '', text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception as e:
        logger.warning(f"[Synthesis] JSON 解析失败，尝试修复: {e}")
        # 如果彻底失败，返回一个保底的最简单的大纲结构
        return {
            "report_strategy": {
                "overall_tone": "客观中立",
                "structure": [{"chapter": "综合分析报告", "focus": "对搜索到的数据进行简单汇总概括", "use_clusters": []}]
            }
        }


def _extract_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from an LLM response without using fallback data."""
    cleaned = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r'```\s*$', '', cleaned.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    start = None
    depth = 0
    in_string = False
    escaped = False
    for idx, char in enumerate(cleaned):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if start is None:
                start = idx
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                data = json.loads(cleaned[start: idx + 1])
                if not isinstance(data, dict):
                    raise ValueError("Report IR response is not a JSON object")
                return data
    raise ValueError("Unable to extract Report IR JSON object")


def _raw_report_excerpt(text: str, limit: int = 1000) -> str:
    raw = str(text or "").replace("\x00", "")
    if len(raw) <= limit:
        return raw
    return raw[:limit]


def _raw_report_tail(text: str, limit: int = 1000) -> str:
    raw = str(text or "").replace("\x00", "")
    if len(raw) <= limit:
        return raw
    return raw[-limit:]


def _log_report_ir_raw(
    *,
    stage: str,
    raw_text: str,
    context: dict[str, Any],
    finish_reason: str = "",
    prompt_chars: int = 0,
    error: Exception | None = None,
) -> None:
    raw = str(raw_text or "")
    log_payload = {
        "stage": stage,
        "finish_reason": finish_reason or "",
        "raw_chars": len(raw),
        "prompt_chars": prompt_chars,
        "clusters": len(context.get("clusters", []) or []),
        "posts": int(context.get("post_count", 0) or 0),
        "comments": int(context.get("comment_count", 0) or 0),
        "citations": len(context.get("citations", []) or []),
        "has_open_brace": "{" in raw,
        "has_close_brace": "}" in raw,
        "raw_prefix": _raw_report_excerpt(raw, 800),
        "raw_suffix": _raw_report_tail(raw, 800),
    }
    if error is not None:
        log_payload["error_type"] = type(error).__name__
        log_payload["error"] = str(error)
    logger.info(
        "[Synthesis][ReportIR] raw diagnostics: {}",
        json.dumps(log_payload, ensure_ascii=False),
    )


async def _invoke_report_ir_llm(
    prompt: str,
    *,
    context: dict[str, Any],
    stage: str,
) -> tuple[str, str]:
    """Use non-streaming completion first; stream only as transport fallback."""
    try:
        response = await _llm_plan.ainvoke(prompt)
        raw_text = _llm_plan._normalize_text(response.content)
        finish_reason = getattr(response, "finish_reason", "") or ""
        _log_report_ir_raw(
            stage=stage,
            raw_text=raw_text,
            context=context,
            finish_reason=finish_reason,
            prompt_chars=len(prompt),
        )
        return raw_text, finish_reason
    except (httpx.TimeoutException, httpx.RequestError, asyncio.TimeoutError) as exc:
        logger.warning(
            "[Synthesis][ReportIR] {} 非流式调用失败，尝试流式兜底: type={}, error={!r}",
            stage,
            type(exc).__name__,
            exc,
        )
        buffer = ""
        async for chunk in _llm_plan.astream(prompt):
            buffer += chunk
        raw_text = _llm_plan._normalize_text(buffer)
        _log_report_ir_raw(
            stage=f"{stage}_stream_fallback",
            raw_text=raw_text,
            context=context,
            finish_reason="stream_fallback",
            prompt_chars=len(prompt),
            error=exc,
        )
        return raw_text, "stream_fallback"


async def _repair_unparseable_report_ir(
    *,
    raw_text: str,
    parse_error: Exception,
    context: dict[str, Any],
    prompt_context: dict[str, Any],
) -> dict[str, Any]:
    logger.warning(
        "[Synthesis][ReportIR] JSON 提取失败，进入 raw-text repair: type={}, error={!r}, raw_chars={}",
        type(parse_error).__name__,
        parse_error,
        len(str(raw_text or "")),
    )
    repair_prompt = SYNTHESIS_REPORT_IR_RAW_REPAIR_PROMPT.format(
        parse_error=f"{type(parse_error).__name__}: {parse_error}",
        allowed_cluster_ids=", ".join(context.get("allowed_cluster_ids", [])),
        allowed_citation_ids=", ".join(context.get("allowed_citation_ids", [])),
        report_context_json=json.dumps(prompt_context, ensure_ascii=False, indent=2),
        raw_chars=len(str(raw_text or "")),
        raw_prefix=_raw_report_excerpt(raw_text, 2000),
        raw_suffix=_raw_report_tail(raw_text, 2000),
    )
    repair_text, _ = await _invoke_report_ir_llm(
        repair_prompt,
        context=context,
        stage="raw_text_repair",
    )
    try:
        return _extract_json_object(repair_text)
    except ValueError as exc:
        raise ValueError(f"Report IR JSON 提取失败，raw-text repair 也失败: {exc}") from exc


def _truncate_text(value: str, limit: int = _MAX_QUOTE_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _cluster_count(cluster: dict[str, Any]) -> int:
    raw = cluster.get("count", cluster.get("avg_count", 1))
    try:
        return max(1, int(float(raw)))
    except (TypeError, ValueError):
        return 1


def _compact_title(value: str) -> str:
    return re.sub(r"[\s　：:，,。.!！?？、\-—_]+", "", str(value or "")).strip()


def _is_generic_analysis_title(title: str) -> bool:
    compact = _compact_title(title)
    if compact in {_compact_title(item) for item in _GENERIC_ANALYSIS_TITLES}:
        return True
    if len(compact) <= 4 and any(keyword in compact for keyword in ("续航", "品控", "发热", "性能", "体验", "建议")):
        return True
    return False


def _is_thin_summary_value(value: str) -> bool:
    compact = _compact_title(value)
    if not compact:
        return True
    if compact in {_compact_title(item) for item in _GENERIC_SUMMARY_VALUES}:
        return True
    return len(compact) < _MIN_SUMMARY_VALUE_CHARS


def _contains_quote_fragment(text: str, citation_ids: list[str], context: dict[str, Any]) -> bool:
    if not text or not citation_ids:
        return False
    citations = {
        item.get("id"): item.get("quote", "")
        for item in context.get("citations", [])
        if isinstance(item, dict)
    }
    normalized_text = re.sub(r"\s+", "", text)
    for citation_id in citation_ids:
        quote = citations.get(citation_id, "")
        normalized_quote = re.sub(r"\s+", "", quote)
        if not normalized_quote:
            continue
        if len(normalized_quote) <= 12:
            if normalized_quote in normalized_text:
                return True
            continue
        for start in range(0, max(1, len(normalized_quote) - 11)):
            fragment = normalized_quote[start: start + 12]
            if fragment and fragment in normalized_text:
                return True
    return False


def _build_report_metadata(state: GraphState) -> ReportMetadata:
    return ReportMetadata(
        query=state.get("user_query_raw", ""),
        intent=state.get("intent", "general"),
        post_count=len(state.get("screened_items", [])),
        comment_count=len(state.get("retrieved_comments", [])),
        confidence_score=float(state.get("confidence_score", 0.0) or 0.0),
        limitations=state.get("limitations", []) or [],
    )


def _collect_raw_report_evidence(state: GraphState, limit: int = 10) -> list[dict[str, Any]]:
    screened_items = state.get("screened_items", []) or []
    post_by_id = {item.get("note_id"): item for item in screened_items if isinstance(item, dict)}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add_row(
        *,
        evidence_id: str,
        content: str,
        source_title: str,
        source_url: str,
        source_type: str,
    ) -> None:
        text = re.sub(r"\s+", " ", str(content or "")).strip()
        if not text:
            return
        key = (source_url or evidence_id, text)
        if key in seen:
            return
        seen.add(key)
        rows.append({
            "evidence_id": evidence_id or f"raw_{len(rows):03d}",
            "content": text,
            "source_title": source_title or "用户原话",
            "source_url": source_url or "",
            "source_type": source_type,
        })

    for item in state.get("evidence_registry", []) or []:
        if not isinstance(item, dict):
            continue
        add_row(
            evidence_id=str(item.get("evidence_id") or ""),
            content=str(item.get("content") or ""),
            source_title=str(item.get("source_title") or ""),
            source_url=str(item.get("source_url") or item.get("source_note_url") or ""),
            source_type=str(item.get("source_type") or "evidence"),
        )
        if len(rows) >= limit:
            return rows

    for index, comment in enumerate(state.get("retrieved_comments", []) or []):
        if not isinstance(comment, dict):
            continue
        post = post_by_id.get(comment.get("note_id"), {})
        add_row(
            evidence_id=str(comment.get("comment_id") or f"comment_{index:03d}"),
            content=str(comment.get("content") or ""),
            source_title=str(post.get("title") or comment.get("nickname") or "用户评论"),
            source_url=str(post.get("note_url") or ""),
            source_type="comment",
        )
        if len(rows) >= limit:
            return rows

    for index, post in enumerate(screened_items):
        if not isinstance(post, dict):
            continue
        title = str(post.get("title") or "").strip()
        desc = str(post.get("desc") or "").strip()
        if rows and not desc:
            continue
        content = f"{title}\n\n{desc}" if title and desc else (title or desc)
        add_row(
            evidence_id=str(post.get("note_id") or f"post_{index:03d}"),
            content=content,
            source_title=title or "帖子正文",
            source_url=str(post.get("note_url") or ""),
            source_type="post_body",
        )
        if len(rows) >= limit:
            return rows

    return rows


def _infer_report_sentiment(text: str) -> str:
    value = str(text or "")
    if any(keyword in value for keyword in ("不好", "不行", "失望", "质疑", "担心", "问题", "缺点", "翻车", "劝退", "差")):
        return "负面"
    if any(keyword in value for keyword in ("不错", "好用", "推荐", "惊喜", "满意", "提升", "优秀", "稳定", "强")):
        return "正面"
    return "中立"


def _fallback_report_topic(item: dict[str, Any], index: int) -> str:
    title = str(item.get("source_title") or "").strip()
    if title and title not in {"用户原话", "用户评论", "帖子正文"}:
        return f"样本反馈{index + 1}：{title[:24]}"
    quote = _truncate_text(str(item.get("content") or ""), limit=24)
    return f"样本反馈{index + 1}：{quote or '原始证据'}"


def _build_report_fallback_clusters(state: GraphState) -> list[dict[str, Any]]:
    clusters = []
    for index, item in enumerate(_collect_raw_report_evidence(state, limit=min(3, _MAX_IR_CLUSTERS))):
        quote = _truncate_text(str(item.get("content") or ""))
        if not quote:
            continue
        clusters.append({
            "topic": _fallback_report_topic(item, index),
            "sentiment": _infer_report_sentiment(quote),
            "count": 1,
            "evidence_ids": [item.get("evidence_id", "")],
            "evidence_quotes": [quote],
            "source_title": item.get("source_title", "用户原话"),
            "source_note_url": item.get("source_url", ""),
            "primary_aspects": [],
            "sub_aspects": [],
            "fallback_generated": True,
        })
    return clusters


def _build_overview_chart(state: GraphState) -> ChartSpec | None:
    post_count = len(state.get("screened_items", []))
    comment_count = len(state.get("retrieved_comments", []))
    clusters = state.get("clusters", []) or []
    summary = state.get("sentiment_summary", {}) or {}
    raw_evidence_count = len(_collect_raw_report_evidence(state, limit=20))

    if not post_count and not comment_count and not clusters and not summary and not raw_evidence_count:
        return None

    top_clusters = sorted(clusters, key=_cluster_count, reverse=True)[:3]
    top_topics = "、".join(str(item.get("topic") or "") for item in top_clusters if item.get("topic"))
    quote_count = sum(len(item.get("evidence_quotes", []) or item.get("quotes", []) or []) for item in clusters)
    if not quote_count:
        quote_count = raw_evidence_count
    sentiment_text = " / ".join(
        f"{label}{summary.get(label)}"
        for label in ("正面", "负面", "中立")
        if summary.get(label) is not None
    )
    if not sentiment_text and summary:
        sentiment_text = " / ".join(f"{label}{value}" for label, value in summary.items())

    data = [
        {
            "label": "样本规模",
            "value": f"{post_count} 篇帖子 / {comment_count} 条评论",
            "insight": "当前样本适合判断舆论方向和主要风险，样本较小时不宜直接外推为全网比例。",
        }
    ]
    if sentiment_text:
        data.append({
            "label": "情绪口径",
            "value": sentiment_text,
            "insight": "该统计来自当前分析链路的情绪归类，只作为倾向参考，不等同于帖子或评论总量占比。",
        })
    if top_topics:
        data.append({
            "label": "高频议题",
            "value": top_topics,
            "insight": "高频议题决定正文分析优先级，也代表用户做购买判断时最容易被影响的风险点。",
        })
    if quote_count:
        data.append({
            "label": "证据密度",
            "value": f"{quote_count} 条用户原话",
            "insight": "证据越集中，越适合支撑问题机制分析；但仍需结合样本来源判断代表性。",
        })
    if not any("口径" in str(row.get("label", "")) or "样本" in str(row.get("label", "")) for row in data):
        data.append({
            "label": "统计口径",
            "value": "当前抓取样本",
            "insight": "报告基于当前检索与筛选结果生成，适合识别方向，不代表全网比例。",
        })
    if len(data) < 3:
        data.append({
            "label": "统计口径",
            "value": "当前样本倾向参考",
            "insight": "样本量或观点聚类不足时，结构化报告会优先保留证据来源和样本限制说明。",
        })

    return ChartSpec(id="chart_overview", type="table", title="舆情指标概览", data=data)


def _build_report_context(state: GraphState) -> dict[str, Any]:
    """Build compact, token-bounded context and deterministic citation registry."""
    clusters = (state.get("clusters", []) or [])[:_MAX_IR_CLUSTERS]
    if not clusters:
        clusters = _build_report_fallback_clusters(state)[:_MAX_IR_CLUSTERS]
        if clusters:
            logger.warning(
                "[Synthesis][ReportIR] clusters 为空，已从原始证据恢复 {} 个上下文观点簇",
                len(clusters),
            )
    citations: list[Citation] = []
    seen_quotes: set[tuple[str, str]] = set()
    cluster_citation_ids: dict[str, list[str]] = {}
    cluster_id_by_topic: dict[str, str] = {}
    evidence_by_id = {
        item.get("evidence_id"): item
        for item in state.get("evidence_registry", []) or []
        if isinstance(item, dict) and item.get("evidence_id")
    }
    evidence_id_to_citation_id: dict[str, str] = {}

    def add_citation(
        *,
        cluster_id: str,
        topic: str,
        sentiment: str,
        source_title: str,
        source_url: str,
        quote: str,
        evidence_id: str = "",
    ) -> str:
        quote_text = _truncate_text(quote)
        if not quote_text:
            return ""
        dedupe_key = (cluster_id, quote_text)
        if dedupe_key in seen_quotes:
            if evidence_id:
                for citation in citations:
                    if citation.cluster_id == cluster_id and citation.quote == quote_text:
                        evidence_id_to_citation_id[evidence_id] = citation.id
                        return citation.id
            return ""
        seen_quotes.add(dedupe_key)
        citation = Citation(
            id=f"cit_{len(citations)}",
            cluster_id=cluster_id,
            topic=topic,
            sentiment=sentiment or "中立",
            source_title=source_title or "用户原话",
            source_url=source_url or "",
            quote=quote_text,
        )
        citations.append(citation)
        cluster_citation_ids.setdefault(cluster_id, []).append(citation.id)
        if evidence_id:
            evidence_id_to_citation_id[evidence_id] = citation.id
        return citation.id

    compact_clusters = []
    for idx, cluster in enumerate(clusters):
        cluster_id = f"cl_{idx}"
        topic = str(cluster.get("topic") or f"观点{idx + 1}")
        sentiment = str(cluster.get("sentiment") or "中立")
        cluster_id_by_topic[topic] = cluster_id
        cluster_citation_ids[cluster_id] = []

        evidence_ids = cluster.get("evidence_ids", []) or []
        quotes = cluster.get("evidence_quotes") or cluster.get("quotes") or []
        if evidence_ids:
            for evidence_id in evidence_ids[:_MAX_QUOTES_PER_CLUSTER]:
                evidence = evidence_by_id.get(evidence_id, {})
                quote = evidence.get("content") or (quotes[0] if quotes else "")
                add_citation(
                    cluster_id=cluster_id,
                    topic=topic,
                    sentiment=sentiment,
                    source_title=evidence.get("source_title") or cluster.get("source_title", ""),
                    source_url=evidence.get("source_url") or cluster.get("source_note_url", ""),
                    quote=quote,
                    evidence_id=evidence_id,
                )
        else:
            for quote in quotes[:_MAX_QUOTES_PER_CLUSTER]:
                add_citation(
                    cluster_id=cluster_id,
                    topic=topic,
                    sentiment=sentiment,
                    source_title=cluster.get("source_title", ""),
                    source_url=cluster.get("source_note_url", ""),
                    quote=quote,
                )

        compact_clusters.append({
            "id": cluster_id,
            "topic": topic,
            "sentiment": sentiment,
            "count": _cluster_count(cluster),
            "trend": cluster.get("trend", ""),
            "primary_aspects": cluster.get("primary_aspects", [])[:2],
            "sub_aspects": cluster.get("sub_aspects", [])[:4],
            "evidence_ids": evidence_ids[:5],
            "citation_ids": cluster_citation_ids[cluster_id],
        })

    for ref in state.get("references", []) or []:
        topic = str(ref.get("topic") or "")
        cluster_id = cluster_id_by_topic.get(topic)
        if not cluster_id:
            continue
        for quote in (ref.get("quotes") or ref.get("evidence_quotes") or [])[:_MAX_QUOTES_PER_CLUSTER]:
            add_citation(
                cluster_id=cluster_id,
                topic=topic,
                sentiment=str(ref.get("sentiment") or "中立"),
                source_title=ref.get("source_title", ""),
                source_url=ref.get("source_note_url", ""),
                quote=quote,
            )

    if not citations and compact_clusters:
        raw_evidence = _collect_raw_report_evidence(state, limit=len(compact_clusters))
        for idx, item in enumerate(raw_evidence):
            row = compact_clusters[idx % len(compact_clusters)]
            add_citation(
                cluster_id=row["id"],
                topic=row["topic"],
                sentiment=row["sentiment"],
                source_title=str(item.get("source_title") or ""),
                source_url=str(item.get("source_url") or ""),
                quote=str(item.get("content") or ""),
                evidence_id=str(item.get("evidence_id") or ""),
            )

    for row in compact_clusters:
        row["citation_ids"] = cluster_citation_ids.get(row["id"], [])

    content_time_analysis = deepcopy(state.get("content_time_analysis", {}) or {})
    if content_time_analysis.get("events"):
        for event in content_time_analysis.get("events", []):
            event["citation_ids"] = [
                cid for cid in (evidence_id_to_citation_id.get(eid) for eid in event.get("evidence_ids", []))
                if cid
            ]

    overview_chart = _build_overview_chart(state)
    return {
        "query": state.get("user_query_raw", ""),
        "intent": state.get("intent", "general"),
        "post_count": len(state.get("screened_items", [])),
        "comment_count": len(state.get("retrieved_comments", [])),
        "sentiment_summary": state.get("sentiment_summary", {}) or {},
        "temporal_context": state.get("temporal_context", {}) or {},
        "content_time_analysis": content_time_analysis,
        "clusters": compact_clusters,
        "citations": [
            {"id": c.id, "cluster_id": c.cluster_id, "quote": c.quote}
            for c in citations
        ],
        "allowed_cluster_ids": [row["id"] for row in compact_clusters],
        "allowed_citation_ids": [c.id for c in citations],
        "required_cluster_ids": [
            row["id"] for row in compact_clusters if int(row.get("count", 1)) >= 2
        ],
        "default_charts": [overview_chart.model_dump()] if overview_chart else [],
        "_citation_registry": citations,
    }


def _normalize_enum(value: Any, aliases: dict[str, str], default: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return default
    return aliases.get(raw) or aliases.get(raw.lower()) or default


def _normalize_section_type(section: dict[str, Any], index: int) -> str:
    raw = str(section.get("type") or "").strip()
    if raw:
        normalized = _SECTION_TYPE_ALIASES.get(raw) or _SECTION_TYPE_ALIASES.get(raw.lower())
        if normalized:
            return normalized

    title = str(section.get("title") or "")
    if "整体印象" in title or "概览" in title or "总览" in title:
        return "overview"
    if "总结" in title or "结论" in title or "建议" in title:
        return "recommendation"
    if "风险" in title or "预警" in title:
        return "risk"
    if "附录" in title:
        return "appendix"
    return "overview" if index == 0 else "analysis"


def _normalize_report_ir_payload(raw_data: dict[str, Any]) -> dict[str, Any]:
    """Normalize common LLM enum drift before strict Pydantic validation."""
    payload = deepcopy(raw_data)

    sections = payload.get("sections")
    if isinstance(sections, list):
        for idx, section in enumerate(sections):
            if not isinstance(section, dict):
                continue
            section["type"] = _normalize_section_type(section, idx)

            cids = section.get("cluster_ids")
            if isinstance(cids, list):
                section["cluster_ids"] = [str(c) for c in cids]

            blocks = section.get("blocks")
            if isinstance(blocks, list):
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    default_type = "list" if block.get("items") else "paragraph"
                    block["type"] = _normalize_enum(
                        block.get("type"),
                        _BLOCK_TYPE_ALIASES,
                        default_type,
                    )
                    bids = block.get("citation_ids")
                    if isinstance(bids, list):
                        block["citation_ids"] = [str(b) for b in bids]

    charts = payload.get("charts")
    if isinstance(charts, list):
        for chart in charts:
            if isinstance(chart, dict):
                chart["type"] = _normalize_enum(chart.get("type"), _CHART_TYPE_ALIASES, "bar")

    return payload


def _validation_error_issues(error: ValidationError) -> list[str]:
    issues = []
    for item in error.errors():
        loc = ".".join(str(part) for part in item.get("loc", [])) or "root"
        message = item.get("msg", "validation error")
        bad_input = item.get("input")
        if bad_input is not None:
            issues.append(f"{loc}: {message}; input={bad_input!r}")
        else:
            issues.append(f"{loc}: {message}")
    return issues


def _coerce_report_ir(
    raw_data: dict[str, Any],
    *,
    metadata: ReportMetadata,
    citations: list[Citation],
) -> ReportIR:
    payload = _normalize_report_ir_payload(raw_data)
    payload.pop("metadata", None)
    payload.pop("citations", None)
    payload.setdefault("version", "1.0")
    payload["metadata"] = metadata.model_dump()
    payload["citations"] = [citation.model_dump() for citation in citations]
    return ReportIR.model_validate(payload)


def _sanitize_report_ir(report: ReportIR, context: dict[str, Any]) -> ReportIR:
    allowed_cluster_ids = set(context.get("allowed_cluster_ids", []))
    allowed_citation_ids = set(context.get("allowed_citation_ids", []))

    for card in report.summary_cards:
        card.supporting_cluster_ids = [
            cid for cid in card.supporting_cluster_ids if cid in allowed_cluster_ids
        ]
    for section in report.sections:
        section.cluster_ids = [cid for cid in section.cluster_ids if cid in allowed_cluster_ids]
        section.blocks = [
            block for block in section.blocks
            if block.text.strip() or block.items
        ]
        for block in section.blocks:
            block.citation_ids = [
                cid for cid in block.citation_ids if cid in allowed_citation_ids
            ]
    if not report.charts and context.get("default_charts"):
        report.charts = [ChartSpec.model_validate(chart) for chart in context["default_charts"]]
    return report


def _chart_row_value(row: dict[str, Any], key: str) -> Any:
    if key == "label":
        return row.get("label", row.get("name", ""))
    if key == "value":
        return row.get("value", row.get("count", ""))
    return row.get(key, "")


def _report_ir_issues(report: ReportIR, context: dict[str, Any]) -> list[str]:
    issues = []
    all_report_texts: list[tuple[str, str]] = [("标题", report.title)]
    all_report_texts.extend((f"摘要「{card.label}」", card.value) for card in report.summary_cards)
    for section in report.sections:
        all_report_texts.append((f"章节「{section.title}」标题", section.title))
        for block in section.blocks:
            if block.text:
                all_report_texts.append((f"章节「{section.title}」正文", block.text))
            for item in block.items:
                all_report_texts.append((f"章节「{section.title}」列表项", str(item)))
    for location, text in all_report_texts:
        banned_hits = [term for term in _BANNED_TEMPORAL_REPORT_TERMS if term in str(text)]
        if banned_hits:
            issues.append(f"{location}包含禁用时间/生命周期表述: {', '.join(banned_hits)}")

    if not report.title.strip():
        issues.append("缺少报告标题")
    if len(report.sections) < 3:
        issues.append("sections 至少需要包含整体印象、主体分析和总结建议")
    analysis_sections = [section for section in report.sections if section.type == "analysis"]
    if len(analysis_sections) < 2:
        issues.append("至少需要 2 个 analysis 类型的主体分析章节")

    titles = [section.title for section in report.sections]
    if not any("整体印象" in title for title in titles):
        issues.append("缺少「整体印象」章节")
    if titles and ("总结" not in titles[-1] and "综合建议" not in titles[-1]):
        issues.append("末章必须是「总结」或「综合建议」")

    content_time_analysis = context.get("content_time_analysis") or {}
    content_events = content_time_analysis.get("events") or []
    if content_time_analysis.get("available") is True and content_events:
        temporal_sections = [section for section in report.sections if "内容事件演化" in section.title]
        if not temporal_sections:
            issues.append("缺少「内容事件演化」章节，必须基于 content_time_analysis.events 撰写")
        else:
            temporal_section = temporal_sections[0]
            if temporal_section.type != "analysis":
                issues.append("「内容事件演化」章节的 section.type 必须是 analysis")
            event_titles = {str(event.get("title") or "").strip() for event in content_events}
            used_event_title = any(
                block.type == "subheading" and str(block.text).strip() in event_titles
                for block in temporal_section.blocks
            )
            if not used_event_title:
                issues.append("「内容事件演化」章节需要使用 content_time_analysis.events 中的事件标题作为小标题")
            event_citation_ids = {
                cid
                for event in content_events
                for cid in (event.get("citation_ids") or [])
                if isinstance(cid, str) and cid
            }
            if event_citation_ids:
                used_citation_ids = {
                    cid for block in temporal_section.blocks for cid in block.citation_ids
                }
                if not used_citation_ids.intersection(event_citation_ids):
                    issues.append("「内容事件演化」章节必须引用 content_time_analysis.events 对应的 citation_ids")

    if len(report.summary_cards) < _MIN_SUMMARY_CARDS:
        issues.append(
            f"关键摘要至少需要 {_MIN_SUMMARY_CARDS} 条，覆盖整体倾向、核心风险、购买决策和样本限制"
        )

    summary_labels = "".join(card.label for card in report.summary_cards)
    summary_requirements = [
        (("整体", "倾向", "态势"), "整体倾向"),
        (("风险", "问题", "痛点", "核心"), "核心风险"),
        (("购买", "建议", "决策", "人群", "适合"), "购买决策"),
        (("样本", "限制", "局限", "口径", "置信"), "样本限制/统计口径"),
    ]
    for keywords, label in summary_requirements:
        if not any(keyword in summary_labels for keyword in keywords):
            issues.append(f"关键摘要缺少「{label}」维度")

    for card in report.summary_cards:
        if _is_thin_summary_value(card.value):
            issues.append(
                f"关键摘要「{card.label}」过于单薄，需要写出现象、原因/证据和决策含义"
            )

    if not report.charts:
        issues.append("数据概览缺失，至少需要一个 table 类型的舆情指标概览")
    else:
        has_insight_table = False
        has_scope_row = False
        has_only_count_rows = True
        for chart in report.charts:
            rows = [row for row in chart.data if isinstance(row, dict)]
            if not rows:
                continue
            if chart.type == "table" and len(rows) >= 3:
                if any(_chart_row_value(row, "insight") or _chart_row_value(row, "basis") for row in rows):
                    has_insight_table = True
            for row in rows:
                keys = {str(key) for key in row.keys() if row.get(key) not in (None, "")}
                if keys - {"label", "name", "value", "count"}:
                    has_only_count_rows = False
                row_text = "".join(str(_chart_row_value(row, key)) for key in ("label", "value", "insight", "basis"))
                if any(keyword in row_text for keyword in ("样本", "口径", "统计", "置信", "限制", "局限")):
                    has_scope_row = True
        if not has_insight_table:
            issues.append("数据概览必须包含 table 类型的舆情指标概览，并为每行提供 insight 解读")
        if has_only_count_rows:
            issues.append("数据概览不能只列情绪数量，需要加入统计口径、样本规模和解读")
        if not has_scope_row:
            issues.append("数据概览必须说明统计口径或样本限制，避免把倾向统计误读为全网比例")

    covered_clusters = set()
    for section in report.sections:
        covered_clusters.update(section.cluster_ids)
        if not section.blocks:
            issues.append(f"章节「{section.title}」没有内容块")
        if section.type == "analysis":
            is_content_time_section = "内容事件演化" in section.title
            min_subheading_count = 1 if is_content_time_section else 2
            min_paragraph_chars = 80 if is_content_time_section else _MIN_ANALYSIS_PARAGRAPH_CHARS

            if _is_generic_analysis_title(section.title):
                issues.append(f"分析章节标题「{section.title}」过于标签化，需要改成洞察型判断句")

            subheading_indexes = [
                idx for idx, block in enumerate(section.blocks)
                if block.type == "subheading" and block.text.strip()
            ]
            if len(subheading_indexes) < min_subheading_count:
                issues.append(f"分析章节「{section.title}」至少需要 {min_subheading_count} 个洞察型 subheading")

            for sub_idx in subheading_indexes:
                subheading = section.blocks[sub_idx]
                if _is_generic_analysis_title(subheading.text):
                    issues.append(f"小标题「{subheading.text}」过于标签化，需要写成带判断的洞察句")

                next_paragraph = None
                for following in section.blocks[sub_idx + 1:]:
                    if following.type == "subheading":
                        break
                    if following.type == "paragraph" and following.text.strip():
                        next_paragraph = following
                        break
                if next_paragraph is None:
                    issues.append(f"小标题「{subheading.text}」后缺少分析段落")
                    continue
                paragraph_text = re.sub(r"\s+", "", next_paragraph.text)
                if len(paragraph_text) < min_paragraph_chars:
                    issues.append(
                        f"小标题「{subheading.text}」后的分析段落过短，至少需要 {min_paragraph_chars} 个中文字符"
                    )
                if context.get("allowed_citation_ids"):
                    if not next_paragraph.citation_ids:
                        issues.append(f"小标题「{subheading.text}」后的分析段落缺少 citation_ids")
                    elif not _contains_quote_fragment(
                        next_paragraph.text,
                        next_paragraph.citation_ids,
                        context,
                    ):
                        issues.append(f"小标题「{subheading.text}」后的分析段落没有自然嵌入用户原话")

        if section.type == "analysis" and context.get("allowed_citation_ids"):
            has_citation = any(block.citation_ids for block in section.blocks)
            if not has_citation:
                issues.append(f"分析章节「{section.title}」缺少 citation_ids")

    missing_required = [
        cid for cid in context.get("required_cluster_ids", [])
        if cid not in covered_clusters
    ]
    if missing_required:
        issues.append(f"高频观点未覆盖: {', '.join(missing_required[:6])}")
    return issues


def _minimal_report_ir(state: GraphState, final_text: str = "") -> ReportIR:
    metadata = _build_report_metadata(state)
    title = "舆情分析结果"
    text = final_text.strip() or "未找到与查询相关且有效的内容，请尝试更换关键词搜索。"
    return ReportIR(
        title=title,
        metadata=metadata,
        sections=[{
            "id": "sec_result",
            "title": "整体印象",
            "type": "overview",
            "cluster_ids": [],
            "blocks": [{"type": "paragraph", "text": text, "citation_ids": []}],
        }],
        citations=[],
    )


async def _generate_report_ir(state: GraphState) -> ReportIR:
    context = _build_report_context(state)
    metadata = _build_report_metadata(state)
    citations = context.pop("_citation_registry")
    prompt_context = {k: v for k, v in context.items() if not k.startswith("_")}

    prompt = SYNTHESIS_REPORT_IR_PROMPT.format(
        query=state.get("user_query_raw", ""),
        report_outline=json.dumps(state.get("_report_outline", {}), ensure_ascii=False, indent=2),
        report_context_json=json.dumps(prompt_context, ensure_ascii=False, indent=2),
    )

    raw_text, _ = await _invoke_report_ir_llm(
        prompt,
        context=context,
        stage="initial",
    )
    try:
        raw_data = _extract_json_object(raw_text)
    except ValueError as exc:
        raw_data = await _repair_unparseable_report_ir(
            raw_text=raw_text,
            parse_error=exc,
            context=context,
            prompt_context=prompt_context,
        )

    try:
        report = _sanitize_report_ir(
            _coerce_report_ir(raw_data, metadata=metadata, citations=citations),
            context,
        )
        issues = _report_ir_issues(report, context)
    except ValidationError as e:
        report = None
        issues = _validation_error_issues(e)

    if not issues:
        return report

    logger.warning(f"[Synthesis][ReportIR] 首次校验未通过: {issues}")
    repair_prompt = SYNTHESIS_REPORT_IR_REPAIR_PROMPT.format(
        issues="\n".join(f"- {issue}" for issue in issues),
        allowed_cluster_ids=", ".join(context.get("allowed_cluster_ids", [])),
        allowed_citation_ids=", ".join(context.get("allowed_citation_ids", [])),
        report_ir_json=json.dumps(raw_data, ensure_ascii=False, indent=2),
    )
    repair_text, _ = await _invoke_report_ir_llm(
        repair_prompt,
        context=context,
        stage="validation_repair",
    )
    try:
        repaired_data = _extract_json_object(repair_text)
    except ValueError as exc:
        repaired_data = await _repair_unparseable_report_ir(
            raw_text=repair_text,
            parse_error=exc,
            context=context,
            prompt_context=prompt_context,
        )
    try:
        repaired = _sanitize_report_ir(
            _coerce_report_ir(repaired_data, metadata=metadata, citations=citations),
            context,
        )
    except ValidationError as e:
        raise ValueError(f"Report IR schema 校验失败: {_validation_error_issues(e)}") from e

    repaired_issues = _report_ir_issues(repaired, context)
    if repaired_issues:
        raise ValueError(f"Report IR 校验失败: {repaired_issues}")
    return repaired


async def node_plan_outline(state: GraphState) -> dict[str, Any]:
    """Plan 节点：分析数据特点，利用 LLM 生成报告撰写策略与大纲"""
    clusters = state.get("clusters", [])
    post_count = len(state.get("screened_items", []))
    comment_count = len(state.get("retrieved_comments", []))
    sentiment_summary = state.get("sentiment_summary", {})

    round_num = state.get("_synthesis_round", 0) + 1
    feedback = state.get("_outline_feedback", "")

    # 【逃生舱】如果根本没数据，直接标记结束，不走大纲规划，给一个默认的最简框架
    if not clusters and post_count == 0:
        logger.info("[Synthesis][Plan] 无可用数据，生成默认空大纲跳过审查")
        return {
            "_synthesis_round": round_num,
            "_synthesis_done": True,
            "_report_outline": {
                "report_strategy": {
                    "overall_tone": "无数据",
                    "structure": [{"chapter": "搜索为空", "focus": "提醒用户更换关键词重新搜索", "use_clusters": []}]
                }
            }
        }

    # 给 cluster 加上 #0, #1 编号供大纲引用
    labeled_clusters = [{"id": i, **cl} for i, cl in enumerate(clusters)]

    # 根据是否有反馈，选择不同的 Prompt
    feedback = state.get("_outline_feedback", "")
    if feedback:
        # 修改模式：传递原大纲 + 反馈 + 修改原则
        previous_outline = state.get("_report_outline", {})
        prompt = SYNTHESIS_MODIFY_OUTLINE_PROMPT.format(
            previous_outline_json=json.dumps(previous_outline, ensure_ascii=False),
            feedback=feedback
        )
        logger.info(f"[Synthesis][Plan] Round {round_num}: 根据反馈修改大纲...")
    else:
        # 初始生成模式：简洁的 Prompt
        prompt = SYNTHESIS_PLAN_OUTLINE_PROMPT.format(
            query=state.get("user_query_raw", ""),
            post_count=post_count,
            comment_count=comment_count,
            sentiment_summary=json.dumps(sentiment_summary, ensure_ascii=False),
            numbered_clusters_json=json.dumps(labeled_clusters, ensure_ascii=False)
        )
        logger.info(f"[Synthesis][Plan] Round {round_num}: 开始起草报告大纲...")

    try:
        resp = await _llm_plan.ainvoke(prompt)
        outline_json = _parse_json_response(resp.content)
    except Exception as e:
        logger.error(f"[Synthesis][Plan] Error: {e}")
        outline_json = _parse_json_response("")

    return {
        "_synthesis_round": round_num,
        "_report_outline": outline_json,
        "_synthesis_done": False  # 等待审查
    }


async def node_observe_outline(state: GraphState) -> dict[str, Any]:
    """Observe 节点：检查大纲质量，标记正确和错误的章节"""
    round_num = state.get("_synthesis_round", 1)
    # 如果已经达到重试上限，或者上一节点由于完全没数据主动挂旗跳过，直接放行
    if state.get("_synthesis_done", False) or round_num >= _MAX_SYNTHESIS_ROUNDS:
        logger.info(f"[Synthesis][Observe] 放行（已达终点或最大轮次 {round_num}）")
        return {"_synthesis_done": True, "_outline_feedback": ""}

    outline = state.get("_report_outline", {})
    clusters = state.get("clusters", [])

    structure = outline.get("report_strategy", {}).get("structure", [])
    if not structure:
        return {"_synthesis_done": False, "_outline_feedback": "大纲中缺少 structure 结构部分。"}

    # ── 新增：检查末章必须存在 ──
    if len(structure) >= 1:
        last_chapter = structure[-1].get("chapter", "")
        if not last_chapter or ("总结" not in last_chapter and "综合建议" not in last_chapter):
            return {
                "_synthesis_done": False,
                "_outline_feedback": f"末章必须命名为「总结」或「综合建议」，当前末章名称：「{last_chapter}」"
            }

    # ── 新增：检查中间章节数量 ──
    if len(structure) >= 3:
        middle_chapter_count = len(structure) - 2  # 排除首章和末章
        if middle_chapter_count < 2:
            feedback = f"中间章节必须有 2~3 个，当前只有 {middle_chapter_count} 个。"
            if middle_chapter_count == 1:
                feedback += "\n建议：将当前唯一的中间章节拆分为2个独立的章节，分别从不同角度分析观点。"
            return {
                "_synthesis_done": False,
                "_outline_feedback": feedback
            }
        elif middle_chapter_count > 3:
            return {
                "_synthesis_done": False,
                "_outline_feedback": f"中间章节过多（{middle_chapter_count}个），建议合并为2~3个核心章节。"
            }

    # ── 标记章节状态 ──
    chapter_issues = {}  # {章节名: [问题列表]}
    correct_chapters = []  # 正确的章节列表

    # 提取所有被引用的 index
    referenced_indices = set()
    for i, chap in enumerate(structure):
        for idx in chap.get("use_clusters", []):
            if isinstance(idx, int):
                referenced_indices.add(idx)

    max_idx = len(clusters) - 1

    # 审查每个章节
    for i, chap in enumerate(structure):
        chap_name = chap.get("chapter", f"章节{i+1}")
        issues = []

        if 0 < i < len(structure) - 1 and _is_generic_analysis_title(chap_name):
            issues.append("主体章节标题过于标签化，必须改成洞察型判断句，说明矛盾、原因、影响或购买决策含义")

        focus = str(chap.get("focus", ""))
        if 0 < i < len(structure) - 1 and len(focus) < 45:
            issues.append("focus 过短，必须写清 2~3 个可展开的小论点，而不是只列关键词")

        # 检查 1: 幻觉防范
        for idx in chap.get("use_clusters", []):
            if not isinstance(idx, int) or idx < 0 or idx > max_idx:
                issues.append(f"引用了不存在的簇编号 #{idx}（最大编号为 #{max_idx}）")

        if issues:
            chapter_issues[chap_name] = issues
        else:
            correct_chapters.append(chap_name)

    # 检查 2: 漏审防范（全局问题，不标记到具体章节）
    missing_clusters = []
    for i, cl in enumerate(clusters):
        if cl.get("count", 1) >= 2 and i not in referenced_indices:
            topic = cl.get("topic", f"簇#{i}")
            missing_clusters.append(f"#{i} {topic}（出现{cl.get('count')}次）")

    # ── 构建结构化反馈 ──
    if not chapter_issues and not missing_clusters:
        logger.info("[Synthesis][Observe] 大纲审查通过")
        return {"_synthesis_done": True, "_outline_feedback": ""}

    # 构建反馈文本
    feedback_parts = []

    # 1. 正确的章节（保留）
    if correct_chapters:
        feedback_parts.append(f"【保留章节】以下章节无需修改：\n" + "\n".join([f"  - {name}" for name in correct_chapters]))

    # 2. 有问题的章节（需修改）
    if chapter_issues:
        feedback_parts.append("【需修改章节】以下章节存在问题：")
        for chap_name, issues in chapter_issues.items():
            feedback_parts.append(f"  - 「{chap_name}」：" + "；".join(issues))

    # 3. 遗漏的观点（需补充）
    if missing_clusters:
        feedback_parts.append(f"【遗漏观点】以下重要观点未被引用，请补充到合适章节：\n" + "\n".join([f"  - {cl}" for cl in missing_clusters]))

    feedback_text = "\n\n".join(feedback_parts)
    logger.warning(f"[Synthesis][Observe] 大纲被驳回，发现 {len(chapter_issues)} 个问题章节，{len(missing_clusters)} 个遗漏观点")

    return {"_synthesis_done": False, "_outline_feedback": feedback_text}


def _route_synthesis(state: GraphState) -> Literal["evaluate_and_score", "plan_outline"]:
    """通过审查或者超限，转去评分与撰写。否则退回重写。"""
    if state.get("_synthesis_done", False):
        return "evaluate_and_score"
    return "plan_outline"


async def _legacy_execute_markdown_report(state: GraphState, config: dict) -> dict[str, Any]:
    """Legacy Markdown writer used as a fallback when Report IR generation fails."""
    queue = config.get("configurable", {}).get("queue")
    outline = state.get("_report_outline", {})
    clusters = state.get("clusters", [])
    post_count = len(state.get("screened_items", []))
    comment_count = len(state.get("retrieved_comments", []))

    if not clusters and post_count == 0:
        logger.info("[Synthesis][Execute] 无数据场景快速返回")
        ans = "## 舆情分析结果\n\n未找到与查询相关且有效的内容，请尝试更换关键词搜索。"
        if queue:
            queue.put_nowait({"event": "report_chunk", "data": {"text": ans}})
        return {"final_answer": ans}

    clusters_json = json.dumps(clusters, ensure_ascii=False)
    outline_fmt = json.dumps(outline, ensure_ascii=False, indent=2)

    prompt = SYNTHESIS_REPORT_PROMPT.format(
        query=state.get("user_query_raw", ""),
        post_count=post_count,
        comment_count=comment_count,
        report_outline=outline_fmt,
        clusters_json=clusters_json
    )
    logger.info(f"[Synthesis][Execute] 正在发出的最终提示词 (前500字): \n{prompt[:500]}...")

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            buffer = ""
            async for chunk in _llm_report.astream(prompt):
                buffer += chunk
                cleaned = _strip_fences(buffer)
                if queue:
                    queue.put_nowait({
                        "event": "report_chunk",
                        "data": {"text": cleaned},
                    })

            final_answer = _strip_fences(buffer)
            if not final_answer:
                final_answer = "## 分析完成\n\n报告内容生成失败，请重试。"

            logger.info(f"[Synthesis][Execute] 报告生成完毕，总字数 {len(final_answer)}。")
            return {"final_answer": final_answer}

        except Exception as e:
            logger.error(f"[Synthesis][Execute] 生成中途断流，第 {attempt+1} 次失败: {e}")
            import asyncio
            if attempt < max_retries:
                if queue:
                    queue.put_nowait({"event": "progress", "data": {"message": f"遭遇网络波动重试 ({attempt+1}/{max_retries})", "progress": 82}})
                await asyncio.sleep(2)
            else:
                ans = f"## 分析异常中断\n\n多次与底层大模型通信阻断，生成发生意外，请稍后再试。错误:{str(e)}"
                if queue:
                    queue.put_nowait({"event": "progress", "data": {"message": "流式生成失败", "progress": 82}})
                return {"final_answer": ans}

    return {} # Unreachable


async def node_execute_report(state: GraphState, config: dict) -> dict[str, Any]:
    """Execute 节点：生成 Report IR v1，并渲染出兼容旧前端的 Markdown。"""
    queue = config.get("configurable", {}).get("queue")
    clusters = state.get("clusters", [])
    post_count = len(state.get("screened_items", []))

    if not clusters and post_count == 0:
        logger.info("[Synthesis][ReportIR] 无数据场景快速返回")
        report_ir = _minimal_report_ir(state)
        final_answer = render_markdown(report_ir)
        if queue:
            queue.put_nowait({"event": "report_chunk", "data": {"text": final_answer}})
        return {
            "report_ir": report_ir.model_dump(),
            "final_answer": final_answer,
        }

    try:
        report_ir = await _generate_report_ir(state)
        final_answer = render_markdown(report_ir)
        if queue:
            queue.put_nowait({"event": "report_chunk", "data": {"text": final_answer}})
        logger.info(
            f"[Synthesis][ReportIR] 报告生成完毕，sections={len(report_ir.sections)}, "
            f"citations={len(report_ir.citations)}, markdown_chars={len(final_answer)}"
        )
        return {
            "report_ir": report_ir.model_dump(),
            "final_answer": final_answer,
        }
    except Exception as e:
        logger.error(
            "[Synthesis][ReportIR] 结构化报告生成失败，回退到旧 Markdown 流程: "
            "type={}, error={!r}, clusters={}, posts={}, comments={}".format(
                type(e).__name__,
                e,
                len(state.get("clusters", []) or []),
                post_count,
                len(state.get("retrieved_comments", []) or []),
            )
        )
        if queue:
            queue.put_nowait({
                "event": "progress",
                "data": {"message": "结构化报告生成遇到波动，回退旧 Markdown 流程", "progress": 82},
            })
        legacy = await _legacy_execute_markdown_report(state, config)
        return {
            **legacy,
            "report_ir": {},
        }


async def node_evaluate_and_score(state: GraphState) -> dict[str, Any]:
    """Score 节点：抛弃 LLM，利用强规则进行增强型 40/30/15/15 加权打分，并梳理所有 references。"""
    # 如果已经有 references（完全复用模式），直接返回
    if state.get("references"):
        logger.info(f"[Synthesis][Score] 检测到预生成的 references ({len(state.get('references', []))} 个)，跳过重新计算")
        clusters = state.get("clusters", [])
        data_score = min(1.0, len(clusters) / 8.0)
        return {
            "confidence_score": data_score,
            "limitations": [],
            "references": state.get("references", [])
        }

    # 正常流程：从 clusters 计算 references
    post_count = len(state.get("screened_items", []))
    comment_count = len(state.get("retrieved_comments", []))
    clusters = state.get("clusters", [])
    sentiment_summary = state.get("sentiment_summary", {})
    limitations = []

    # 1. 维度1：数据底座（权重 40%）
    if post_count >= 5 and comment_count >= 20: data_score = 1.0
    elif post_count >= 3 and comment_count >= 12: data_score = 0.8
    elif post_count >= 2 and comment_count >= 7: data_score = 0.6
    elif post_count >= 2 and comment_count >= 5: data_score = 0.4
    elif post_count >= 1: data_score = 0.3
    else:
        data_score = 0.1
        limitations.append("极其缺乏有效的样本数据量")

    # 2. 维度2：观点丰富/发散度（权重 30%）
    cluster_count = len(clusters)
    if cluster_count >= 6: div_score = 1.0
    elif cluster_count >= 4: div_score = 0.8
    elif cluster_count >= 3: div_score = 0.6
    elif cluster_count >= 2: div_score = 0.4
    else:
        div_score = 0.2
        limitations.append("聚类出的痛点维度单一，不足刻画全貌")

    # 3. 维度3：情绪对抗度 / 辩证性（权重 15%）
    sentiments = set(sentiment_summary.keys())
    # 清洗掉一些可能奇怪的空格
    sentiments = {s.strip() for s in sentiments}
    if "正面" in sentiments and "负面" in sentiments:
        sent_score = 1.0
    elif len(sentiments) >= 2:
        sent_score = 0.7
    elif len(sentiments) == 1:
        sent_score = 0.4
        limitations.append("舆情面貌一边倒，缺乏对比和制衡性的反面视角")
    else:
        sent_score = 0.2

    # 4. 维度4：证据引用坚实度（权重 15%）
    total_quotes = sum(len(c.get("evidence_quotes", [])) for c in clusters)
    if total_quotes >= 10: ev_score = 1.0
    elif total_quotes >= 5: ev_score = 0.7
    elif total_quotes >= 2: ev_score = 0.4
    else:
        ev_score = 0.2
        limitations.append("报告由于缺乏来自用户端的原话佐证说服力受限")

    final_score = (data_score * 0.40) + (div_score * 0.30) + (sent_score * 0.15) + (ev_score * 0.15)
    final_score = round(final_score, 2)

    # 构建结构化的前端 references 下拉列表
    seen: set[str] = set()
    references: list[dict] = []
    for cl in clusters:
        url = cl.get("source_note_url", "")
        if not url: continue
        key = f"{url}|{cl.get('topic', '')}"
        if key in seen: continue
        seen.add(key)
        references.append({
            "topic": cl.get("topic", ""),
            "sentiment": cl.get("sentiment", "中立"),
            "source_note_url": url,
            "source_title": cl.get("source_title", "无标题"),
            "quotes": cl.get("evidence_quotes", []),
        })

    logger.info(f"[Synthesis][Score] 加权总得分={final_score} [Data:{data_score}, Div:{div_score}, Sent:{sent_score}, Evid:{ev_score}]")
    return {
        "confidence_score": final_score,
        "limitations": limitations,
        "references": references
    }


def build_synthesis_graph():
    """构建 Synthesis 子图

    流程：
      plan_outline (写大纲) -> observe_outline (质检大纲)
          -> { if 不合格 -> 回炉重写 / if 合格 -> } evaluate_and_score (四维度算分和整理引用)
          -> execute_report (生成 Report IR 并渲染 Markdown) -> END
    """
    from langgraph.graph import StateGraph

    g = StateGraph(GraphState)

    g.add_node("plan_outline", node_plan_outline)
    g.add_node("observe_outline", node_observe_outline)
    g.add_node("execute_report", node_execute_report)
    g.add_node("evaluate_and_score", node_evaluate_and_score)

    g.set_entry_point("plan_outline")
    g.add_edge("plan_outline", "observe_outline")

    # 只有通过了规则检验才能去一门心思地去写文，防止烂提纲和发散幻觉
    g.add_conditional_edges(
        "observe_outline",
        _route_synthesis,
        {
            "evaluate_and_score": "evaluate_and_score",
            "plan_outline": "plan_outline"
        }
    )

    g.add_edge("evaluate_and_score", "execute_report")
    g.add_edge("execute_report", "__end__")

    return g.compile()
