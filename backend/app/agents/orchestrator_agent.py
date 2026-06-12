"""Orchestrator Subgraph — 意图识别 ReAct Agent
职责：通过 ReAct 循环深入分析用户意图，生成高质量的意图分析结果

核心流程：
  1. Reasoning: 分析用户查询，识别意图、实体、关注方面和用户需求（Function Calling 结构化输出）
  2. Action: 三阶段管道 — 热搜工具调用 → LLM更新决策 → 代码增量合并 + 补全分析
  3. Observation: 规则判断意图分析质量，决定是否继续推理

循环终止条件:
  - 意图分析质量分数达到阈值（>= 0.7）
  - 达到最大推理轮次（2轮）
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from langgraph.graph import StateGraph
from loguru import logger

from app.models.schemas import GraphState
from app.prompts.templates import ORCHESTRATOR_FC_PROMPT, REASONING_FC_PROMPT
from app.tools.llm import create_llm
from app.tools.hot_topics import hot_topics_client
from app.tools.current_time import current_time_client
from app.tools.tool_schemas import INTENT_TOOLS, ORCHESTRATOR_TOOLS
from app.utils.temporal import infer_temporal_context, normalize_temporal_context

_llm_reasoning = None
_llm_action = None

_MAX_INTENT_ROUNDS = 4  # 最多 2 轮 ReAct 循环
_MAX_FC_ITERATIONS = 4  # Action 节点 Function Calling 最大迭代次数

# 边界控制常量
_MAX_ENTITIES_FROM_HOT_TOPICS = 3
_MAX_ASPECTS_FROM_HOT_TOPICS = 2
_CONFIDENCE_ADJ_RANGE = (-0.2, 0.3)

_TEMPORAL_QUERY_MARKERS = (
    "今天",
    "今日",
    "昨天",
    "最近",
    "近期",
    "近来",
    "近半年",
    "半年内",
    "近一年",
    "一年内",
    "发布初期",
    "上市初期",
    "以前",
    "过去",
    "历史",
    "现在还",
    "变化",
    "变了",
)

_EVENT_TOPIC_MARKERS = (
    "助农",
    "卖菜",
    "惠农",
    "扶贫",
    "公益",
    "专线",
    "活动",
    "事件",
    "风波",
    "争议",
    "回应",
    "发声",
    "直播",
    "发布会",
    "发布",
    "降价",
    "涨价",
    "翻车",
    "测评",
    "评测",
    "融资",
    "上市",
    "合作",
    "政治立场",
    "央视",
)

_TOPIC_PREFIX_QUALIFIERS = (
    "贵州",
    "农村",
    "公益",
    "扶贫",
    "惠农",
    "助农",
)


def _create_state_llm(state: GraphState, fallback: Any = None, **kwargs: Any):
    if state.get("_llm_config"):
        return create_llm(llm_config=state.get("_llm_config"), **kwargs)
    if fallback is not None:
        return fallback
    return create_llm(**kwargs)


def _dedupe_keep_order(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        value = str(value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _compact_text(value: str) -> str:
    return str(value or "").replace(" ", "").lower()


def _derive_canonical_entity(entity: str) -> tuple[str, bool]:
    """Split obvious topic/search phrases into a stable main entity plus a topic hint."""
    text = str(entity or "").strip()
    if not text:
        return "", False

    marker_positions = [
        text.find(marker)
        for marker in _EVENT_TOPIC_MARKERS
        if marker in text and text.find(marker) > 0
    ]
    if not marker_positions:
        return text, False

    split_at = min(marker_positions)
    canonical = text[:split_at].strip(" -_/·的关于")
    for qualifier in _TOPIC_PREFIX_QUALIFIERS:
        qualifier_at = canonical.find(qualifier)
        if qualifier_at > 1:
            prefix = canonical[:qualifier_at].strip(" -_/·的关于")
            if len(prefix) >= 2:
                canonical = prefix
                break
    if len(canonical) < 2:
        return text, False
    return canonical, canonical != text


def _canonicalize_entities(
    entities: list[str] | None,
    aliases: list[str] | None = None,
    existing_entities: list[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """
    Keep product_entities as stable memory entities.

    Obvious event/search phrases are returned as search hints instead of entities.
    """
    raw_entities = _dedupe_keep_order(entities or [])
    existing = _dedupe_keep_order(existing_entities or [])
    hints = []

    derived_rows = []
    derived_candidates = []
    for raw in raw_entities:
        canonical, changed = _derive_canonical_entity(raw)
        if changed:
            hints.append(raw)
        derived_rows.append((raw, canonical, changed))
        if canonical:
            derived_candidates.append(canonical)

    prefix_candidates = _dedupe_keep_order(existing + derived_candidates)
    canonical_entities = []
    for raw, canonical, changed in derived_rows:
        final = canonical
        if changed:
            compact_final = _compact_text(final)
            for candidate in prefix_candidates:
                compact_candidate = _compact_text(candidate)
                if (
                    candidate
                    and candidate != final
                    and len(compact_candidate) >= 2
                    and compact_final.startswith(compact_candidate)
                ):
                    final = candidate
                    break
        canonical_entities.append(final)

    if not canonical_entities:
        canonical_entities = existing

    alias_values = []
    for alias in aliases or []:
        alias = str(alias or "").strip()
        if not alias:
            continue
        canonical_alias, changed = _derive_canonical_entity(alias)
        if changed:
            hints.append(alias)
            continue
        if alias not in canonical_entities:
            alias_values.append(canonical_alias)

    return (
        _dedupe_keep_order(canonical_entities),
        _dedupe_keep_order(alias_values),
        _dedupe_keep_order(hints),
    )


def _parse_reasoning_json(text: str) -> dict[str, Any]:
    """清理并解析 LLM 返回的 JSON 推理结果。"""
    import re
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    text = re.sub(r'```\s*$', '', text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r'\{[^}]+\}', text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return {}


def _query_has_temporal_marker(query: str) -> bool:
    text = str(query or "")
    return any(marker in text for marker in _TEMPORAL_QUERY_MARKERS) or bool(re.search(r"20\d{2}\s*年", text))


async def node_reasoning(state: GraphState) -> dict[str, Any]:
    """ReAct Reasoning 节点：深度意图分析（使用 Function Calling）

    功能：
      - 识别意图类型（产品比较、质量问题、性价比、用户体验、热点事件等）
      - 提取产品实体和别名
      - 识别用户关注的核心方面（价格、质量、功能等）
      - 提取用户需求和痛点
      - 生成初步的改写查询
      - 构建搜索上下文，指导Retrieve Agent
    """
    query = state.get("user_query_raw", "")
    round_num = state.get("_intent_round", 0) + 1

    prompt = REASONING_FC_PROMPT.format(query=query)
    session_intent_frame = state.get("_session_intent_frame", {})
    session_last_run_ref = state.get("_session_last_run_ref", {})
    if session_intent_frame:
        prompt += (
            "\n\n上一轮会话意图分析结果（仅用于解析省略/指代，不要机械照搬）：\n"
            f"{json.dumps(session_intent_frame, ensure_ascii=False)}\n"
            f"上一轮长期记忆 run 引用：{json.dumps(session_last_run_ref, ensure_ascii=False)}\n"
            "如果当前查询出现“那、刚才、继续、再看看、这个产品”等省略表达，"
            "请优先继承上一轮的实体，并结合当前查询生成新的关注方面和改写查询。"
        )

    messages: list[dict] = [{"role": "user", "content": prompt}]
    llm_reasoning = _create_state_llm(state, fallback=_llm_reasoning, temperature=0)

    try:
        resp = await llm_reasoning.ainvoke(messages, tools=INTENT_TOOLS)

        if resp.tool_calls and resp.tool_calls[0].name == "analyze_intent":
            data = resp.tool_calls[0].arguments

            # 空结果检测：如果关键字段全为默认值，说明 FC 解析失败
            if not data or (
                not data.get("product_entities")
                and not data.get("user_needs")
                and data.get("intent") in (None, "general")
            ):
                logger.warning("[Orchestrator][Reasoning] FC 返回空结果，降级处理")
                raise Exception("FC 返回空结果")

            intent = data.get("intent", "general")
            intent_confidence = float(data.get("intent_confidence", 0.5))
            entities, aliases, entity_search_hints = _canonicalize_entities(
                data.get("product_entities", []),
                data.get("aliases", []),
            )
            user_needs = data.get("user_needs", [])
            rewritten = data.get("rewritten_query", query)
            search_hints = _dedupe_keep_order((data.get("search_hints") or []) + entity_search_hints)

            # 从扁平字段重建嵌套结构
            key_aspects_raw = data.get("key_aspects") or []
            key_aspects = [
                {"aspect": a, "priority": "high", "user_sentiment": "neutral"}
                for a in key_aspects_raw
            ] if key_aspects_raw else []

            primary_candidates, _, primary_hints = _canonicalize_entities(
                [data.get("primary_entity", "")] if data.get("primary_entity") else [],
                [],
                existing_entities=entities,
            )
            primary_entity = primary_candidates[0] if primary_candidates else (entities[0] if entities else "")
            search_hints = _dedupe_keep_order(search_hints + primary_hints)
            temporal_context = normalize_temporal_context(
                data.get("temporal_context"),
                query=query,
            )
            if primary_entity:
                search_context = {
                    "primary_entity": primary_entity,
                    "focus_aspects": key_aspects_raw,
                    "search_hints": search_hints,
                    "time_relevance": data.get("time_relevance", "evergreen"),
                }
            else:
                search_context = {}

            entities_confidence = min(1.0, len(entities) * 0.4 + 0.2) if entities else 0.0

            intent_analysis_score = (
                (intent_confidence * 0.4) +
                (entities_confidence * 0.3) +
                (min(len(user_needs), 3) / 3.0 * 0.3)
            )

            logger.info(
                f"[Orchestrator][Reasoning] Round {round_num} (FC): "
                f"intent={intent}, confidence={intent_confidence:.2f}, "
                f"entities={entities}, score={intent_analysis_score:.2f}"
            )

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
                "temporal_context": temporal_context,
                "intent_analysis_score": intent_analysis_score,
                "missing_dimensions": [],
                "_intent_round": round_num,
                "_intent_done": False,
            }

        else:
            logger.warning("[Orchestrator][Reasoning] LLM 未调用 analyze_intent 工具，使用降级策略")
            raise Exception("LLM 未调用工具")

    except Exception as e:
        logger.warning(f"[Orchestrator][Reasoning] failed: {e}, using fallback strategy")
        fallback_entities, _, fallback_hints = _canonicalize_entities([query], [])

        return {
            "intent": "general",
            "intent_confidence": 0.0,
            "product_entities": fallback_entities or [query],
            "aliases": [],
            "entities_confidence": 0.0,
            "key_aspects": [],
            "user_needs": [],
            "user_query_rewritten": query,
            "search_context": {
                "primary_entity": fallback_entities[0],
                "focus_aspects": [],
                "search_hints": fallback_hints,
                "time_relevance": "evergreen",
            } if fallback_entities else {},
            "temporal_context": infer_temporal_context(query),
            "intent_analysis_score": 0.0,
            "missing_dimensions": [],
            "_intent_round": round_num,
            "_intent_done": False,
        }


# ---------------------------------------------------------------------------
# Action 节点：三阶段管道 + 补全分析
# ---------------------------------------------------------------------------

async def _execute_hot_topics_tool(name: str, arguments: dict) -> dict[str, Any]:
    """执行意图 Agent 外部工具调用。"""
    if name == "search_hot_topics":
        keyword = arguments.get("keyword", "")
        platform = arguments.get("platform", "all")
        return await hot_topics_client.search_topics(keyword, platform)
    elif name == "get_trending_list":
        platform = arguments.get("platform", "weibo")
        limit = int(arguments.get("limit", 20))
        topics = await hot_topics_client.fetch_trending(platform, limit)
        return {
            "platform": platform,
            "update_time": "",
            "topics": topics,
        }
    elif name == "search_baidu_related":
        keyword = arguments.get("keyword", "")
        return await hot_topics_client.search_baidu_related(keyword)
    elif name == "search_baidu_baike":
        keyword = arguments.get("keyword", "")
        return await hot_topics_client.search_baidu_baike(keyword)
    elif name == "get_current_time":
        return await current_time_client.get_current_time()
    else:
        return {"error": f"Unknown tool: {name}"}


async def _supplement_analysis(
    query: str,
    state: GraphState,
    merged_state: dict[str, Any],
    missing_fields: list[str],
) -> dict[str, Any]:
    """补全分析：用 analyze_intent 工具做一次完整意图分析，只取缺失字段。"""
    prompt = f"""对以下查询进行完整的意图分析：{query}

