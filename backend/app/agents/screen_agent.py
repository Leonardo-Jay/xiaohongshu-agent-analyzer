"""Screen Agent — 用 Claude Haiku 筛选相关帖子，过滤广告/无关内容。
实现 main_graph.py 中 content_review_filter 节点的真实逻辑。
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger

from app.models.schemas import GraphState
from app.prompts.templates import SCREEN_PROMPT
from app.tools.llm import create_llm

_llm = create_llm(temperature=0)


async def screen_posts(state: GraphState) -> dict[str, Any]:
    """调用 Claude 筛选相关帖子，返回 screened_items + screening_stats。"""
    posts = state.get("retrieved_posts", [])
    if not posts:
        return {
            "screened_items": [],
            "screening_stats": {"total": 0, "passed": 0, "rejected": 0, "reject_reasons": []},
        }

    slim = [
        {
            "note_id": p.get("note_id", ""),
            "title": p.get("title", ""),
            "desc": (p.get("desc") or "")[:150],
            "like_count": p.get("like_count", 0),
            "comment_count": p.get("comment_count", 0),
        }
        for p in posts
    ]

    prompt = SCREEN_PROMPT.format(
        query=state.get("user_query_raw", ""),
        posts_json=json.dumps(slim, ensure_ascii=False),
    )

    try:
        resp = await _llm.ainvoke(prompt)
        data = json.loads(resp.content)
        selected_ids: list[str] = data.get("selected_ids", [])
    except Exception as e:
        logger.warning(f"[Screen] LLM 筛选失败，降级取前 5: {e}")
        selected_ids = [p["note_id"] for p in posts[:8]]

    id_set = set(selected_ids)
    screened = [p for p in posts if p.get("note_id") in id_set]
    order = {nid: i for i, nid in enumerate(selected_ids)}
    screened.sort(key=lambda p: order.get(p.get("note_id", ""), 999))

    rejected = len(posts) - len(screened)
    logger.info(f"[Screen] 筛选出 {len(screened)} 篇，淘汰 {rejected} 篇")
    return {
        "screened_items": screened,
        "screening_stats": {
            "total": len(posts),
            "passed": len(screened),
            "rejected": rejected,
            "reject_reasons": [],
        },
    }
