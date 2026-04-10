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

import asyncio
import json
import random
from typing import Any, Literal

from loguru import logger

from app.models.schemas import GraphState
from app.prompts.templates import REACT_ACTION_PROMPT, RETRIEVE_EXPAND_PROMPT
from app.tools.llm import create_llm
from app.tools.mcp_client import XhsMcpClient, XhsMcpClientPool

_llm_first = create_llm(temperature=0)
_llm_expand = create_llm(temperature=0)

_MAX_RETRIEVE_ROUNDS = 3  # 最多 3 轮 ReAct 循环
_MIN_POSTS = 7  # 目标最少帖子数
_MAX_POSTS_PER_ROUND = 7  # 单轮最多新帖子


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

    # 获取记忆复用参数
    reuse_ratio = state.get("_reuse_ratio", 0.0)
    exclude_note_ids = state.get("_exclude_note_ids", [])
    exclude_set = set(exclude_note_ids) if exclude_note_ids else set()

    # 优先使用 workflow 传入的目标（增量模式时已计算），否则自己计算
    target_posts = state.get("_target_posts")
    if target_posts is None:
        target_posts = _MIN_POSTS
        if reuse_ratio > 0:
            # 最多减少 80%
            target_posts = max(3, int(_MIN_POSTS * (1 - reuse_ratio * 0.8)))
            logger.info(f"[Retrieve] 记忆复用模式: reuse_ratio={reuse_ratio}, 目标={target_posts} 篇, 排除 {len(exclude_set)} 个历史帖子")
    else:
        logger.info(f"[Retrieve] 使用 workflow 传入的目标: {target_posts} 篇, 排除 {len(exclude_set)} 个历史帖子")

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
            target_count=target_posts,
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
        "_target_posts": target_posts,
        "_exclude_note_ids": list(exclude_set),
    }


async def node_fetch_posts(state: GraphState, config: dict[str, Any]) -> dict[str, Any]:
    """ReAct Fetch Posts 节点：并发执行 MCP 检索（使用连接池）

    功能：
      - 从配置中获取连接池（大小 MCP_POOL_SIZE，默认 2）
      - 使用连接池并发搜索帖子（每个关键词一个任务）
      - 按 note_id 去重，新帖子追加到 retrieved_posts
      - 对新帖子并发调用 client.fetch_post_detail(url) 拉取详情
      - 新帖子总量上限 15 篇（单轮）
      - search_attempts += 1

    加速设计：
      - 搜索阶段：使用连接池并发执行多个关键词搜索
      - 详情阶段：并发拉取所有新帖子的详情
      - 完成后连接池自动关闭，释放资源
    """
    current_batch = state.get("_current_batch", [])
    existing_posts = state.get("retrieved_posts", [])
    existing_ids = {p["note_id"] for p in existing_posts}

    # 获取排除列表（来自记忆复用）
    exclude_note_ids = state.get("_exclude_note_ids", [])
    exclude_set = set(exclude_note_ids) if exclude_note_ids else set()

    # 从 config 获取连接池和 queue
    pool: XhsMcpClientPool = config.get("configurable", {}).get("pool")
    queue = config.get("configurable", {}).get("queue")

    # 如果没有连接池，尝试从 client 创建临时连接池
    if not pool:
        client: XhsMcpClient = config.get("configurable", {}).get("client")
        if client:
            logger.warning("[Retrieve][FetchPosts] 未找到连接池，使用单个 client 串行执行")
            return await _fetch_posts_serial(current_batch, existing_posts, existing_ids, client, queue)
        logger.warning("[Retrieve][FetchPosts] MCP client/pool not found in config")
        return {"retrieved_posts": existing_posts, "search_attempts": state.get("search_attempts", 0) + 1}

    new_posts: list[dict[str, Any]] = []
    search_attempts = state.get("search_attempts", 0) + 1

    # 并发搜索：每个关键词一个任务
    async def _search_keyword(keyword: str) -> list[dict]:
        try:
            # 添加随机延迟（1.0~2.5 秒），模拟人类搜索行为
            await asyncio.sleep(random.uniform(1.0, 2.5))
            async with pool.borrow() as client:
                posts = await client.search_posts(keyword, require_num=4)
                logger.info(f"[Retrieve][FetchPosts] 搜索 '{keyword}' 获取 {len(posts)} 篇")
                return posts
        except Exception as e:
            logger.warning(f"[Retrieve][FetchPosts] search_posts failed for '{keyword}': {e}")
            if "登录已过期" in str(e) or "login" in str(e).lower():
                raise RuntimeError("COOKIE_EXPIRED")
            return []

    # 执行并发搜索
    search_tasks = [asyncio.create_task(_search_keyword(kw)) for kw in current_batch]
    search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

    # 收集结果并去重，同时过滤排除列表
    for result in search_results:
        if isinstance(result, Exception):
            # 检查是否是 Cookie 过期错误
            if "COOKIE_EXPIRED" in str(result) or "登录已过期" in str(result):
                logger.error(f"[Retrieve][FetchPosts] 检测到 Cookie 过期，终止流程")
                raise RuntimeError("COOKIE_EXPIRED")
            continue
        for p in result:
            note_id = p.get("note_id")
            # 排除历史已分析帖子
            if note_id in exclude_set:
                continue
            if note_id and note_id not in existing_ids:
                existing_ids.add(note_id)
                new_posts.append(p)
                if len(new_posts) >= _MAX_POSTS_PER_ROUND:
                    break
        if len(new_posts) >= _MAX_POSTS_PER_ROUND:
            break

    # 记录实际排除的数量
    if exclude_set:
        logger.info(f"[Retrieve][FetchPosts] 过滤了 {len(exclude_set)} 个历史帖子")

    if not new_posts:
        logger.info(f"[Retrieve][FetchPosts] 无新帖子，累计 {len(existing_posts)} 篇")
        return {"retrieved_posts": existing_posts, "search_attempts": search_attempts}

    # 并发拉取详情
    enriched = await _fetch_details_concurrent(new_posts, pool, queue)

    combined = existing_posts + enriched
    logger.info(f"[Retrieve][FetchPosts] 本轮获取 {len(enriched)} 篇，累计 {len(combined)} 篇")

    return {
        "retrieved_posts": combined,
        "search_attempts": search_attempts,
    }


