"""Analyze Agent — 拉取评论并做观点聚类与情感分析。
实现 main_graph.py 中 fetch_comments_batch / dedupe_and_cluster / opinion_analysis 节点。

优化：使用 XhsMcpClientPool 连接池，只启动 pool_size 个 MCP 子进程，
所有帖子通过 pool.borrow() 复用连接，真正并发且无重复进程开销。
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import Any

from loguru import logger

from app.models.schemas import GraphState
from app.prompts.templates import OPINION_PROMPT
from app.tools.llm import create_llm
from app.tools.mcp_client import XhsMcpClient, XhsMcpClientPool

_llm = create_llm(temperature=0)


async def _fetch_comments_with_retry(
    client, note_url: str, note_id: str, max_retries: int = 2
) -> list[dict]:
    """获取评论，失败后随机延迟重试。"""
    for attempt in range(max_retries + 1):
        try:
            return await client.search_comments(note_url)
        except Exception as e:
            if attempt < max_retries:
                wait = 2 + attempt * 2 + random.uniform(0, 1)
                logger.warning(
                    f"[Analyze] 评论获取失败，{wait:.1f}s 后重试 "
                    f"({attempt+1}/{max_retries}) {note_id}: {e}"
                )
                await asyncio.sleep(wait)
            else:
                logger.warning(f"[Analyze] 评论获取彻底失败 {note_id}: {e}")
                return []
    return []  # unreachable


async def _process_post(post: dict, state: GraphState, pool: XhsMcpClientPool) -> tuple[list[dict], list[dict]]:
    """从连接池借用客户端，拉取评论并做观点聚类。"""
    note_id = post.get("note_id", "")
    note_url = post.get("note_url", "")

    try:
        async with pool.borrow() as client:
            # 随机延迟，避免多个 client 同时发出请求被识别为爬虫
            await asyncio.sleep(random.uniform(0.5, 2.0))
            comments = await _fetch_comments_with_retry(client, note_url, note_id)
    except Exception as e:
        logger.warning(f"[Analyze] 借用客户端失败 {note_id}: {e}")
        comments = []

    # 将帖子正文作为博主评论插入，博主本人内容也是真实观点来源
    desc = post.get("desc", "").strip()
    if desc:
        synthetic = {
            "comment_id": "__post_body__",
            "content": desc,
            "nickname": "[博主]",
        }
        comments = [synthetic] + comments

    clusters: list[dict] = []
    if comments:
        prompt = OPINION_PROMPT.format(
            query=state.get("user_query_raw", ""),
            title=post.get("title", ""),
            desc=(post.get("desc") or "")[:200],
            comments_json=json.dumps(
                [{"content": c.get("content", ""), "like_count": c.get("like_count", 0)}
                 for c in comments[:50]],
                ensure_ascii=False,
            ),
        )
        try:
            resp = await _llm.ainvoke(prompt)
            data = json.loads(resp.content)
            clusters = data.get("clusters", [])
            for cl in clusters:
                cl["source_note_id"] = note_id
                cl["source_title"] = post.get("title", "无标题")
                cl["source_note_url"] = note_url
        except Exception as e:
            logger.warning(f"[Analyze] 观点聚类失败 {note_id}: {e}")

    return comments, clusters


async def _process_post_desc_only(post: dict, state: GraphState) -> tuple[list[dict], list[dict]]:
    """只用帖子正文（desc）做观点聚类，不拉取评论。"""
    note_id = post.get("note_id", "")
    note_url = post.get("note_url", "")
    desc = post.get("desc", "").strip()
    if not desc:
        return [], []
    comments = [{
        "comment_id": "__post_body__",
        "content": desc,
        "nickname": "[博主]",
    }]
    clusters: list[dict] = []
    prompt = OPINION_PROMPT.format(
        query=state.get("user_query_raw", ""),
        title=post.get("title", ""),
        desc=desc[:200],
        comments_json=json.dumps(
            [{"content": c.get("content", ""), "like_count": c.get("like_count", 0)} for c in comments],
            ensure_ascii=False,
        ),
    )
    try:
        resp = await _llm.ainvoke(prompt)
        data = json.loads(resp.content)
        clusters = data.get("clusters", [])
        for cl in clusters:
            cl["source_note_id"] = note_id
            cl["source_title"] = post.get("title", "无标题")
            cl["source_note_url"] = note_url
    except Exception as e:
        logger.warning(f"[Analyze] 正文聚类失败 {note_id}: {e}")
    return comments, clusters


async def fetch_and_analyze(state: GraphState, pool: XhsMcpClientPool) -> dict[str, Any]:
    """对每篇筛选帖子并发拉取评论并做观点聚类，复用连接池，一次性返回所有字段。"""
    posts = state.get("screened_items", [])
    if not posts:
        return {
            "retrieved_comments": [],
            "clusters": [],
            "sentiment_summary": {},
            "evidence_ledger": [],
        }

    logger.info(f"[Analyze] 并发处理 {len(posts)} 篇帖子（评论拉取限前3，其余只用正文）")
    # 按评论数降序，top-3 拉评论，其余只用帖子正文
    posts_sorted = sorted(posts, key=lambda p: int(p.get("comment_count") or 0), reverse=True)
    comment_posts = posts_sorted[:3]
    desc_only_posts = posts_sorted[3:]
    comment_tasks = [_process_post(p, state, pool) for p in comment_posts]
    desc_tasks = [_process_post_desc_only(p, state) for p in desc_only_posts]

    # 替换原有的 asyncio.wait_for 块为以下稳健的 gather 实现
    # 使用 return_exceptions=True 确保个别任务失败不会阻塞整体流程
    results = await asyncio.gather(*comment_tasks, *desc_tasks, return_exceptions=True)
    
    # 统一处理结果：将所有异常转化为空结果 ([], [])，防止程序崩溃
    final_results = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning(f"[Analyze] 任务执行出错: {r}")
            final_results.append(([], []))
        else:
            final_results.append(r)
    results = final_results

    all_comments: list[dict] = []
    all_clusters: list[dict] = []
    seen_comment_ids: set[str] = set()

    for comments, clusters in results:
        for c in comments:
            cid = c.get("comment_id", "")
            if cid and cid not in seen_comment_ids:
                seen_comment_ids.add(cid)
                all_comments.append(c)
        all_clusters.extend(clusters)

    # 汇总情感
    sentiment_counts: dict[str, int] = {}
    for cl in all_clusters:
        s = cl.get("sentiment", "中立")
        sentiment_counts[s] = sentiment_counts.get(s, 0) + cl.get("count", 1)

    evidence_ledger = [
        {"topic": cl.get("topic"), "sentiment": cl.get("sentiment"),
         "quotes": cl.get("evidence_quotes", []), "source": cl.get("source_title", "")}
        for cl in all_clusters
    ]

    logger.info(f"[Analyze] {len(all_comments)} 条评论，{len(all_clusters)} 个观点簇")
    return {
        "retrieved_comments": all_comments,
        "clusters": all_clusters,
        "sentiment_summary": sentiment_counts,
        "evidence_ledger": evidence_ledger,
    }
