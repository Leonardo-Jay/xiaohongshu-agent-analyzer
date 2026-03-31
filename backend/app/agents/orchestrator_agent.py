"""Orchestrator Agent — 意图识别、查询改写与搜索计划生成。
实现 main_graph.py 中 classify_intent / rewrite_and_plan 节点的真实逻辑。
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger

from app.models.schemas import GraphState
from app.prompts.templates import CLASSIFY_PROMPT, REWRITE_PROMPT
from app.tools.llm import create_llm

_llm = create_llm(temperature=0)


async def classify_intent(state: GraphState) -> dict[str, Any]:
    """调用 Claude 判断意图、提取产品实体和别名，并改写查询。"""
    query = state.get("user_query_raw", "")
    prompt = CLASSIFY_PROMPT.format(query=query)

    try:
        resp = await _llm.ainvoke(prompt)
        data = json.loads(resp.content)
        intent = data.get("intent", "general")
        product_entities = data.get("product_entities", [])
        aliases = data.get("aliases", [])
        rewritten = data.get("rewritten_query", query)
        logger.info(f"[Orchestrator] intent={intent}, entities={product_entities}")
    except Exception as e:
        logger.warning(f"[Orchestrator] classify_intent failed, fallback: {e}")
        intent = "general"
        product_entities = []
        aliases = []
        rewritten = query

    return {
        "intent": intent,
        "product_entities": product_entities,
        "aliases": aliases,
        "user_query_rewritten": rewritten,
    }


async def rewrite_and_plan(state: GraphState) -> dict[str, Any]:
    """基于 intent + entities 生成 3~5 个扩展搜索词，填充 query_plan。"""
    query = state.get("user_query_raw", "")
    intent = state.get("intent", "general")
    entities = state.get("product_entities", [])
    aliases = state.get("aliases", [])

    prompt = REWRITE_PROMPT.format(
        query=query,
        intent=intent,
        entities="、".join(entities) if entities else query,
        aliases="、".join(aliases) if aliases else "无",
    )

    try:
        resp = await _llm.ainvoke(prompt)
        data = json.loads(resp.content)
        query_plan: list[str] = data.get("query_plan", [])
        # 确保原始查询也在搜索词列表中
        rewritten = state.get("user_query_rewritten", query)
        if rewritten not in query_plan:
            query_plan.insert(0, rewritten)
        query_plan = query_plan[:5]  # 最多 5 个
        logger.info(f"[Orchestrator] query_plan={query_plan}")
    except Exception as e:
        logger.warning(f"[Orchestrator] rewrite_and_plan failed, fallback: {e}")
        query_plan = [state.get("user_query_rewritten", query)]

    return {"query_plan": query_plan}
