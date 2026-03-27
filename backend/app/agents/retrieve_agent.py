import asyncio
import json
from typing import Any

from loguru import logger

from app.models.schemas import GraphState
from app.prompts.templates import EXPAND_PROMPT
from app.tools.llm import create_llm
from app.tools.mcp_client import XhsMcpClient

_llm = create_llm(temperature=0)


async def retrieve_posts(state: GraphState, client: XhsMcpClient, queue: asyncio.Queue | None = None) -> dict[str, Any]:
    """遍历 query_plan，对每个词搜索最多 10 条帖子，合并去重，返回 retrieved_posts。"""
    query_plan: list[str] = state.get("query_plan") or [state.get("user_query_raw", "")]
    existing_ids: set[str] = {p["note_id"] for p in state.get("retrieved_posts", [])}
    new_posts: list[dict[str, Any]] = []

    for q in query_plan:
        logger.info(f"[Retrieve] 搜索: {q}")
        try:
            posts = await client.search_posts(q, require_num=10)
        except Exception as e:
            logger.warning(f"[Retrieve] search_posts failed for '{q}': {e}")
            if "登录已过期" in str(e) or "login" in str(e).lower():
                raise RuntimeError("COOKIE_EXPIRED")
            continue
        for p in posts:
            if p.get("note_id") not in existing_ids:
                existing_ids.add(p["note_id"])
                new_posts.append(p)

    if not new_posts:
        return {"retrieved_posts": list(state.get("retrieved_posts", []))}

    # 逐篇拉取详情，每篇完成后推送进度
    enriched: list[dict] = []
    total = len(new_posts)
    for i, post in enumerate(new_posts):
        try:
            url = post["note_url"]
            logger.debug(f"[Retrieve] fetch_post_detail url={url}")
            detail = await client.fetch_post_detail(url)
            merged = {**post, **detail}
        except Exception as e:
            logger.warning(f"[Retrieve] fetch detail failed {post.get('note_id')}: {e}")
            merged = post
        enriched.append(merged)
        if queue is not None:
            title = merged.get("title") or merged.get("desc", "")[:20] or f"帖子 {i+1}"
            queue.put_nowait({
                "event": "post_reading",
                "data": {"index": i + 1, "total": total, "title": title},
            })

    combined = list(state.get("retrieved_posts", [])) + enriched
    logger.info(f"[Retrieve] 共获取 {len(combined)} 篇帖子")
    return {"retrieved_posts": combined}


async def expand_queries_fallback(state: GraphState) -> dict[str, Any]:
    """覆盖率不足时调用 Claude 生成新搜索词，追加到 query_plan，search_attempts += 1。"""
    query = state.get("user_query_raw", "")
    used = state.get("query_plan", [query])
    post_count = len(state.get("retrieved_posts", []))
    attempts = state.get("search_attempts", 0) + 1

    prompt = EXPAND_PROMPT.format(
        query=query,
        used_queries="、".join(used),
        post_count=post_count,
    )

    try:
        resp = await _llm.ainvoke(prompt)
        data = json.loads(resp.content)
        new_queries: list[str] = data.get("new_queries", [])
    except Exception as e:
        logger.warning(f"[Retrieve] expand_queries_fallback LLM failed: {e}")
        new_queries = []

    # 追加不重复的新词
    used_set = set(used)
    appended = [q for q in new_queries if q not in used_set]
    new_plan = used + appended
    logger.info(f"[Retrieve] fallback attempt={attempts}, 新词={appended}")
    return {"query_plan": new_plan, "search_attempts": attempts}
