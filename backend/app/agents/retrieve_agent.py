"""Retrieve Subgraph — Function Calling 检索 Agent
职责：接收 Orchestrator 的意图分析结果，通过 Function Calling 让 LLM 自主决策
     搜索关键词和工具调用，爬取小红书帖子。

核心流程：
  node_retrieve_fc: LLM 接收工具定义，自主决策调用 search_posts，
                   观察结果后决定继续搜索或结束。

循环终止条件:
  - 去重后帖子总数 >= 目标数量（默认 7 篇）
  - 达到最大检索轮次（3 轮）
  - LLM 主动停止调用工具
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import Any, Literal

from loguru import logger

from app.models.schemas import GraphState
from app.prompts.templates import RETRIEVE_FC_SYSTEM_PROMPT
from app.tools.llm import ToolCall, create_llm
from app.tools.mcp_client import XhsMcpClient, XhsMcpClientPool
from app.tools.tool_schemas import RETRIEVE_TOOLS

_llm = create_llm(temperature=0)

_MAX_RETRIEVE_ROUNDS = 3
_MIN_POSTS = 7
_MAX_FC_ITERATIONS = 10  # 单轮最多工具调用次数


async def _execute_retrieve_tool(
    tc: ToolCall,
    pool: XhsMcpClientPool,
    existing_ids: set[str],
    exclude_set: set[str],
    new_posts: list[dict],
    new_keywords: list[str],
) -> dict[str, Any]:
    """执行单个工具调用，返回结果供 LLM 继续推理。"""
    if tc.name == "search_posts":
        keyword = tc.arguments.get("keyword", "")
        require_num = int(tc.arguments.get("require_num", 5))

        if keyword and keyword not in new_keywords:
            new_keywords.append(keyword)

        try:
            async with pool.borrow() as client:
                posts = await client.search_posts(keyword, require_num=require_num)

            added = 0
            for p in posts:
                note_id = p.get("note_id")
                if note_id and note_id not in existing_ids and note_id not in exclude_set:
                    existing_ids.add(note_id)
                    new_posts.append(p)
                    added += 1

            logger.info(f"[Retrieve][FC] search_posts '{keyword}': found={len(posts)}, new_added={added}")
            return {"status": "ok", "keyword": keyword, "found": len(posts), "new_added": added}
        except Exception as e:
            logger.warning(f"[Retrieve][FC] search_posts failed '{keyword}': {e}")
            if "登录已过期" in str(e) or "login" in str(e).lower():
                raise RuntimeError("COOKIE_EXPIRED")
            return {"status": "error", "error": str(e)}

    return {"status": "unknown_tool", "name": tc.name}


async def _fetch_details_concurrent(
    new_posts: list[dict],
    pool: XhsMcpClientPool,
    queue,
) -> list[dict]:
    """并发拉取帖子详情。"""
    enriched: list[dict] = []
    total = len(new_posts)

    async def _fetch_one(post: dict, index: int) -> dict | None:
        try:
            url = post.get("note_url")
            if url:
                await asyncio.sleep(random.uniform(0.5, 1.5))
                async with pool.borrow() as client:
                    detail = await client.fetch_post_detail(url)
                    merged = {**post, **detail}
                logger.info(f"[Retrieve][FC] detail {index+1}/{total}: {merged.get('title', '')[:20]}")
                return merged
            return post
        except Exception as e:
            logger.warning(f"[Retrieve][FC] fetch detail failed {post.get('note_id')}: {e}")
            return post

    tasks = [asyncio.create_task(_fetch_one(p, i)) for i, p in enumerate(new_posts)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            if "COOKIE_EXPIRED" in str(result) or "登录已过期" in str(result):
                raise RuntimeError("COOKIE_EXPIRED")
            enriched.append(new_posts[i])
        elif result is not None:
            enriched.append(result)

        if queue is not None:
            merged = enriched[-1] if enriched else new_posts[i]
            title = merged.get("title") or merged.get("desc", "")[:20] or f"帖子 {i+1}"
            queue.put_nowait({
                "event": "post_reading",
                "data": {"index": i + 1, "total": total, "title": title},
            })

    return enriched


async def node_retrieve_fc(state: GraphState, config: dict[str, Any]) -> dict[str, Any]:
    """Function Calling 版检索节点：LLM 自主决策搜索关键词和工具调用。

    LLM 接收工具定义（search_posts），在多轮对话中自主决策：
    1. 生成关键词并调用 search_posts
    2. 观察搜索结果，判断是否继续
    3. 帖子数量足够时停止调用工具
    """
    query = state.get("user_query_raw", "")
    intent = state.get("intent", "general")
    entities = state.get("product_entities", [])
    aliases = state.get("aliases", [])
    search_context = state.get("search_context", {})
    retrieved_posts = state.get("retrieved_posts", [])
    used_keywords = state.get("_used_keywords", [])
    round_num = state.get("_retrieve_round", 0) + 1
    reuse_ratio = state.get("_reuse_ratio", 0.0)
    exclude_note_ids = state.get("_exclude_note_ids", [])

    pool: XhsMcpClientPool = config.get("configurable", {}).get("pool")
    queue = config.get("configurable", {}).get("queue")
    api_type = config.get("configurable", {}).get("api_type", 2)

    # 计算目标帖子数
    target_posts = state.get("_target_posts") or None
    if target_posts is None:
        target_posts = _MIN_POSTS
        if reuse_ratio > 0:
            target_posts = max(3, int(_MIN_POSTS * (1 - reuse_ratio * 0.8)))
            logger.info(f"[Retrieve][FC] 记忆复用: reuse_ratio={reuse_ratio}, 目标={target_posts}")
    else:
        logger.info(f"[Retrieve][FC] workflow 传入目标: {target_posts}")

    if not pool:
        client: XhsMcpClient = config.get("configurable", {}).get("client")
        if not client:
            logger.warning("[Retrieve][FC] MCP client/pool not found")
            return {
                "retrieved_posts": [],
                "_retrieve_round": round_num,
                "_retrieve_done": True,
                "_target_posts": target_posts,
                "search_attempts": state.get("search_attempts", 0) + 1,
            }

    system_prompt = RETRIEVE_FC_SYSTEM_PROMPT.format(
        query=query,
        intent=intent,
        entities=",".join(entities) if entities else query,
        aliases=",".join(aliases) if aliases else "无",
        search_context=json.dumps(search_context, ensure_ascii=False),
        used_keywords="、".join(used_keywords) if used_keywords else "无",
        current_count=len(retrieved_posts),
        target_count=target_posts,
    )

    messages: list[dict] = [{"role": "user", "content": system_prompt}]
    new_posts: list[dict] = []
    new_keywords: list[str] = []
    existing_ids = {p["note_id"] for p in retrieved_posts if p.get("note_id")}
    exclude_set = set(exclude_note_ids)

    # Function Calling 多轮循环
    for iteration in range(_MAX_FC_ITERATIONS):
        try:
            resp = await _llm.ainvoke(messages, tools=RETRIEVE_TOOLS)
        except Exception as e:
            logger.warning(f"[Retrieve][FC] LLM 调用失败 iteration={iteration}: {e}")
            break

        # LLM 决定结束（无工具调用）
        if resp.finish_reason != "tool_calls" or not resp.tool_calls:
            logger.info(f"[Retrieve][FC] LLM 停止调用工具，iteration={iteration}")
            break

        # 将 assistant 消息加入对话历史
        messages.append({
            "role": "assistant",
            "content": resp.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                }
                for tc in resp.tool_calls
            ],
        })

        # 执行工具调用
        tool_results = []
        for tc in resp.tool_calls:
            result = await _execute_retrieve_tool(
                tc, pool, existing_ids, exclude_set, new_posts, new_keywords
            )
            tool_results.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

        messages.extend(tool_results)

        # 检查是否已达到目标
        total = len(retrieved_posts) + len(new_posts)
        if total >= target_posts:
            logger.info(f"[Retrieve][FC] 已达到目标 {target_posts} 篇，停止")
            break

        # 关键词间延迟（第一次不延迟）
        if iteration > 0:
            await asyncio.sleep(random.uniform(2.0, 4.0))

    # 拉取详情
    if api_type == 2 and new_posts:
        enriched = await _fetch_details_concurrent(new_posts, pool, queue)
    else:
        if api_type == 1:
            logger.info(f"[Retrieve][FC] api_type=1: 跳过详情拉取，保留 {len(new_posts)} 篇基本信息")
        enriched = new_posts

    total_after = len(retrieved_posts) + len(enriched)
    coverage_score = min(total_after / target_posts, 1.0) if target_posts > 0 else 1.0
    retrieve_done = total_after >= target_posts or round_num >= _MAX_RETRIEVE_ROUNDS

    # 检查关键错误：重试后仍然没有任何帖子
    critical_errors = []
    abort_analysis = False
    if total_after == 0:
        critical_errors.append({
            "stage": "retrieve",
            "error_type": "zero_posts",
            "message": "所有关键词搜索都失败或返回空结果",
            "keywords_tried": new_keywords,
            "search_attempts": state.get("search_attempts", 0) + 1,
        })
        abort_analysis = True
        logger.error(f"[Retrieve][FC] 关键错误: 无法获取任何帖子")

    logger.info(
        f"[Retrieve][FC] Round {round_num}: new={len(enriched)}, total={total_after}, "
        f"coverage={coverage_score:.2f}, done={retrieve_done}"
    )

    return {
        "retrieved_posts": enriched,
        "_used_keywords": new_keywords,
        "_retrieve_round": round_num,
        "_retrieve_done": retrieve_done,
        "_target_posts": target_posts,
        "_exclude_note_ids": list(exclude_set),
        "retrieval_coverage_score": coverage_score,
        "search_attempts": state.get("search_attempts", 0) + 1,
        "_critical_errors": critical_errors,
        "_abort_analysis": abort_analysis,
    }


def _route_coverage(state: GraphState) -> Literal["retrieve_fc", "__end__"]:
    if state.get("_retrieve_done"):
        return "__end__"
    return "retrieve_fc"


async def node_error_report(state: GraphState) -> dict[str, Any]:
    """生成错误报告并终止分析。"""
    critical_errors = state.get("_critical_errors", [])
    error_report = _generate_error_report(critical_errors)
    logger.info("[Retrieve][ErrorReport] 生成错误报告，终止分析")

    return {
        "final_answer": error_report,
        "confidence_score": 0.0,
        "limitations": ["系统错误导致无法完成分析"],
        "_retrieve_done": True,
        "_analyze_done": True,
    }


def _generate_error_report(critical_errors: list[dict]) -> str:
    """生成用户友好的错误报告。"""
    sections = []
    sections.append("# ⚠️ 分析失败报告\n\n")
    sections.append("很抱歉，系统在分析过程中遇到了关键错误，无法完成分析。\n\n")

    retrieve_errors = [e for e in critical_errors if e.get("stage") == "retrieve"]
    analyze_errors = [e for e in critical_errors if e.get("stage") == "analyze"]

    if retrieve_errors:
        sections.append("## 检索阶段错误\n\n")
        for err in retrieve_errors:
            if err.get("error_type") == "zero_posts":
                sections.append("- **无法获取帖子**: 所有关键词搜索都失败或返回空结果\n")
                keywords = err.get("keywords_tried", [])
                if keywords:
                    sections.append(f"  - 尝试关键词: {', '.join(keywords)}\n")

    if analyze_errors:
        sections.append("## 分析阶段错误\n\n")
        for err in analyze_errors:
            if err.get("error_type") == "no_data":
                sections.append(f"- **无法获取内容**: 找到{err.get('posts_count', 0)}篇帖子，但评论爬取全部失败且帖子正文为空\n")

    sections.append("\n## 建议\n\n")
    sections.append("1. 检查小红书Cookie是否过期\n")
    sections.append("2. 检查网络连接是否正常\n")
    sections.append("3. 稍后重试\n")

    return "".join(sections)


def build_retrieve_graph():
    """构建 Function Calling 版检索子图。"""
    from langgraph.graph import StateGraph, END

    g = StateGraph(GraphState)
    g.add_node("retrieve_fc", node_retrieve_fc)
    g.add_node("error_report", node_error_report)

    g.set_entry_point("retrieve_fc")
    # 关键错误时跳到 error_report，否则正常循环
    g.add_conditional_edges("retrieve_fc", _route_after_retrieve)
    g.add_edge("error_report", END)

    return g.compile()


def _route_after_retrieve(state: GraphState) -> Literal["error_report", "retrieve_fc", "__end__"]:
    """关键错误时直接跳到错误报告节点。"""
    if state.get("_abort_analysis"):
        return "error_report"
    if state.get("_retrieve_done"):
        return "__end__"
    return "retrieve_fc"
