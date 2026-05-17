"""Analyze Subgraph — 评论拉取与观点聚类 Agent
职责：接收 Screen 子图输出的筛选结果（screened_items），通过 Function Calling 让 LLM
     自主决策爬取哪些帖子的评论，并进行观点聚类、情感分析。

核心流程：
  1. Fetch Comments FC: LLM 通过 Function Calling 决策爬取哪些帖子的评论
  2. Cluster Opinions: 对所有爬取的评论进行观点聚类（LLM，60 秒超时）
  3. Validate Clusters: 验证观点簇与意图的相关性（LLM，30 秒超时）
  4. Check Quality: 质量检查（规则），决定是否需要继续爬取

循环终止条件：
  - 评论总数 >= 50 条 且 有冲突观点（正负面都有）→ 结束
  - 评论总数 >= 40 条 且 观点簇 >= 5 个 → 结束
  - 已达到 2 轮循环上限 → 结束
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any, Literal

from loguru import logger

from app.models.schemas import GraphState
from app.prompts.templates import (
    ANALYZE_FC_SYSTEM_PROMPT,
    CONTENT_TIME_ANALYSIS_PROMPT,
    OPINION_PROMPT,
    VALIDATE_CLUSTERS_PROMPT,
)
from app.tools.llm import create_llm
from app.tools.mcp_client import XhsMcpClientPool
from app.tools.tool_schemas import ANALYZE_TOOLS
from app.utils.temporal import parse_xhs_time

_llm = create_llm(temperature=0)

_MAX_ANALYZE_ROUNDS = 2
_MIN_COMMENTS = 40
_TARGET_COMMENTS = 50
_TOP_POSTS_PER_ROUND = 3
_MAX_EVIDENCE_FOR_CLUSTERING = 200
_MAX_EVIDENCE_FOR_TIME_ANALYSIS = 60
_BANNED_CONTENT_TIME_TERMS = ("较早样本", "中后段样本", "爆发期", "扩散期", "沉淀期", "复燃期", "传播生命周期")


def _is_valid_comment(content: str) -> bool:
    if len(content) < 2:
        return False
    emoji_pattern = re.compile(
        r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF'
        r'\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF]'
    )
    text_only = emoji_pattern.sub('', content)
    if not text_only.strip():
        return False
    text_only = re.sub(r'[^\w\u4e00-\u9fff]', '', content)
    if not text_only.strip():
        return False
    if re.search(r'(.)\1{2,}', content) and len(set(content)) < 3:
        return False
    return True


def _filter_invalid_comments(comments: list[dict]) -> tuple[list[dict], int]:
    valid = [c for c in comments if _is_valid_comment(c.get("content", ""))]
    return valid, len(comments) - len(valid)


def _source_type(comment: dict[str, Any]) -> str:
    if str(comment.get("comment_id", "")).startswith("__post_body__") or comment.get("nickname") == "[博主]":
        return "post_body"
    return "comment"


def _build_evidence_registry(
    screened_items: list[dict[str, Any]],
    raw_comments: list[dict[str, Any]],
    current_time: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    id_to_post = {p.get("note_id"): p for p in screened_items}
    registry = []
    seen_content = set()
    for index, comment in enumerate(raw_comments[:_MAX_EVIDENCE_FOR_CLUSTERING]):
        content = str(comment.get("content", "")).strip()
        if not content:
            continue
        note_id = comment.get("note_id", "")
        dedupe_key = (note_id, content)
        if dedupe_key in seen_content:
            continue
        seen_content.add(dedupe_key)
        post = id_to_post.get(note_id, {})
        raw_time = (
            comment.get("create_time")
            or comment.get("created_at_raw")
            or post.get("upload_time")
            or post.get("published_at_raw")
            or ""
        )
        created_at, created_at_raw, parsed = parse_xhs_time(raw_time, current_time=current_time)
        registry.append({
            "evidence_id": f"ev_{len(registry):03d}",
            "source_type": _source_type(comment),
            "content": content,
            "note_id": note_id,
            "created_at": created_at,
            "created_at_raw": created_at_raw,
            "time_parseable": parsed,
            "source_title": post.get("title", "无标题"),
            "source_url": post.get("note_url", ""),
            "like_count": int(comment.get("like_count", 0) or 0),
            "nickname": comment.get("nickname", ""),
            "order": index,
        })
    return registry


def _evidence_for_prompt(registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in registry[:_MAX_EVIDENCE_FOR_CLUSTERING]:
        rows.append({
            "evidence_id": item["evidence_id"],
            "source_type": item.get("source_type", "comment"),
            "content": item.get("content", ""),
            "like_count": item.get("like_count", 0),
            "nickname": item.get("nickname", ""),
            "note_id": item.get("note_id", ""),
            "created_at": item.get("created_at", ""),
            "created_at_raw": item.get("created_at_raw", ""),
        })
    return rows


def _assign_evidence_ids_to_clusters(clusters: list[dict[str, Any]], registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid_ids = {item["evidence_id"] for item in registry}
    content_by_id = {item["evidence_id"]: item.get("content", "") for item in registry}
    for cluster in clusters:
        ids = [eid for eid in cluster.get("evidence_ids", []) if eid in valid_ids]
        quotes = [str(q or "").strip() for q in cluster.get("evidence_quotes", []) if str(q or "").strip()]
        if not ids and quotes:
            for quote in quotes:
                quote_compact = re.sub(r"\s+", "", quote)
                for item in registry:
                    content_compact = re.sub(r"\s+", "", item.get("content", ""))
                    if quote_compact and (quote_compact in content_compact or content_compact in quote_compact):
                        ids.append(item["evidence_id"])
                        break
        if ids and not quotes:
            quotes = [content_by_id[eid] for eid in ids[:2] if content_by_id.get(eid)]
        cluster["evidence_ids"] = list(dict.fromkeys(ids))[:5]
        cluster["evidence_quotes"] = quotes[:2]
    return clusters


def _attach_cluster_sources(clusters: list[dict[str, Any]], registry: list[dict[str, Any]]) -> None:
    evidence_by_id = {item["evidence_id"]: item for item in registry}
    for cluster in clusters:
        first = next((evidence_by_id.get(eid) for eid in cluster.get("evidence_ids", []) if evidence_by_id.get(eid)), None)
        if first:
            cluster["source_note_url"] = first.get("source_url", "")
            cluster["source_title"] = first.get("source_title", "无标题")


async def _fetch_comments_with_retry(
    client, note_url: str, note_id: str, max_retries: int = 2
) -> list[dict]:
    for attempt in range(max_retries + 1):
        try:
            return await client.search_comments(note_url)
        except Exception as e:
            if attempt < max_retries:
                wait = 2 + attempt * 2 + random.uniform(0, 1)
                logger.warning(f"[Analyze] 评论获取失败，{wait:.1f}s 后重试 ({attempt+1}/{max_retries}) {note_id}: {e}")
                await asyncio.sleep(wait)
            else:
                logger.warning(f"[Analyze] 评论获取彻底失败 {note_id}: {e}")
                return []
    return []


async def _use_post_body_as_comments(state: GraphState) -> dict[str, Any]:
    screened_items = state.get("screened_items", [])
    comments = []
    for post in screened_items:
        note_id = post.get("note_id", "")
        desc = post.get("desc", "").strip()
        title = post.get("title", "").strip()
        if desc or title:
            content = f"{title}\n\n{desc}" if title and desc else (title or desc)
            comments.append({
                "comment_id": f"__post_body__{note_id}",
                "content": content,
                "nickname": post.get("nickname", "[博主]"),
                "note_id": note_id,
                "create_time": post.get("upload_time", "") or post.get("published_at", ""),
            })
    logger.info(f"[Analyze][FC] api_type=1: 使用 {len(comments)} 篇帖子正文作为评论")
    return {
        "retrieved_comments": comments,
        "_raw_comments_for_clustering": comments,
        "_fetched_comment_count": len(comments),
        "_filtered_comment_count": 0,
        "_analyze_round": state.get("_analyze_round", 0) + 1,
        "_analyze_done": True,
    }


async def node_fetch_comments_fc(state: GraphState, config: dict) -> dict[str, Any]:
    """Function Calling 版评论爬取节点：LLM 自主决策爬取哪些帖子的评论。"""
    api_type = config.get("configurable", {}).get("api_type", 2)
    if api_type == 1:
        return await _use_post_body_as_comments(state)

    screened_items = state.get("screened_items", [])
    existing_comments = state.get("retrieved_comments", [])
    fetched_ids = set(state.get("_posts_to_fetch", []))
    round_num = state.get("_analyze_round", 0) + 1
    reuse_ratio = state.get("_reuse_ratio", 0.0)
    pool: XhsMcpClientPool = config.get("configurable", {}).get("pool")

    if not screened_items:
        logger.warning("[Analyze][FC] 无可供分析的帖子")
        return {
            "retrieved_comments": [],
            "_raw_comments_for_clustering": state.get("_raw_comments_for_clustering", []),
            "_fetched_comment_count": len(existing_comments),
            "_filtered_comment_count": state.get("_filtered_comment_count", 0),
            "_analyze_round": round_num,
            "_analyze_done": True,
        }

    if not pool:
        logger.warning("[Analyze][FC] 连接池未找到")
        return {
            "retrieved_comments": [],
            "_raw_comments_for_clustering": state.get("_raw_comments_for_clustering", []),
            "_fetched_comment_count": len(existing_comments),
            "_filtered_comment_count": state.get("_filtered_comment_count", 0),
            "_analyze_round": round_num,
        }

    # 计算本轮目标爬取数量
    base_num = _TOP_POSTS_PER_ROUND
    if reuse_ratio > 0.3:
        base_num = max(2, int(base_num * (1 - min(0.7, reuse_ratio * 0.8))))
        logger.info(f"[Analyze][FC] 记忆复用: reuse_ratio={reuse_ratio}, 本轮爬取 {base_num} 篇")

    # 构建帖子摘要供 LLM 决策（排除已爬取的）
    remaining_posts = [p for p in screened_items if p.get("note_id") not in fetched_ids]
    posts_summary = [
        {
            "note_id": p.get("note_id"),
            "note_url": p.get("note_url"),
            "title": p.get("title", "")[:40],
            "comment_count": p.get("comment_count", 0),
            "relevance_score": round(float(p.get("relevance_score") or 0.5), 2),
        }
        for p in remaining_posts[:10]
    ]

    if not posts_summary:
        logger.info("[Analyze][FC] 所有帖子已爬取完毕")
        return {
            "retrieved_comments": [],
            "_raw_comments_for_clustering": state.get("_raw_comments_for_clustering", []),
            "_fetched_comment_count": len(existing_comments),
            "_filtered_comment_count": state.get("_filtered_comment_count", 0),
            "_analyze_round": round_num,
            "_analyze_done": True,
        }

    system_prompt = ANALYZE_FC_SYSTEM_PROMPT.format(
        query=state.get("user_query_raw", ""),
        current_comment_count=len(existing_comments),
        target_comment_count=_TARGET_COMMENTS,
        round_num=round_num,
        max_rounds=_MAX_ANALYZE_ROUNDS,
        posts_json=json.dumps(posts_summary, ensure_ascii=False),
        max_posts_this_round=base_num,
    )

    messages: list[dict] = [{"role": "user", "content": system_prompt}]
    all_new_comments: list[dict] = []
    selected_ids: list[str] = []
    total_filtered = 0

    # Function Calling 多轮循环
    for _ in range(base_num + 2):
        try:
            resp = await _llm.ainvoke(messages, tools=ANALYZE_TOOLS)
        except Exception as e:
            logger.warning(f"[Analyze][FC] LLM 调用失败: {e}")
            break

        if resp.finish_reason != "tool_calls" or not resp.tool_calls:
            logger.info("[Analyze][FC] LLM 停止调用工具")
            break

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

        tool_results = []
        for tc in resp.tool_calls:
            if tc.name != "search_comments":
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps({"status": "unknown_tool"}),
                })
                continue

            note_url = tc.arguments.get("note_url", "")
            post = next((p for p in screened_items if p.get("note_url") == note_url), {})
            note_id = post.get("note_id", "")

            if note_id and note_id not in selected_ids:
                selected_ids.append(note_id)

            try:
                async with pool.borrow() as client:
                    await asyncio.sleep(random.uniform(0.8, 2.5))
                    comments = await _fetch_comments_with_retry(client, note_url, note_id)

                for c in comments:
                    c["note_id"] = note_id

                desc = post.get("desc", "").strip()
                if desc:
                    comments = [{
                        "comment_id": f"__post_body__{note_id}",
                        "content": desc,
                        "nickname": "[博主]",
                        "note_id": note_id,
                        "create_time": post.get("upload_time", "") or post.get("published_at", ""),
                    }] + comments

                valid_comments, filtered = _filter_invalid_comments(comments)
                all_new_comments.extend(valid_comments)
                total_filtered += filtered

                logger.info(f"[Analyze][FC] search_comments '{note_id}': {len(valid_comments)} 条有效评论")
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps({"status": "ok", "comment_count": len(valid_comments), "note_id": note_id}),
                })
            except Exception as e:
                logger.warning(f"[Analyze][FC] 爬取评论失败 {note_id}: {e}")
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps({"status": "error", "error": str(e)}),
                })

        messages.extend(tool_results)

    # 去重
    seen_ids = {c.get("comment_id") for c in existing_comments}
    unique_new = [c for c in all_new_comments if c.get("comment_id") not in seen_ids]
    total_count = len(existing_comments) + len(unique_new)

    # 检查关键错误：没有评论 且 没有帖子正文
    posts_with_desc = [p for p in screened_items if p.get("desc", "").strip()]
    has_post_body = len(posts_with_desc) > 0

    critical_errors = []
    abort_analysis = False
    if total_count == 0 and not has_post_body:
        critical_errors.append({
            "stage": "analyze",
            "error_type": "no_data",
            "message": f"有{len(screened_items)}篇帖子，但评论爬取全部失败，且帖子正文为空",
            "posts_count": len(screened_items),
            "posts_with_body": len(posts_with_desc),
        })
        abort_analysis = True
        logger.error(f"[Analyze][FC] 关键错误: 无法获取任何评论或正文")

    logger.info(f"[Analyze][FC] Round {round_num}: 新增 {len(unique_new)} 条评论，累计 {total_count} 条")

    return {
        "retrieved_comments": unique_new,
        "_raw_comments_for_clustering": all_new_comments,
        "_posts_to_fetch": selected_ids,
        "_analyze_round": round_num,
        "_fetched_comment_count": total_count,
        "_filtered_comment_count": state.get("_filtered_comment_count", 0) + total_filtered,
        "_critical_errors": critical_errors,
        "_abort_analysis": abort_analysis,
    }


async def node_cluster_opinions(state: GraphState, config: dict) -> dict[str, Any]:
    raw_comments = state.get("_raw_comments_for_clustering", [])
    screened_items = state.get("screened_items", [])
    existing_clusters = state.get("clusters", [])
    reuse_ratio = state.get("_reuse_ratio", 0.0)
    current_time = state.get("current_time", {}) or {}

    if reuse_ratio > 0.6 and existing_clusters:
        logger.info(f"[Analyze][ClusterOpinions] 复用记忆观点簇: reuse_ratio={reuse_ratio}")
        return {"clusters": existing_clusters}

    logger.info(f"[Analyze][ClusterOpinions] 输入评论数={len(raw_comments)}")

    if not raw_comments:
        return {"clusters": existing_clusters}

    evidence_registry = _build_evidence_registry(screened_items, raw_comments, current_time=current_time)
    comments_data = _evidence_for_prompt(evidence_registry)

    prompt = OPINION_PROMPT.format(
        query=state.get("user_query_raw", ""),
        comment_count=len(comments_data),
        all_comments_json=json.dumps(comments_data, ensure_ascii=False),
    )

    try:
        resp = await asyncio.wait_for(_llm.ainvoke(prompt), timeout=60.0)
        data = json.loads(resp.content)
        clusters = data.get("clusters", [])
        clusters = _assign_evidence_ids_to_clusters(clusters, evidence_registry)
        _attach_cluster_sources(clusters, evidence_registry)

        logger.info(f"[Analyze][ClusterOpinions] 输出 {len(clusters)} 个观点簇")

        if state.get("_enable_memory"):
            try:
                from app.utils.aspect_tagger import get_aspect_tagger
                tagger = get_aspect_tagger()
                intent = state.get("intent", "general")
                domain = "product" if intent in ["product_comparison", "quality_issue", "price_value"] else "general"
                clusters = await tagger.generate_tags(clusters, domain=domain)
            except Exception as e:
                logger.warning(f"[Analyze][ClusterOpinions] 三层标签生成失败: {e}")
                for cluster in clusters:
                    cluster.setdefault("primary_aspects", [])
                    cluster.setdefault("sub_aspects", [])
                    cluster.setdefault("synonym_aspects", [])

        return {"clusters": clusters, "evidence_registry": evidence_registry}

    except asyncio.TimeoutError:
        logger.warning("[Analyze][ClusterOpinions] 聚类超时 60 秒")
        return {"clusters": existing_clusters}
    except Exception as e:
        logger.warning(f"[Analyze][ClusterOpinions] 聚类失败：{e}")
        return {"clusters": existing_clusters}


async def node_validate_clusters(state: GraphState, config: dict) -> dict[str, Any]:
    clusters = state.get("clusters", [])
    if not clusters:
        return {"clusters": []}

    prompt = VALIDATE_CLUSTERS_PROMPT.format(
        intent=state.get("intent", "general"),
        key_aspects=json.dumps(state.get("key_aspects", []), ensure_ascii=False),
        user_needs="、".join(state.get("user_needs", [])) or "无",
        clusters_json=json.dumps(clusters, ensure_ascii=False),
    )

    try:
        resp = await asyncio.wait_for(_llm.ainvoke(prompt), timeout=30.0)
        data = json.loads(resp.content)
        validated_clusters = data.get("clusters", [])

        original_map = {cl.get("topic"): cl for cl in clusters}
        for vcl in validated_clusters:
            orig = original_map.get(vcl.get("topic"), {})
            vcl["source_note_url"] = orig.get("source_note_url", "")
            vcl["source_title"] = orig.get("source_title", "")
            vcl["primary_aspects"] = orig.get("primary_aspects", [])
            vcl["sub_aspects"] = orig.get("sub_aspects", [])
            vcl["synonym_aspects"] = orig.get("synonym_aspects", [])
            vcl["evidence_ids"] = [
                eid for eid in vcl.get("evidence_ids", orig.get("evidence_ids", []))
                if eid in set(orig.get("evidence_ids", []))
            ] or orig.get("evidence_ids", [])

        removed = len(clusters) - len(validated_clusters)
        logger.info(f"[Analyze][ValidateClusters] 保留 {len(validated_clusters)} 个，删除 {removed} 个不相关观点")

        return {
            "clusters": validated_clusters,
            "_need_refetch": len(validated_clusters) < 5,
        }

    except asyncio.TimeoutError:
        logger.warning("[Analyze][ValidateClusters] 验证超时 30 秒")
        return {"clusters": clusters}
    except Exception as e:
        logger.warning(f"[Analyze][ValidateClusters] 验证失败：{e}")
        return {"clusters": clusters}


def _has_conflicting_sentiment(clusters: list) -> bool:
    sentiments = {cl.get("sentiment", "中立") for cl in clusters}
    return "正面" in sentiments and "负面" in sentiments


def _build_evidence_buckets(
    registry: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    cluster_by_evidence: dict[str, list[dict[str, Any]]] = {}
    for idx, cluster in enumerate(clusters):
        cluster_id = f"cl_{idx}"
        for evidence_id in cluster.get("evidence_ids", []) or []:
            cluster_by_evidence.setdefault(evidence_id, []).append({
                "cluster_id": cluster_id,
                "topic": cluster.get("topic", ""),
                "sentiment": cluster.get("sentiment", "中立"),
            })

    rows = []
    for item in registry[:_MAX_EVIDENCE_FOR_TIME_ANALYSIS]:
        rows.append({
            **item,
            "_clusters": cluster_by_evidence.get(item["evidence_id"], []),
        })

    parseable = [item for item in rows if item.get("created_at")]
    if len(parseable) == len(rows) and rows:
        ordering_basis = "parsed_time"
    elif parseable:
        ordering_basis = "mixed_time_and_order"
    else:
        ordering_basis = "retrieval_order"

    rows.sort(key=lambda item: (item.get("created_at") or "9999-99-99", item.get("order", 9999)))
    if len(rows) < 3:
        return [], ordering_basis

    bucket_count = min(3, len(rows))
    labels = ["先出现", "随后扩展", "后续转向"]
    buckets = []
    for index in range(bucket_count):
        start = index * len(rows) // bucket_count
        end = (index + 1) * len(rows) // bucket_count
        chunk = rows[start:end]
        if not chunk:
            continue
        cluster_counts: dict[str, dict[str, Any]] = {}
        for item in chunk:
            for cluster in item.get("_clusters", []):
                current = cluster_counts.setdefault(cluster["cluster_id"], {**cluster, "count": 0})
                current["count"] += 1
        top_clusters = sorted(cluster_counts.values(), key=lambda item: item["count"], reverse=True)[:4]
        dated = [item.get("created_at") for item in chunk if item.get("created_at")]
        date_range = ""
        if dated:
            date_range = dated[0] if dated[0] == dated[-1] else f"{dated[0]} 至 {dated[-1]}"
        buckets.append({
            "label": labels[index],
            "date_range": date_range,
            "top_clusters": top_clusters,
            "representative_evidence": [
                {
                    "evidence_id": item["evidence_id"],
                    "quote": item.get("content", "")[:120],
                    "source_type": item.get("source_type", ""),
                    "created_at": item.get("created_at", ""),
                    "clusters": item.get("_clusters", [])[:3],
                }
                for item in chunk[:4]
            ],
        })
    return buckets, ordering_basis


def _empty_content_time_analysis(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "ordering_basis": "retrieval_order",
        "dominant_pattern": "无明显演化",
        "events": [],
        "limitations": [reason],
    }


def _sanitize_content_time_analysis(
    payload: dict[str, Any],
    *,
    allowed_cluster_ids: set[str],
    allowed_evidence_ids: set[str],
    ordering_basis: str,
) -> dict[str, Any]:
    available = bool(payload.get("available", True))
    dominant_pattern = str(payload.get("dominant_pattern") or "无明显演化")
    events = []
    for index, event in enumerate(payload.get("events", [])[:4]):
        if not isinstance(event, dict):
            continue
        text = " ".join(str(event.get(key, "")) for key in ("title", "summary", "sequence_label"))
        if any(term in text for term in _BANNED_CONTENT_TIME_TERMS):
            continue
        evidence_ids = [eid for eid in event.get("evidence_ids", []) if eid in allowed_evidence_ids]
        cluster_ids = [cid for cid in event.get("cluster_ids", []) if cid in allowed_cluster_ids]
        if not evidence_ids or not cluster_ids:
            continue
        events.append({
            "id": str(event.get("id") or f"cte_{index + 1}"),
            "order": int(event.get("order") or index + 1),
            "event_type": str(event.get("event_type") or "topic_shift"),
            "title": str(event.get("title") or "内容表达出现变化"),
            "sequence_label": str(event.get("sequence_label") or "随后扩展"),
            "summary": str(event.get("summary") or ""),
            "cluster_ids": cluster_ids,
            "evidence_ids": evidence_ids,
            "confidence": max(0.0, min(1.0, float(event.get("confidence", 0.6) or 0.6))),
        })
    limitations = [str(item) for item in payload.get("limitations", []) if item]
    if not events:
        available = False
        limitations = limitations or ["当前样本不足以支撑稳定的内容时间演化判断。"]
    return {
        "available": available,
        "ordering_basis": str(payload.get("ordering_basis") or ordering_basis),
        "dominant_pattern": dominant_pattern,
        "events": sorted(events, key=lambda item: item["order"]),
        "limitations": limitations,
    }


async def node_analyze_content_time(state: GraphState, config: dict) -> dict[str, Any]:
    temporal_context = state.get("temporal_context", {}) or {}
    if temporal_context.get("content_time_analysis") == "skip":
        return {"content_time_analysis": _empty_content_time_analysis("当前查询不要求内容时间分析。")}

    clusters = state.get("clusters", []) or []
    registry = state.get("evidence_registry", []) or []
    if len(clusters) < 2 or len(registry) < 3:
        return {"content_time_analysis": _empty_content_time_analysis("观点簇或证据数量不足，未生成内容时间演化。")}

    buckets, ordering_basis = _build_evidence_buckets(registry, clusters)
    if not buckets:
        return {"content_time_analysis": _empty_content_time_analysis("可排序证据不足，未生成内容时间演化。")}

    compact_clusters = [
        {
            "cluster_id": f"cl_{idx}",
            "topic": cluster.get("topic", ""),
            "sentiment": cluster.get("sentiment", "中立"),
            "count": cluster.get("count", 1),
            "evidence_ids": cluster.get("evidence_ids", []),
        }
        for idx, cluster in enumerate(clusters)
    ]
    prompt = CONTENT_TIME_ANALYSIS_PROMPT.format(
        query=state.get("user_query_raw", ""),
        temporal_context_json=json.dumps(temporal_context, ensure_ascii=False),
        clusters_json=json.dumps(compact_clusters, ensure_ascii=False),
        evidence_buckets_json=json.dumps(buckets, ensure_ascii=False),
    )
    allowed_cluster_ids = {row["cluster_id"] for row in compact_clusters}
    allowed_evidence_ids = {item["evidence_id"] for item in registry}
    try:
        resp = await asyncio.wait_for(_llm.ainvoke(prompt), timeout=30.0)
        payload = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", resp.content.strip()))
    except Exception as e:
        logger.warning(f"[Analyze][ContentTime] 生成失败，使用空结果: {e}")
        payload = _empty_content_time_analysis("内容时间分析生成失败，未强行推断。")

    analysis = _sanitize_content_time_analysis(
        payload,
        allowed_cluster_ids=allowed_cluster_ids,
        allowed_evidence_ids=allowed_evidence_ids,
        ordering_basis=ordering_basis,
    )
    logger.info(f"[Analyze][ContentTime] available={analysis.get('available')}, events={len(analysis.get('events', []))}")
    return {"content_time_analysis": analysis}


async def node_check_quality(state: GraphState) -> dict[str, Any]:
    # 如果已经标记完成（如 api_type=1 模式），直接返回
    if state.get("_analyze_done"):
        logger.info("[Analyze][CheckQuality] 已标记完成，跳过质量检查")
        return {
            "_analyze_done": True,
            "_need_refetch": False,
            "sentiment_summary": state.get("sentiment_summary", {}),
            "evidence_ledger": state.get("evidence_ledger", []),
        }

    comment_count = state.get("_fetched_comment_count", 0)
    clusters = state.get("clusters", [])
    round_num = state.get("_analyze_round", 0)
    unique_opinion_count = len(clusters)
    has_conflict = _has_conflicting_sentiment(clusters)

    should_stop = False
    stop_reason = ""

    if comment_count >= _TARGET_COMMENTS and has_conflict:
        should_stop = True
        stop_reason = "评论充足且有冲突观点"
    elif comment_count >= _MIN_COMMENTS and unique_opinion_count >= 5:
        should_stop = True
        stop_reason = f"观点簇足够 ({unique_opinion_count}个)"
    elif round_num >= _MAX_ANALYZE_ROUNDS:
        should_stop = True
        stop_reason = "达到最大轮次"
    elif len(state.get("_posts_to_fetch", [])) >= len(state.get("screened_items", [])):
        should_stop = True
        stop_reason = "无更多帖子可爬"

    if state.get("_need_refetch") and round_num < _MAX_ANALYZE_ROUNDS:
        should_stop = False
        stop_reason = "观点簇相关性不足，需要爬取更多帖子"

    sentiment_counts: dict[str, int] = {}
    for cl in clusters:
        s = cl.get("sentiment", "中立")
        sentiment_counts[s] = sentiment_counts.get(s, 0) + cl.get("count", 1)

    evidence_ledger = [
        {"topic": cl.get("topic"), "sentiment": cl.get("sentiment"),
         "quotes": cl.get("evidence_quotes", []), "source": cl.get("source_title", "")}
        for cl in clusters
    ]

    logger.info(
        f"[Analyze][CheckQuality] Round {round_num}: "
        f"评论={comment_count}, 观点簇={unique_opinion_count}, 冲突={has_conflict}, "
        f"停止={should_stop} ({stop_reason})"
    )

    return {
        "_analyze_done": should_stop,
        "_need_refetch": False,
        "sentiment_summary": sentiment_counts,
        "evidence_ledger": evidence_ledger,
    }


def _route_analyze(state: GraphState) -> Literal["fetch_comments_fc", "__end__"]:
    if state.get("_analyze_done"):
        return "__end__"
    logger.info(f"[Analyze][Route] 继续第 {state.get('_analyze_round', 0) + 1} 轮爬取")
    return "fetch_comments_fc"


async def node_error_report(state: GraphState) -> dict[str, Any]:
    """生成错误报告并终止分析。"""
    from app.agents.retrieve_agent import _generate_error_report

    critical_errors = state.get("_critical_errors", [])
    error_report = _generate_error_report(critical_errors)
    logger.info("[Analyze][ErrorReport] 生成错误报告，终止分析")

    return {
        "final_answer": error_report,
        "confidence_score": 0.0,
        "limitations": ["系统错误导致无法完成分析"],
        "_analyze_done": True,
    }


def _route_after_fetch_comments(state: GraphState) -> Literal["error_report", "cluster_opinions"]:
    """关键错误时直接跳到错误报告节点。"""
    if state.get("_abort_analysis"):
        return "error_report"
    return "cluster_opinions"


def build_analyze_graph():
    from langgraph.graph import StateGraph, END

    g = StateGraph(GraphState)
    g.add_node("fetch_comments_fc", node_fetch_comments_fc)
    g.add_node("error_report", node_error_report)
    g.add_node("cluster_opinions", node_cluster_opinions)
    g.add_node("validate_clusters", node_validate_clusters)
    g.add_node("analyze_content_time", node_analyze_content_time)
    g.add_node("check_quality", node_check_quality)

    g.set_entry_point("fetch_comments_fc")
    # 关键错误时跳到 error_report，否则继续聚类
    g.add_conditional_edges("fetch_comments_fc", _route_after_fetch_comments)
    g.add_edge("error_report", END)
    g.add_edge("cluster_opinions", "validate_clusters")
    g.add_edge("validate_clusters", "analyze_content_time")
    g.add_edge("analyze_content_time", "check_quality")
    g.add_conditional_edges("check_quality", _route_analyze)

    return g.compile()
