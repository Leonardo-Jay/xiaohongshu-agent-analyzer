"""Orchestrator Subgraph — 意图识别 ReAct Agent
职责：通过 ReAct 循环深入分析用户意图，生成高质量的意图分析结果

核心流程：
  1. Reasoning: 分析用户查询，识别意图、实体、关注方面和用户需求
  2. Action: 从不同角度重新审视查询，补充缺失的分析维度
  3. Observation: 评估意图识别质量，决定是否继续推理

循环终止条件:
  - 意图分析质量分数达到阈值（>= 0.8）
  - 达到最大推理轮次（2轮）
"""
from __future__ import annotations

import json
from typing import Any, Literal

from langgraph.graph import StateGraph
from loguru import logger

from app.models.schemas import GraphState
from app.prompts.templates import CLASSIFY_PROMPT, INTENT_ACTION_PROMPT, INTENT_OBSERVATION_PROMPT
from app.tools.llm import create_llm

_llm_reasoning = create_llm(temperature=0)
_llm_action = create_llm(temperature=0)
_llm_observation = create_llm(temperature=0)

_MAX_INTENT_ROUNDS = 2  # 最多 2 轮 ReAct 循环


def _parse_reasoning_json(text: str) -> dict[str, Any]:
    """清理并解析 LLM 返回的 JSON 推理结果。"""
    import re
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    text = re.sub(r'```\s*$', '', text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception:
        import re
        m = re.search(r'\{[^}]+\}', text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return {}


async def node_reasoning(state: GraphState) -> dict[str, Any]:
    """ReAct Reasoning 节点：深度意图分析

    功能：
      - 识别意图类型（产品比较、质量问题、性价比、用户体验等）
      - 提取产品实体和别名
      - 识别用户关注的核心方面（价格、质量、功能等）
      - 提取用户需求和痛点
      - 生成初步的改写查询
      - 构建搜索上下文，指导Retrieve Agent
    """
    query = state.get("user_query_raw", "")
    round_num = state.get("_intent_round", 0) + 1

    prompt = CLASSIFY_PROMPT.format(query=query)

    try:
        resp = await _llm_reasoning.ainvoke(prompt)
        data = _parse_reasoning_json(resp.content)

        # 从数据中提取意图分析结果
        intent = data.get("intent", "general")
        intent_confidence = float(data.get("intent_confidence", 0.0))
        entities = data.get("product_entities", [])
        aliases = data.get("aliases", [])
        entities_confidence = float(data.get("entities_confidence", 0.0))
        key_aspects = data.get("key_aspects", [])
        user_needs = data.get("user_needs", [])
        rewritten = data.get("rewritten_query", query)
        search_context = data.get("search_context", {})

        # 计算初步质量分数
        intent_analysis_score = (
            (intent_confidence * 0.4) +
            (entities_confidence * 0.3) +
            (min(len(user_needs), 3) / 3.0 * 0.3)
        )

        logger.info(
            f"[Orchestrator][Reasoning] Round {round_num}: "
            f"intent={intent}, confidence={intent_confidence:.2f}, "
            f"entities={entities}, score={intent_analysis_score:.2f}"
        )
    except Exception as e:
        logger.warning(f"[Orchestrator][Reasoning] failed: {e}")
        intent = "general"
        intent_confidence = 0.0
        entities = []
        aliases = []
        entities_confidence = 0.0
        key_aspects = []
        user_needs = []
        rewritten = query
        search_context = {}
        intent_analysis_score = 0.0

    return {
        "intent": intent,
        "intent_confidence": intent_confidence,
        "product_entities": entities,
        "aliases": aliases,
        "entities_confidence": entities_confidence,
        "key_aspects": key_aspects,
        "user_needs": user_needs,
        "user_query_rewritten": rewritten,
        "search_context": search_context,
        "intent_analysis_score": intent_analysis_score,
        "missing_dimensions": [],
        "_intent_round": round_num,
        "_intent_done": False,  # 默认不结束，等待 Observation 判断
    }


async def node_action(state: GraphState) -> dict[str, Any]:
    """ReAct Action 节点：深度意图分析补充

    功能：
      - 基于初步分析，从不同角度重新审视查询
      - 识别潜在的隐含需求
      - 补充缺失的分析维度
      - 优化意图分类的颗粒度
      - 完善关键方面和用户需求提取
    """
    query = state.get("user_query_raw", "")
    intent = state.get("intent", "general")
    entities = state.get("product_entities", [])
    key_aspects = state.get("key_aspects", [])
    user_needs = state.get("user_needs", [])
    round_num = state.get("_intent_round", 0)

    # 将 key_aspects 转换为字符串用于 prompt
    aspects_str = "、".join([a.get("aspect", "") for a in key_aspects]) if key_aspects else "无"
    needs_str = "、".join(user_needs) if user_needs else "无"

    prompt = INTENT_ACTION_PROMPT.format(
        query=query,
        round=round_num,
        intent=intent,
        entities="、".join(entities) if entities else "无",
        aspects=aspects_str,
        needs=needs_str,
    )

    try:
        resp = await _llm_action.ainvoke(prompt)
        data = _parse_reasoning_json(resp.content)

        # 更新意图分析结果
        updated_intent = data.get("intent", intent)
        updated_intent_confidence = float(data.get("intent_confidence", 0.0))
        updated_entities = data.get("product_entities", entities)
        updated_aliases = data.get("aliases", [])
        updated_entities_confidence = float(data.get("entities_confidence", 0.0))
        updated_key_aspects = data.get("key_aspects", [])
        updated_user_needs = data.get("user_needs", [])
        improvement_summary = data.get("improvement_summary", "")

        # 重新计算质量分数
        updated_score = (
            (updated_intent_confidence * 0.4) +
            (updated_entities_confidence * 0.3) +
            (min(len(updated_user_needs), 3) / 3.0 * 0.3)
        )

        logger.info(
            f"[Orchestrator][Action] Round {round_num}: "
            f"improved_intent={updated_intent}, "
            f"new_aspects={len(updated_key_aspects)}, "
            f"new_needs={len(updated_user_needs)}, "
            f"new_score={updated_score:.2f}, "
            f"summary={improvement_summary}"
        )

        return {
            "intent": updated_intent,
            "intent_confidence": updated_intent_confidence,
            "product_entities": updated_entities,
            "aliases": updated_aliases,
            "entities_confidence": updated_entities_confidence,
            "key_aspects": updated_key_aspects,
            "user_needs": updated_user_needs,
            "intent_analysis_score": updated_score,
        }
    except Exception as e:
        logger.warning(f"[Orchestrator][Action] failed: {e}")
        # 如果失败，保持原有分析结果，必须返回至少一个状态值防止 InvalidUpdateError
        return {"_intent_round": round_num}


async def node_observation(state: GraphState) -> dict[str, Any]:
    """ReAct Observation 节点：评估意图识别质量

    功能：
      - 评估意图分类的置信度和准确性
      - 评估实体识别的完整性
      - 评估用户需求提取的深度
      - 决定是否继续推理

    注意：
      - 此节点只评估意图识别质量，不涉及检索评估
      - 检索相关的评估将由Retrieve Agent负责
    """
    intent = state.get("intent", "general")
    intent_confidence = state.get("intent_confidence", 0.0)
    entities = state.get("product_entities", [])
    entities_confidence = state.get("entities_confidence", 0.0)
    key_aspects = state.get("key_aspects", [])
    user_needs = state.get("user_needs", [])
    round_num = state.get("_intent_round", 0)
    current_score = state.get("intent_analysis_score", 0.0)

    # 将 key_aspects 转换为字符串用于 prompt
    aspects_str = "、".join([a.get("aspect", "") for a in key_aspects]) if key_aspects else "无"
    needs_str = "、".join(user_needs) if user_needs else "无"

    prompt = INTENT_OBSERVATION_PROMPT.format(
        intent=intent,
        intent_confidence=intent_confidence,
        entities="、".join(entities) if entities else "无",
        entities_confidence=entities_confidence,
        aspects=aspects_str,
        needs=needs_str,
    )

    try:
        resp = await _llm_observation.ainvoke(prompt)
        data = _parse_reasoning_json(resp.content)

        quality_dimensions = data.get("quality_dimensions", {})
        updated_score = float(data.get("intent_analysis_score", current_score))
        missing_dimensions = data.get("missing_dimensions", [])
        should_continue = data.get("should_continue", False)
        continue_reason = data.get("continue_reason", "")

        # 结合 LLM 评估和规则判断
        # 综合质量分数 >= 0.8 或者已经达到最大轮次，则停止
        should_stop = (
            updated_score >= 0.8 or
            round_num >= _MAX_INTENT_ROUNDS or
            not should_continue
        )

        logger.info(
            f"[Orchestrator][Observation] Round {round_num}: "
            f"score={updated_score:.2f}, missing={missing_dimensions}, "
            f"should_continue={should_continue}, stop={should_stop}, "
            f"reason={continue_reason}"
        )

        return {
            "intent_analysis_score": updated_score,
            "missing_dimensions": missing_dimensions,
            "_intent_done": should_stop,
        }
    except Exception as e:
        logger.warning(f"[Orchestrator][Observation] failed: {e}")
        # 如果评估失败，使用规则判断
        should_stop = (
            current_score >= 0.8 or
            round_num >= _MAX_INTENT_ROUNDS
        )
        logger.info(
            f"[Orchestrator][Observation] Round {round_num} (fallback): "
            f"score={current_score:.2f}, stop={should_stop}"
        )
        return {
            "_intent_done": should_stop,
        }


def _route_observation(state: GraphState) -> Literal["reasoning", "__end__"]:
    """条件边：根据观察结果决定是否继续循环"""
    if state.get("_intent_done"):
        return "__end__"
    return "reasoning"


def build_orchestrator_graph():
    """构建意图识别 ReAct 子图

    完整 ReAct 循环：
      reasoning -> action -> observation
    """
    g = StateGraph(GraphState)

    # 添加所有节点
    g.add_node("reasoning", node_reasoning)
    g.add_node("action", node_action)
    g.add_node("observation", node_observation)

    # 设置入口点
    g.set_entry_point("reasoning")

    # 设置边连接
    g.add_edge("reasoning", "action")
    g.add_edge("action", "observation")

    # 添加条件边：根据 Observation 结果决定是否循环
    g.add_conditional_edges("observation", _route_observation)

    return g.compile()
