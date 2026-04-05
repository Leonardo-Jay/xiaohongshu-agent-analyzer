"""Retrieve Subgraph — 检索 ReAct Agent
职责：接收 Orchestrator 的意图分析结果，生成小红书检索关键词，调用 MCP 工具爬取帖子，
     观察帖子数量是否足够，不够则结合意图结果生成不重复的新关键词继续搜索。

核心流程：
  1. Plan Keywords: 基于 orchestrator 的 search_context 生成检索关键词
  2. Fetch Posts: 调用 MCP 工具搜索帖子并拉取详情
  3. Check Coverage: 检查帖子数量是否足够（>= 10 篇），决定是否需要继续搜索

循环终止条件:
  - 去重后帖子总数 >= 10 篇
  - 达到最大检索轮次（3 轮）
"""
from __future__ import annotations

import json
from typing import Any, Literal

from loguru import logger

from app.models.schemas import GraphState
from app.prompts.templates import REACT_ACTION_PROMPT, RETRIEVE_EXPAND_PROMPT
from app.tools.llm import create_llm
from app.tools.mcp_client import XhsMcpClient

_llm_first = create_llm(temperature=0)
_llm_expand = create_llm(temperature=0)

_MAX_RETRIEVE_ROUNDS = 3  # 最多 3 轮 ReAct 循环
_MIN_POSTS = 20  # 目标最少帖子数
_MAX_POSTS_PER_ROUND = 20  # 单轮最多新帖子