当前已有分析结果：
- 意图: {merged_state.get('intent', 'general')}
- 实体: {merged_state.get('product_entities', [])}
- 关注方面: {[a.get('aspect', '') for a in merged_state.get('key_aspects', [])]}
- 缺失字段: {missing_fields}
- 时间上下文: {merged_state.get('temporal_context', {})}

请调用 analyze_intent 工具，重点确保 {missing_fields} 字段有值。"""

    prompt += """

字段边界要求：
- product_entities/primary_entity 只表示稳定主实体，用于长期记忆归档。
- 人物 + 事件/活动类查询，应把人物作为主实体，把事件短语放到 key_aspects 或 search_hints。
- 例如“吴克群助农卖菜”应输出 product_entities=["吴克群"]，不要输出 ["吴克群助农"]。"""

    try:
        llm_action = _create_state_llm(state, fallback=_llm_action, temperature=0)
        resp = await llm_action.ainvoke(
            [{"role": "user", "content": prompt}],
            tools=INTENT_TOOLS,
        )

        if resp.tool_calls and resp.tool_calls[0].name == "analyze_intent":
            data = resp.tool_calls[0].arguments
            if not data or (
                not data.get("product_entities")
                and not data.get("user_needs")
                and data.get("intent") in (None, "general")
            ):
                logger.warning("[Orchestrator][Action] 补全分析 FC 返回空结果")
                return {}

            updates: dict[str, Any] = {}
            current_entities = merged_state.get("product_entities", [])
            entities, aliases, entity_search_hints = _canonicalize_entities(
                data.get("product_entities", []),
                data.get("aliases", []),
                existing_entities=current_entities,
            )
            if not current_entities and entities:
                updates["product_entities"] = entities

            # 只填充缺失的字段，不覆盖已有值
            if "user_needs" in missing_fields and data.get("user_needs"):
                updates["user_needs"] = data["user_needs"]
            if "aliases" in missing_fields and aliases:
                updates["aliases"] = aliases
            if "search_context" in missing_fields:
                # 从扁平字段重建 search_context
                primary_candidates, _, primary_hints = _canonicalize_entities(
                    [data.get("primary_entity", "")] if data.get("primary_entity") else [],
                    [],
                    existing_entities=entities or current_entities,
                )
                primary_entity = (
                    primary_candidates[0]
                    if primary_candidates
                    else ((entities or current_entities or [""])[0])
                )
                if primary_entity:
                    key_aspects_raw = data.get("key_aspects") or []
                    search_hints = _dedupe_keep_order(
                        (data.get("search_hints") or []) + entity_search_hints + primary_hints
                    )
                    updates["search_context"] = {
                        "primary_entity": primary_entity,
                        "focus_aspects": key_aspects_raw,
                        "search_hints": search_hints,
                        "time_relevance": data.get("time_relevance", "evergreen"),
                    }
            if "temporal_context" in missing_fields or data.get("temporal_context"):
                updates["temporal_context"] = normalize_temporal_context(
                    data.get("temporal_context"),
                    query=query,
                    current_time=merged_state.get("current_time", {}),
                )
            if "intent_confidence" in missing_fields and data.get("intent_confidence"):
                updates["intent_confidence"] = float(data["intent_confidence"])
            # 如果 intent 仍是 general，尝试更新
            if merged_state.get("intent") == "general" and data.get("intent") and data["intent"] != "general":
                updates["intent"] = data["intent"]
            # 如果 rewritten_query 仍是原始查询，尝试更新
            if merged_state.get("user_query_rewritten") == query and data.get("rewritten_query"):
                updates["user_query_rewritten"] = data["rewritten_query"]

            logger.info(f"[Orchestrator][Action] 补全分析完成: 填充了 {list(updates.keys())}")
            return updates

        logger.warning("[Orchestrator][Action] 补全分析 LLM 未调用 analyze_intent")
        return {}
    except Exception as e:
        logger.warning(f"[Orchestrator][Action] 补全分析失败: {e}")
        return {}


async def node_action(state: GraphState) -> dict[str, Any]:
    """ReAct Action 节点：三阶段管道 + 补全分析

    阶段1：工具调用 — LLM自主决定是否调用热搜，获取当下舆论背景
    阶段2：更新决策 — LLM基于热搜结果，输出"改什么、为什么改"的结构化决策
    阶段3：代码合并 — 代码层面将更新决策增量合并到state，不覆盖只补充
    阶段4：补全分析 — 如果关键字段仍缺失，用 analyze_intent 补全
    """
    query = state.get("user_query_raw", "")
    intent = state.get("intent", "general")
    entities = state.get("product_entities", [])
    key_aspects = state.get("key_aspects", [])
    user_needs = state.get("user_needs", [])
    round_num = state.get("_intent_round", 0)

    aspects_str = "、".join([a.get("aspect", "") for a in key_aspects]) if key_aspects else "无"
    needs_str = "、".join(user_needs) if user_needs else "无"

    # ── 阶段1：Function Calling 多轮循环（热搜工具调用）──
    system_prompt = ORCHESTRATOR_FC_PROMPT.format(
        query=query,
        entities="、".join(entities) if entities else "无",
        aspects=aspects_str,
        needs=needs_str,
    )

    messages: list[dict] = [{"role": "user", "content": system_prompt}]
    llm_action = _create_state_llm(state, fallback=_llm_action, temperature=0)

    hot_topics_called = False
    hot_topics_results: list[dict] = []
    metrics: dict[str, Any] = {"called": False}
    if _query_has_temporal_marker(query):
        current_time = await current_time_client.get_current_time()
        metrics["current_time"] = current_time
        messages.append({
            "role": "user",
            "content": (
                "当前时间工具结果如下，请在后续 update_intent_analysis 中据此校正 temporal_context："
                f"{json.dumps(current_time, ensure_ascii=False)}"
            ),
        })

    for iteration in range(_MAX_FC_ITERATIONS):
        try:
            resp = await llm_action.ainvoke(messages, tools=ORCHESTRATOR_TOOLS)
        except Exception as e:
            logger.warning(f"[Orchestrator][Action] FC iteration={iteration} failed: {e}")
            break

        if resp.tool_calls:
            # LLM 调用了 update_intent_analysis → 进入阶段2
            for tc in resp.tool_calls:
                if tc.name == "update_intent_analysis":
                    decision = tc.arguments
                    updates = _merge_decision(state, decision, metrics)
                    # 继续到阶段4
                    return await _finalize_action(state, query, updates, metrics)

            # 执行热搜工具调用
            messages.append({
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in resp.tool_calls
                ],
            })

            tool_results = []
            for tc in resp.tool_calls:
                result = await _execute_hot_topics_tool(tc.name, tc.arguments)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

                hot_topics_called = True
                if tc.name == "get_current_time" and result.get("now_iso"):
                    metrics["current_time"] = result
                if result.get("matched_topics"):
                    hot_topics_results.extend(result["matched_topics"])
                elif result.get("topics"):
                    hot_topics_results.extend(result["topics"])

                logger.info(
                    f"[Orchestrator][Action] 工具调用: {tc.name}({tc.arguments}), "
                    f"结果数量: {len(result.get('matched_topics', result.get('topics', [])))}"
                )

            messages.extend(tool_results)
        else:
            logger.info(f"[Orchestrator][Action] LLM 决定不调用热搜工具，iteration={iteration}")
            break

    # ── 阶段2：如果调用了热搜但没有主动输出 update_intent_analysis，提示 LLM 输出决策 ──
    updates: dict[str, Any] = {"_hot_topics_metrics": metrics}

    if hot_topics_called and hot_topics_results:
        metrics["called"] = True
        metrics["source_topics"] = [t.get("title", "") for t in hot_topics_results[:5]]

        prompt_decision = (
            f"热搜查询已完成，共找到 {len(hot_topics_results)} 条相关热搜。"
            "请根据这些热搜信息，调用 update_intent_analysis 工具决定是否更新意图分析。"
        )
        messages.append({"role": "user", "content": prompt_decision})

        try:
            resp = await llm_action.ainvoke(messages, tools=ORCHESTRATOR_TOOLS)
            if resp.tool_calls:
                for tc in resp.tool_calls:
                    if tc.name == "update_intent_analysis":
                        decision = tc.arguments
                        updates = _merge_decision(state, decision, metrics)
                        return await _finalize_action(state, query, updates, metrics)
        except Exception as e:
            logger.warning(f"[Orchestrator][Action] 阶段2 LLM调用失败: {e}")

    # 没有调用热搜或决策失败
    metrics["updated"] = False
    return await _finalize_action(state, query, updates, metrics)


async def _finalize_action(
    state: GraphState,
    query: str,
    updates: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """阶段4：补全分析 — 如果热搜合并后关键字段仍缺失，用 analyze_intent 补全。"""

    # 确保 metrics 字段存在
    if "_hot_topics_metrics" not in updates:
        updates["_hot_topics_metrics"] = metrics
    if metrics.get("current_time"):
        updates["current_time"] = metrics["current_time"]
        base_temporal = updates.get("temporal_context") or state.get("temporal_context", {})
        updates["temporal_context"] = normalize_temporal_context(
            base_temporal,
            query=query,
            current_time=metrics["current_time"],
        )

    # 预览合并后的状态
    merged_state = {**state, **updates}

    missing_fields = []
    if not merged_state.get("user_needs"):
        missing_fields.append("user_needs")
    if not merged_state.get("aliases"):
        missing_fields.append("aliases")
    if not merged_state.get("search_context") or not merged_state.get("search_context", {}).get("primary_entity"):
        missing_fields.append("search_context")
    if not merged_state.get("temporal_context"):
        missing_fields.append("temporal_context")
    if merged_state.get("intent_confidence", 0) < 0.3:
        missing_fields.append("intent_confidence")

    if missing_fields:
        logger.info(f"[Orchestrator][Action] 补全分析启动，缺失字段: {missing_fields}")
        supplement = await _supplement_analysis(query, state, merged_state, missing_fields)
        updates.update(supplement)

    return updates


def _merge_decision(state: GraphState, decision: dict, metrics: dict[str, Any]) -> dict[str, Any]:
    """阶段3：代码增量合并 — 不覆盖只补充，去重后追加，边界控制截断。"""
    metrics["called"] = metrics.get("called", True)
    metrics["updated"] = True

    updates: dict[str, Any] = {"_hot_topics_metrics": metrics}

    current_intent = state.get("intent", "general")
    current_confidence = state.get("intent_confidence", 0.0)
    current_entities = state.get("product_entities", [])
    current_aspects = state.get("key_aspects", [])
    current_search_context = state.get("search_context", {})
    current_temporal_context = state.get("temporal_context", {})
    current_time = metrics.get("current_time") or state.get("current_time", {})

    # 修正意图
    if decision.get("should_update_intent") and decision.get("new_intent"):
        new_intent = decision["new_intent"]
        if new_intent != current_intent:
            updates["intent"] = new_intent
            metrics["intent_changed"] = True
            metrics["intent_update_reason"] = decision.get("intent_update_reason", "")
    else:
        metrics["intent_changed"] = False

    # 置信度调整（受边界控制）
    adj = decision.get("confidence_adjustment", 0.0)
    adj = max(_CONFIDENCE_ADJ_RANGE[0], min(_CONFIDENCE_ADJ_RANGE[1], adj))
    new_confidence = min(1.0, max(0.0, current_confidence + adj))
    updates["intent_confidence"] = new_confidence

    # 补充实体（只补充稳定主实体；热搜标题/事件短语转为搜索提示）
    new_entities = decision.get("new_entities_to_add") or []
    canonical_new_entities, _, entity_search_hints = _canonicalize_entities(
        new_entities,
        [],
        existing_entities=current_entities,
    )
    entities_to_add = [
        e for e in canonical_new_entities[:_MAX_ENTITIES_FROM_HOT_TOPICS]
        if e and e not in current_entities
    ]
    if entities_to_add:
        updates["product_entities"] = current_entities + entities_to_add
    metrics["entities_added"] = len(entities_to_add)

    # 补充关注方面（去重 + 边界控制）
    new_aspects = decision.get("new_aspects_to_add", [])
    existing_aspect_names = {a.get("aspect", "") for a in current_aspects}
    aspects_to_add = []
    for a in new_aspects[:_MAX_ASPECTS_FROM_HOT_TOPICS]:
        if a.get("aspect") and a["aspect"] not in existing_aspect_names:
            aspects_to_add.append(a)
    if aspects_to_add:
        updates["key_aspects"] = current_aspects + aspects_to_add
    metrics["aspects_added"] = len(aspects_to_add)

    # 时效性更新
    time_relevance = decision.get("time_relevance", "no_change")
    if time_relevance != "no_change":
        if current_search_context:
            updated_context = {**current_search_context, "time_relevance": time_relevance}
        else:
            updated_context = {"time_relevance": time_relevance}
        updates["search_context"] = updated_context
        metrics["time_relevance_changed"] = True
    else:
        metrics["time_relevance_changed"] = False

    if decision.get("temporal_context"):
        updates["temporal_context"] = normalize_temporal_context(
            decision.get("temporal_context"),
            query=state.get("user_query_raw", ""),
            current_time=current_time,
        )
        metrics["temporal_context_changed"] = updates["temporal_context"] != current_temporal_context
    elif current_temporal_context:
        updates["temporal_context"] = normalize_temporal_context(
            current_temporal_context,
            query=state.get("user_query_raw", ""),
            current_time=current_time,
        )
        metrics["temporal_context_changed"] = False
    else:
        updates["temporal_context"] = infer_temporal_context(state.get("user_query_raw", ""), current_time=current_time)
        metrics["temporal_context_changed"] = _query_has_temporal_marker(state.get("user_query_raw", ""))

    if current_time:
        updates["current_time"] = current_time

    # 补充检索建议
    search_hints = (decision.get("search_hints_to_add") or []) + entity_search_hints
    if search_hints:
        base_context = updates.get("search_context", current_search_context or {})
        existing_hints = base_context.get("search_hints", [])
        merged_hints = existing_hints + [h for h in search_hints if h not in existing_hints]
        if "search_context" not in updates:
            updates["search_context"] = {**base_context} if base_context else {}
        updates["search_context"]["search_hints"] = merged_hints

    # 记录来源热搜
    metrics["source_topics"] = metrics.get("source_topics", [])
    for a in new_aspects:
        source = a.get("source_hot_topic", "")
        if source and source not in metrics["source_topics"]:
            metrics["source_topics"].append(source)

    # 重新计算质量分数
    new_score = (
        (new_confidence * 0.4) +
        (state.get("entities_confidence", 0.0) * 0.3) +
        (min(len(state.get("user_needs", [])), 3) / 3.0 * 0.3)
    )
    updates["intent_analysis_score"] = new_score

    logger.info(
        f"[Orchestrator][Action] 合并完成: intent_changed={metrics.get('intent_changed', False)}, "
        f"entities_added={metrics.get('entities_added', 0)}, "
        f"aspects_added={metrics.get('aspects_added', 0)}, "
        f"temporal={updates.get('temporal_context', {}).get('mode', '')}, "
        f"confidence={new_confidence:.2f}, score={new_score:.2f}"
    )

    return updates


# ---------------------------------------------------------------------------
# Observation 节点：规则判断（不再使用LLM自评）
# ---------------------------------------------------------------------------

async def node_observation(state: GraphState) -> dict[str, Any]:
    """ReAct Observation 节点：规则判断意图识别质量

    评估维度（总分 1.0）：
      - 是否识别到实体（0.20）
      - 是否有关注方面（0.15）
      - 意图是否明确非general（0.10）
      - 置信度是否达标（0.15）
      - 是否有用户需求（0.20）
      - 搜索上下文是否完整（0.20）
    """
    entities = state.get("product_entities", [])
    key_aspects = state.get("key_aspects", [])
    intent = state.get("intent", "general")
    intent_confidence = state.get("intent_confidence", 0.0)
    user_needs = state.get("user_needs", [])
    search_context = state.get("search_context", {})
    round_num = state.get("_intent_round", 0)

    score = 0.0
    if entities:
        score += 0.20
    if key_aspects:
        score += 0.15
    if intent != "general":
        score += 0.10
    if intent_confidence >= 0.5:
        score += 0.15
    if user_needs:
        score += 0.20
    if search_context and search_context.get("primary_entity"):
        score += 0.20

    should_stop = score >= 0.7 or round_num >= _MAX_INTENT_ROUNDS

    logger.info(
        f"[Orchestrator][Observation] Round {round_num}: "
        f"score={score:.2f}, entities={bool(entities)}, aspects={bool(key_aspects)}, "
        f"needs={bool(user_needs)}, search_ctx={bool(search_context and search_context.get('primary_entity'))}, "
        f"stop={should_stop}"
    )

    return {
        "intent_analysis_score": score,
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

    g.add_node("reasoning", node_reasoning)
    g.add_node("action", node_action)
    g.add_node("observation", node_observation)

    g.set_entry_point("reasoning")

    g.add_edge("reasoning", "action")
    g.add_edge("action", "observation")

    g.add_conditional_edges("observation", _route_observation)

    return g.compile()
