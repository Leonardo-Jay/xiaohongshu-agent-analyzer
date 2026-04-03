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
    """仅从 clusters 构建 references + 规则计算置信度。
    报告生成已由 synthesize_from_streaming 流式完成。
    """
    clusters = state.get("clusters", [])
    post_count = len(state.get("screened_items", []))
    comment_count = len(state.get("retrieved_comments", []))

    if not clusters and post_count == 0:
        return {
            "final_answer": "## 分析结果\n\n未找到与查询相关的内容，请尝试更换关键词。",
            "confidence_score": 0.0,
            "limitations": ["搜索结果为空，无法进行舆情分析。"],
            "references": [],
        }

    # 规则置信度（不再调用 LLM，避免额外等待）
    if post_count >= 5 and comment_count >= 30:
        confidence_score = 0.8
    elif post_count >= 3 and comment_count >= 10:
        confidence_score = 0.65
    elif post_count >= 2:
        confidence_score = 0.5
    else:
        confidence_score = 0.35

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

    logger.info(f"[Synthesis] references={len(references)}，置信度={confidence_score}")
    return {
        "confidence_score": confidence_score,
        "limitations": [],
        "references": references,
    }


async def synthesize_from_streaming(state: GraphState, queue) -> dict:
    """流式生成报告：边生成边推送 chunks 给前端，返回完整报告文本。

    此函数生成报告的完整 markdown 文本并实时推送到队列。
    references 等元数据仍然通过原来 synthesize() 返回。
    """
    clusters = state.get("clusters", [])
    post_count = len(state.get("screened_items", []))
    comment_count = len(state.get("retrieved_comments", []))

    if not clusters and post_count == 0:
        return {"final_answer": ""}

    clusters_json = json.dumps(clusters, ensure_ascii=False)
    fmt_args = dict(
        query=state.get("user_query_raw", ""),
        post_count=post_count,
        comment_count=comment_count,
        clusters_json=clusters_json,
    )
    report_prompt = SYNTHESIS_REPORT_PROMPT.format(**fmt_args)

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            buffer = ""
            async for chunk in _llm_report.astream(report_prompt):
                buffer += chunk
                cleaned = _strip_fences(buffer)
                queue.put_nowait({
                    "event": "report_chunk",
                    "data": {"text": cleaned},
                })
            
            final_answer = _strip_fences(buffer)
            if not final_answer:
                final_answer = "## 分析完成\n\n报告内容为空，请重试。"
            logger.info(f"[Synthesis] 流式报告生成成功，字数={len(final_answer)}")
            return {"final_answer": final_answer}

        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            logger.error(f"[Synthesis] 流式生成第 {attempt+1} 次失败: {str(e)}\n{error_details}")
            
            if attempt < max_retries:
                queue.put_nowait({"event": "progress", "data": {"message": f"报告生成重试中 ({attempt+1}/{max_retries})...", "progress": 82}})
                await asyncio.sleep(2)  # 等待 2 秒后重试
            else:
                queue.put_nowait({"event": "progress", "data": {"message": "报告生成失败，请重试", "progress": 82}})
                return {"final_answer": f"## 分析出错\n\n流式生成报告重试多次后仍失败，建议检查网络或密钥。错误详情: {str(e)}"}