def _parse_keywords_json(text: str) -> list[str]:
    """清理并解析 LLM 返回的关键词 JSON 结果。"""
    import re
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    text = re.sub(r'```\s*$', '', text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
        # 兼容多种返回格式
        if isinstance(data, dict):
            return data.get("query_plan", data.get("new_keywords", data.get("new_queries", [])))
        elif isinstance(data, list):
            return data
        return []
    except Exception:
        # 尝试提取 JSON 数组
        m = re.search(r'\[[^\]]+\]', text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return []


async def node_plan_keywords(state: GraphState, config: dict[str, Any]) -> dict[str, Any]:
    """ReAct Plan Keywords 节点：生成检索关键词

    功能：
      - 第 1 轮：使用 REACT_ACTION_PROMPT 基于 orchestrator 的意图分析生成 3~5 个关键词
      - 第 2+ 轮：使用 RETRIEVE_EXPAND_PROMPT，结合 search_context 生成不重复的新关键词
    """
    query = state.get("user_query_raw", "")
    intent = state.get("intent", "general")
    entities = state.get("product_entities", [])
    aliases = state.get("aliases", [])
    search_context = state.get("search_context", {})
    used_keywords = state.get("_used_keywords", [])
    retrieved_posts = state.get("retrieved_posts", [])
    round_num = state.get("_retrieve_round", 0) + 1

    # 第 1 轮使用 REACT_ACTION_PROMPT，后续轮次使用 RETRIEVE_EXPAND_PROMPT
    if round_num == 1:
        entities_str = ",".join(entities) if entities else query
        aliases_str = ",".join(aliases) if aliases else "无"
        prompt = REACT_ACTION_PROMPT.format(
            query=query,
            intent=intent,
            entities=entities_str,
            aliases=aliases_str,
        )
        llm = _llm_first
    else:
        # 第 2+ 轮：使用扩展 prompt
        prompt = RETRIEVE_EXPAND_PROMPT.format(
            query=query,
            intent=intent,
            search_context=json.dumps(search_context, ensure_ascii=False),
            used_keywords="、".join(used_keywords) if used_keywords else "无",
            current_post_count=len(retrieved_posts),
            target_count=_MIN_POSTS,
        )
        llm = _llm_expand

    try:
        resp = await llm.ainvoke(prompt)
        keywords = _parse_keywords_json(resp.content)

        # 确保原始查询也在关键词列表中（第 1 轮）
        if round_num == 1 and query not in keywords:
            keywords.insert(0, query)

        # 去重：只保留未使用过的新关键词
        used_set = set(used_keywords)
        new_keywords = [kw for kw in keywords if kw not in used_set]

        # 更新已使用关键词列表
        updated_used_keywords = used_keywords + new_keywords

        # 设置本轮要搜索的关键词批次
        current_batch = new_keywords[:5]  # 每轮最多 5 个关键词

        logger.info(
            f"[Retrieve][PlanKeywords] Round {round_num}: "
            f"generated={len(keywords)}, new={len(new_keywords)}, batch={current_batch}"
        )
    except Exception as e:
        logger.warning(f"[Retrieve][PlanKeywords] failed: {e}")
        # Fallback: 使用原始查询
        if round_num == 1:
            current_batch = [query]
            updated_used_keywords = used_keywords + [query]
        else:
            # 后续轮次 LLM 失败，尝试简单变体
            current_batch = [f"{query} 评测", f"{query} 怎么样"]
            updated_used_keywords = used_keywords + current_batch

    return {
        "_retrieve_round": round_num,
        "_current_batch": current_batch,
        "_used_keywords": updated_used_keywords,
    }


async def node_fetch_posts(state: GraphState, config: dict[str, Any]) -> dict[str, Any]:
    """ReAct Fetch Posts 节点：执行 MCP 检索

    功能：
      - 遍历 _current_batch 中的每个关键词
      - 调用 client.search_posts(keyword, require_num=5) 搜索帖子
      - 按 note_id 去重，新帖子追加到 retrieved_posts
      - 对新帖子调用 client.fetch_post_detail(url) 拉取详情
      - 新帖子总量上限 15 篇（单轮）
      - search_attempts += 1
    """
    current_batch = state.get("_current_batch", [])
    existing_posts = state.get("retrieved_posts", [])
    existing_ids = {p["note_id"] for p in existing_posts}

    # 从 config 获取 MCP client 和 queue
    client: XhsMcpClient = config.get("configurable", {}).get("client")
    queue = config.get("configurable", {}).get("queue")

    if not client:
        logger.warning("[Retrieve][FetchPosts] MCP client not found in config")
        return {"retrieved_posts": existing_posts, "search_attempts": state.get("search_attempts", 0) + 1}

    new_posts: list[dict[str, Any]] = []
    search_attempts = state.get("search_attempts", 0) + 1

    for keyword in current_batch:
        logger.info(f"[Retrieve][FetchPosts] 搜索：{keyword}")
        try:
            posts = await client.search_posts(keyword, require_num=5)
        except Exception as e:
            logger.warning(f"[Retrieve][FetchPosts] search_posts failed for '{keyword}': {e}")
            if "登录已过期" in str(e) or "login" in str(e).lower():
                raise RuntimeError("COOKIE_EXPIRED")
            continue

        for p in posts:
            note_id = p.get("note_id")
            if note_id and note_id not in existing_ids:
                existing_ids.add(note_id)
                new_posts.append(p)
                if len(new_posts) >= _MAX_POSTS_PER_ROUND:
                    break
        if len(new_posts) >= _MAX_POSTS_PER_ROUND:
            break

    if not new_posts:
        logger.info(f"[Retrieve][FetchPosts] 无新帖子，累计 {len(existing_posts)} 篇")
        return {"retrieved_posts": existing_posts, "search_attempts": search_attempts}

    # 逐篇拉取详情
    enriched: list[dict] = []
    total = len(new_posts)
    for i, post in enumerate(new_posts):
        try:
            url = post.get("note_url")
            if url:
                detail = await client.fetch_post_detail(url)
                merged = {**post, **detail}
            else:
                merged = post
        except Exception as e:
            logger.warning(f"[Retrieve][FetchPosts] fetch detail failed {post.get('note_id')}: {e}")
            merged = post
        enriched.append(merged)

        # 推送进度事件
        if queue is not None:
            title = merged.get("title") or merged.get("desc", "")[:20] or f"帖子 {i + 1}"
            queue.put_nowait({
                "event": "post_reading",
                "data": {"index": i + 1, "total": total, "title": title},
            })

    combined = existing_posts + enriched
    logger.info(f"[Retrieve][FetchPosts] 本轮获取 {len(enriched)} 篇，累计 {len(combined)} 篇")

    return {
        "retrieved_posts": combined,
        "search_attempts": search_attempts,
    }


async def node_check_coverage(state: GraphState) -> dict[str, Any]:
    """ReAct Check Coverage 节点：观察帖子数量

    功能：
      - 检查 retrieved_posts 数量是否 >= _MIN_POSTS
      - 检查是否达到最大轮次 _MAX_RETRIEVE_ROUNDS
      - 决定是否需要继续搜索

    注意：
      - 此节点只评估帖子数量，不评估帖子质量
      - 帖子质量评估由 Screen Agent 负责
    """
    total = len(state.get("retrieved_posts", []))
    round_num = state.get("_retrieve_round", 0)

    # 计算覆盖率分数
    coverage_score = min(total / _MIN_POSTS, 1.0)

    # 终止条件：数量够了 或 达到最大轮次
    should_stop = total >= _MIN_POSTS or round_num >= _MAX_RETRIEVE_ROUNDS

    logger.info(
        f"[Retrieve][CheckCoverage] Round {round_num}: "
        f"posts={total}, target={_MIN_POSTS}, coverage={coverage_score:.2f}, stop={should_stop}"
    )

    return {
        "_retrieve_done": should_stop,
        "retrieval_coverage_score": coverage_score,
    }


def _route_coverage(state: GraphState) -> Literal["plan_keywords", "__end__"]:
    """条件边：根据覆盖率检查结果决定是否继续循环"""
    if state.get("_retrieve_done"):
        return "__end__"
    return "plan_keywords"


def build_retrieve_graph():
    """构建检索 ReAct 子图

    完整 ReAct 循环：
      plan_keywords -> fetch_posts -> check_coverage
    """
    from langgraph.graph import StateGraph

    g = StateGraph(GraphState)

    # 添加所有节点
    g.add_node("plan_keywords", node_plan_keywords)
    g.add_node("fetch_posts", node_fetch_posts)
    g.add_node("check_coverage", node_check_coverage)

    # 设置入口点
    g.set_entry_point("plan_keywords")

    # 设置边连接
    g.add_edge("plan_keywords", "fetch_posts")
    g.add_edge("fetch_posts", "check_coverage")

    # 添加条件边：根据 Check Coverage 结果决定是否循环
    g.add_conditional_edges("check_coverage", _route_coverage)

    return g.compile()
