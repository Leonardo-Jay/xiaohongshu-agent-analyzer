"""Synthesis Agent — 汇总所有分析结果，生成最终报告。
实现 main_graph.py 中 synthesize_answer 节点的真实逻辑。

两次并发 LLM 调用：
  1. meta 调用：返回小型 JSON（confidence_score + limitations）
  2. report 调用：直接返回纯 Markdown 报告文本，不包在 JSON 里
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from loguru import logger

from app.models.schemas import GraphState
from app.prompts.templates import SYNTHESIS_META_PROMPT, SYNTHESIS_REPORT_PROMPT
from app.tools.llm import create_llm

_llm_meta = create_llm(temperature=0)
_llm_report = create_llm(temperature=0.3)


def _fix_llm_json(text: str) -> str:
    """清理 LLM 输出：去除代码围栏，转义字符串值内的字面控制字符。"""
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    text = re.sub(r'```\s*$', '', text.strip(), flags=re.MULTILINE).strip()
    result = []
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            result.append(ch)
            escaped = False
        elif ch == '\\' and in_string:
            result.append(ch)
            escaped = True
        elif ch == '"':
            in_string = not in_string
            result.append(ch)
        elif in_string and ch == '\n':
            result.append('\\n')
        elif in_string and ch == '\r':
            result.append('\\r')
        elif in_string and ch == '\t':
            result.append('\\t')
        else:
            result.append(ch)
    return ''.join(result)


def _strip_fences(text: str) -> str:
    """去除 LLM 可能加的 markdown/代码围栏。"""
    text = re.sub(r'^```(?:markdown)?\s*', '', text.strip(), flags=re.MULTILINE)
    return re.sub(r'```\s*$', '', text.strip(), flags=re.MULTILINE).strip()


async def synthesize(state: GraphState) -> dict[str, Any]:
    """两次并发 LLM 调用：meta（小型 JSON）+ report（纯 Markdown）。"""
    clusters = state.get("clusters", [])
    post_count = len(state.get("screened_items", []))
    comment_count = len(state.get("retrieved_comments", []))

    if not clusters and post_count == 0:
        return {
            "final_answer": "## 分析结果\n\n未找到与查询相关的内容，请尝试更换关键词。",
            "confidence_score": 0.0,
            "limitations": ["搜索结果为空，无法进行舆情分析。"],
        }

    clusters_json = json.dumps(clusters, ensure_ascii=False, indent=2)
    fmt_args = dict(
        query=state.get("user_query_raw", ""),
        post_count=post_count,
        comment_count=comment_count,
        clusters_json=clusters_json,
    )

    meta_prompt = SYNTHESIS_META_PROMPT.format(**fmt_args)
    report_prompt = SYNTHESIS_REPORT_PROMPT.format(**fmt_args)

    try:
        meta_resp, report_resp = await asyncio.gather(
            _llm_meta.ainvoke(meta_prompt),
            _llm_report.ainvoke(report_prompt),
        )
    except Exception as e:
        logger.error(f"[Synthesis] LLM 调用失败: {e}")
        return {
            "final_answer": "## 分析失败\n\n生成报告时发生内部错误，请稍后重试。",
            "confidence_score": 0.0,
            "limitations": [str(e)],
        }

    # 解析 meta（小型 JSON）
    confidence_score = 0.5
    limitations: list[str] = []
    try:
        meta = json.loads(_fix_llm_json(meta_resp.content))
        confidence_score = float(meta.get("confidence_score", 0.5))
        lim = meta.get("limitations", "")
        if lim:
            limitations = [lim]
    except Exception as e:
        logger.warning(f"[Synthesis] meta JSON 解析失败（忽略）: {e}")

    # 报告：纯 Markdown，直接使用
    final_answer = _strip_fences(report_resp.content)
    if not final_answer:
        final_answer = "## 分析完成\n\n报告内容为空，请重试。"

    # 从 clusters 构建结构化 references，供前端展示原贴引用
    seen: set[str] = set()
    references: list[dict] = []
    for cl in clusters:
        url = cl.get("source_note_url", "")
        if not url:
            continue
        key = f"{url}|{cl.get('topic', '')}"
        if key in seen:
            continue
        seen.add(key)
        references.append({
            "topic": cl.get("topic", ""),
            "sentiment": cl.get("sentiment", "中立"),
            "source_note_url": url,
            "source_title": cl.get("source_title", "无标题"),
            "quotes": cl.get("evidence_quotes", []),
        })

    logger.info(f"[Synthesis] 报告生成完毕，字数={len(final_answer)}，引用数={len(references)}")
    return {
        "final_answer": final_answer,
        "confidence_score": confidence_score,
        "limitations": limitations,
        "references": references,
    }