async def _fetch_posts_serial(
    current_batch: list[str],
    existing_posts: list[dict],
    existing_ids: set[str],
    client: XhsMcpClient,
    queue
) -> dict[str, Any]:
    """串行版本（向后兼容）：当没有连接池时使用。"""
    new_posts: list[dict[str, Any]] = []
    search_attempts = 1

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


async def _fetch_details_concurrent(
    new_posts: list[dict],
    pool: XhsMcpClientPool,
    queue
) -> list[dict]:
    """并发拉取帖子详情。"""
    enriched: list[dict] = []
    total = len(new_posts)

    async def _fetch_detail(post: dict, index: int) -> dict | None:
        try:
            url = post.get("note_url")
            if url:
                # 添加随机延迟（0.5~1.5 秒）
                await asyncio.sleep(random.uniform(0.5, 1.5))
                async with pool.borrow() as client:
                    detail = await client.fetch_post_detail(url)
                    merged = {**post, **detail}
                logger.info(f"[Retrieve][FetchDetails] {index + 1}/{total}: {merged.get('title', '无标题')[:20]}")
                return merged
            return post
        except Exception as e:
            logger.warning(f"[Retrieve][FetchDetails] fetch detail failed {post.get('note_id')}: {e}")
            return post

    # 并发拉取详情（使用连接池，自动限流）
    detail_tasks = [asyncio.create_task(_fetch_detail(p, i)) for i, p in enumerate(new_posts)]
    results = await asyncio.gather(*detail_tasks, return_exceptions=True)

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            # 检查是否是 Cookie 过期错误
            if "COOKIE_EXPIRED" in str(result) or "登录已过期" in str(result):
                logger.error(f"[Retrieve][FetchDetails] 检测到 Cookie 过期，终止流程")
                raise RuntimeError("COOKIE_EXPIRED")
            enriched.append(new_posts[i])
        elif result is not None:
            enriched.append(result)

        # 推送进度事件
        if queue is not None:
            merged = enriched[-1] if enriched else new_posts[i]
            title = merged.get("title") or merged.get("desc", "")[:20] or f"帖子 {i + 1}"
            queue.put_nowait({
                "event": "post_reading",
                "data": {"index": i + 1, "total": total, "title": title},
            })

    return enriched


async def node_check_coverage(state: GraphState) -> dict[str, Any]:
    """ReAct Check Coverage 节点：观察帖子数量

    功能：
      - 检查 retrieved_posts 数量是否 >= 动态目标（支持记忆复用调整）
      - 检查是否达到最大轮次 _MAX_RETRIEVE_ROUNDS
      - 决定是否需要继续搜索

    注意：
      - 此节点只评估帖子数量，不评估帖子质量
      - 帖子质量评估由 Screen Agent 负责
    """
    total = len(state.get("retrieved_posts", []))
    round_num = state.get("_retrieve_round", 0)

    # 获取动态目标（记忆复用时可能减少）
    target_posts = state.get("_target_posts", _MIN_POSTS)

    # 计算覆盖率分数
    coverage_score = min(total / target_posts, 1.0) if target_posts > 0 else 1.0

    # 终止条件：数量够了 或 达到最大轮次
    should_stop = total >= target_posts or round_num >= _MAX_RETRIEVE_ROUNDS

    logger.info(
        f"[Retrieve][CheckCoverage] Round {round_num}: "
        f"posts={total}, target={target_posts}, coverage={coverage_score:.2f}, stop={should_stop}"
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
