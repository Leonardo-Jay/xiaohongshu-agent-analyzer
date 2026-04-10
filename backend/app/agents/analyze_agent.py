"""Analyze Subgraph — 评论拉取与观点聚类 ReAct Agent
职责：接收 Screen 子图输出的筛选结果（screened_items），爬取评论并进行观点聚类、情感分析。
     支持 ReAct 循环，当观点过少时自动爬取更多帖子。

核心流程：
  1. Select Posts: 按评论数 + 相关性加权选择本轮要爬取的帖子（top-3 优先）
  2. Fetch Comments: 并发爬取评论（30 秒总超时），过滤无效评论
  3. Cluster Opinions: 对所有爬取的评论进行观点聚类（LLM，60 秒超时）
  4. Validate Clusters: 验证观点簇与意图的相关性（LLM，30 秒超时），删除不相关观点
  5. Check Quality: 质量检查（规则），决定是否需要继续爬取

循环终止条件：
  - 评论总数 >= 30 条 且 有冲突观点（正负面都有）→ 结束
  - 评论总数 >= 15 条 且 观点簇 >= 3 个 → 结束
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
from app.prompts.templates import OPINION_PROMPT, VALIDATE_CLUSTERS_PROMPT
from app.tools.llm import create_llm
from app.tools.mcp_client import XhsMcpClientPool

_llm = create_llm(temperature=0)

_MAX_ANALYZE_ROUNDS = 2  # 最多 2 轮 ReAct 循环
_MIN_COMMENTS = 10  # 最少评论数（触发终止条件）
_TARGET_COMMENTS = 20  # 目标评论数
_TOP_POSTS_PER_ROUND = 3  # 每轮爬取评论的帖子数


def _is_valid_comment(content: str) -> bool:
    """检查评论是否有效（非纯 emoji、非无意义内容）。

    过滤规则：
    1. 长度检查：至少 2 个字符
    2. 纯 emoji 检测：去掉 emoji 后无内容
    3. 纯标点检测：去掉标点后无内容
    4. 无意义重复：如"啊啊啊""哈哈哈"（同一字符重复 3 次以上）
    """
    # 1. 长度检查
    if len(content) < 2:
        return False

    # 2. 纯 emoji 检测
    emoji_pattern = re.compile(
        r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF'
         r'\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF]'
    )
    text_only = emoji_pattern.sub('', content)
    if not text_only.strip():
        return False

    # 3. 纯标点检测（只保留中文、英文、数字）
    text_only = re.sub(r'[^\w\u4e00-\u9fff]', '', content)
    if not text_only.strip():
        return False

    # 4. 无意义重复检测（同一字符重复 3 次以上且总字符种类少于 3 种）
    if re.search(r'(.)\1{2,}', content) and len(set(content)) < 3:
        return False

    return True


def _filter_invalid_comments(comments: list[dict]) -> tuple[list[dict], int]:
    """过滤无效评论，返回 (有效评论列表, 过滤数量)。"""
    valid = []
    for c in comments:
        content = c.get("content", "")
        if _is_valid_comment(content):
            valid.append(c)
    filtered_count = len(comments) - len(valid)
    return valid, filtered_count


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
    return []


async def node_select_posts(state: GraphState) -> dict[str, Any]:
    """Select Posts 节点：选择本轮要爬取评论的帖子

    功能：
      - 从 screened_items 中按评论数 + 相关性加权选择 top-N 帖子
      - 第 1 轮：选择评论数最多的 top-3
      - 第 2 轮：选择剩余的帖子（如果第 1 轮观点不足）
      - 记忆复用模式：根据 reuse_ratio 减少爬取数量

    选择公式：
      score = comment_count * 0.7 + relevance_score * 100 * 0.3
      （评论数权重 70%，相关性权重 30%）
    """
    screened_items = state.get("screened_items", [])
    posts_to_fetch = state.get("_posts_to_fetch", [])
    round_num = state.get("_analyze_round", 0) + 1

    # 获取记忆复用参数
    reuse_ratio = state.get("_reuse_ratio", 0.0)

    if not screened_items:
        logger.warning("[Analyze][SelectPosts] 无可供分析的帖子")
        return {
            "_analyze_round": round_num,
            "_posts_to_fetch": [],
            "_analyze_done": True,
        }

    # 排除已爬取过的帖子
    fetched_ids = set(posts_to_fetch)
    remaining_posts = [p for p in screened_items if p.get("note_id") not in fetched_ids]

    if not remaining_posts:
        logger.info("[Analyze][SelectPosts] 所有帖子已爬取完毕")
        return {
            "_analyze_round": round_num,
            "_posts_to_fetch": [],
            "_analyze_done": True,
        }

    # 计算加权分数并排序
    def calc_score(post: dict) -> float:
        comment_count = int(post.get("comment_count") or 0)
        relevance_score = float(post.get("relevance_score") or 0.5)
        return comment_count * 0.7 + relevance_score * 100 * 0.3

    remaining_posts.sort(key=calc_score, reverse=True)

    # 计算本轮要爬取的数量（记忆复用时减少）
    base_num = _TOP_POSTS_PER_ROUND
    if reuse_ratio > 0.3:
        # 复用率高时减少爬取数量
        reduce_factor = min(0.7, reuse_ratio * 0.8)
        base_num = max(2, int(base_num * (1 - reduce_factor)))
        logger.info(f"[Analyze][SelectPosts] 记忆复用模式: reuse_ratio={reuse_ratio}, 减少爬取到 {base_num} 篇")

    num_to_fetch = min(len(remaining_posts), base_num)
    selected = remaining_posts[:num_to_fetch]
    selected_ids = [p.get("note_id") for p in selected]

    logger.info(
        f"[Analyze][SelectPosts] Round {round_num}: "
        f"剩余 {len(remaining_posts)} 篇，选择 {num_to_fetch} 篇：{selected_ids}"
    )

    return {
        "_analyze_round": round_num,
        "_posts_to_fetch": posts_to_fetch + selected_ids,
    }


async def node_fetch_comments(state: GraphState, config: dict) -> dict[str, Any]:
    """Fetch Comments 节点：爬取评论（30 秒总超时）

    功能：
      - 从 _posts_to_fetch 获取本轮要爬取的帖子
      - 从 screened_items 中找到对应的帖子详情
      - 使用连接池并发爬取评论（30 秒超时）
      - 过滤无效评论（纯 emoji、无意义内容）
      - 将帖子正文作为博主评论插入
      - 保存原始评论到 _raw_comments_for_clustering，供后续聚类节点使用

    注意：此节点不做 LLM 聚类，只负责爬取评论
    """
    posts_to_fetch_ids = state.get("_posts_to_fetch", [])
    screened_items = state.get("screened_items", [])
    existing_comments = state.get("retrieved_comments", [])
    existing_filtered_count = state.get("_filtered_comment_count", 0)

    # 找到本轮要爬取的帖子详情
    id_to_post = {p.get("note_id"): p for p in screened_items}
    target_posts = [id_to_post[nid] for nid in posts_to_fetch_ids if nid in id_to_post]

    if not target_posts:
        logger.warning("[Analyze][FetchComments] 未找到要爬取的帖子")
        return {
            "retrieved_comments": existing_comments,
            "_raw_comments_for_clustering": state.get("_raw_comments_for_clustering", []),
            "_fetched_comment_count": len(existing_comments),
            "_filtered_comment_count": existing_filtered_count,
        }

    # 从 config 获取连接池
    pool: XhsMcpClientPool = config.get("configurable", {}).get("pool")

    if not pool:
        logger.warning("[Analyze][FetchComments] 连接池未找到")
        return {
            "retrieved_comments": existing_comments,
            "_raw_comments_for_clustering": state.get("_raw_comments_for_clustering", []),
            "_fetched_comment_count": len(existing_comments),
            "_filtered_comment_count": existing_filtered_count,
        }

    # 用于累积过滤统计
    total_filtered_this_round = 0

    async def _process_post(post: dict) -> tuple[list[dict], int]:
        """爬取单篇帖子的评论（不做聚类），返回 (评论列表, 过滤数量)。"""
        nonlocal total_filtered_this_round
        note_id = post.get("note_id", "")
        note_url = post.get("note_url", "")

        try:
            async with pool.borrow() as client:
                await asyncio.sleep(random.uniform(0.8, 2.5))
                comments = await _fetch_comments_with_retry(client, note_url, note_id)
        except Exception as e:
            logger.warning(f"[Analyze] 借用客户端失败 {note_id}: {e}")
            comments = []

        # 为每条评论添加 note_id 标记（用于聚类时分组）
        for c in comments:
            c["note_id"] = note_id

        # 将帖子正文作为博主评论插入
        desc = post.get("desc", "").strip()
        if desc:
            synthetic = {
                "comment_id": f"__post_body__{note_id}",
                "content": desc,
                "nickname": "[博主]",
                "note_id": note_id,
            }
            comments = [synthetic] + comments

        # 过滤无效评论，获取过滤统计
        valid_comments, filtered = _filter_invalid_comments(comments)
        return valid_comments, filtered

    # 30 秒总超时控制 —— 使用 asyncio.wait 兼容 Python 3.10
    all_new_comments: list[dict] = []

    # 为每个帖子创建独立的任务
    tasks = [asyncio.create_task(_process_post(p)) for p in target_posts]

    if tasks:
        done_tasks, pending_tasks = await asyncio.wait(
            tasks,
            timeout=30.0,
            return_when=asyncio.ALL_COMPLETED
        )

        for task in done_tasks:
            try:
                result, filtered = task.result()
                all_new_comments.extend(result)
                total_filtered_this_round += filtered
            except Exception as e:
                logger.warning(f"[Analyze][FetchComments] 处理已完成任务时遇到异常：{e}")

        if pending_tasks:
            logger.warning(f"[Analyze][FetchComments] 30 秒超时，保留已完成评论。取消 {len(pending_tasks)} 个未完成任务。")
            for p_task in pending_tasks:
                p_task.cancel()

    # 去重评论
    seen_ids = {c.get("comment_id") for c in existing_comments}
    unique_new_comments = []
    for c in all_new_comments:
        cid = c.get("comment_id")
        if cid and cid not in seen_ids:
            seen_ids.add(cid)
            unique_new_comments.append(c)

    total_count = len(existing_comments) + len(unique_new_comments)
    total_filtered = existing_filtered_count + total_filtered_this_round

    if total_filtered_this_round > 0:
        logger.info(
            f"[Analyze][FetchComments] 获取 {len(unique_new_comments)} 条新评论，"
            f"累计 {total_count} 条，本轮过滤 {total_filtered_this_round} 条无效评论"
        )
    else:
        logger.info(
            f"[Analyze][FetchComments] 获取 {len(unique_new_comments)} 条新评论，累计 {total_count} 条"
        )

    return {
        "retrieved_comments": existing_comments + unique_new_comments,
        "_raw_comments_for_clustering": all_new_comments,
        "_fetched_comment_count": total_count,
        "_filtered_comment_count": total_filtered,
    }


async def node_cluster_opinions(state: GraphState, config: dict) -> dict[str, Any]:
    """Cluster Opinions 节点：批量聚类所有评论（60 秒超时）

    改动：
      - 一次性处理所有评论，不按 note 分组
      - 强制要求输出 7~14 个观点簇
      - 超时从 90 秒降至 60 秒
      - 支持记忆复用：reuse_ratio > 0.6 时直接复用观点簇
      - 为观点簇生成 aspect_tags（用于精确匹配检索）
    """
    raw_comments = state.get("_raw_comments_for_clustering", [])
    screened_items = state.get("screened_items", [])
    existing_clusters = state.get("clusters", [])

    # 获取记忆复用参数
    reuse_ratio = state.get("_reuse_ratio", 0.0)
    memory_context = state.get("memory_context", "")

    # 高复用率时直接复用记忆中的观点簇
    if reuse_ratio > 0.6 and existing_clusters:
        logger.info(f"[Analyze][ClusterOpinions] 复用记忆观点簇: reuse_ratio={reuse_ratio}, 数量={len(existing_clusters)}")
        return {"clusters": existing_clusters}

    logger.info(f"[Analyze][ClusterOpinions] 输入评论数={len(raw_comments)}")

    if not raw_comments:
        logger.info("[Analyze][ClusterOpinions] 无评论需要聚类")
        return {"clusters": existing_clusters}

    # 构建 note_id 到帖子信息的映射（用于后续补充 source 字段）
    id_to_post = {p.get("note_id"): p for p in screened_items}

    # 准备评论数据（最多 200 条，避免 token 超限）
    comments_data = [
        {
            "content": c.get("content", ""),
            "like_count": c.get("like_count", 0),
            "nickname": c.get("nickname", ""),
            "note_id": c.get("note_id", "")
        }
        for c in raw_comments[:200]
    ]

    # 构建全局聚类 Prompt
    prompt = OPINION_PROMPT.format(
        query=state.get("user_query_raw", ""),
        comment_count=len(comments_data),
        all_comments_json=json.dumps(comments_data, ensure_ascii=False)
    )

    try:
        # 60 秒超时
        resp = await asyncio.wait_for(_llm.ainvoke(prompt), timeout=60.0)
        data = json.loads(resp.content)
        clusters = data.get("clusters", [])

        # 为每个 cluster 补充 source_note_url 和 source_title（从该簇评论的 note_id 推断）
        # 由于是批量聚类，LLM 可能不知道具体来源，我们从评论数据中恢复
        for cl in clusters:
            # 尝试从该簇的 quotes 反推来源 note_id
            # 简化处理：直接使用第一个有 note_id 的评论作为来源
            for c in comments_data:
                if c.get("note_id"):
                    post = id_to_post.get(c["note_id"], {})
                    cl["source_note_url"] = post.get("note_url", "")
                    cl["source_title"] = post.get("title", "无标题")
                    break

        logger.info(f"[Analyze][ClusterOpinions] 输出 {len(clusters)} 个观点簇")

        # 为观点簇生成三层标签（主标签、子标签、同义标签）
        try:
            from app.utils.aspect_tagger import get_aspect_tagger
            tagger = get_aspect_tagger()

            # 判断领域类型（根据 intent）
            intent = state.get("intent", "general")
            domain = "product" if intent in ["product_comparison", "quality_issue", "price_value"] else "general"

            # 批量生成三层标签
            clusters = await tagger.generate_tags(clusters, domain=domain)
            logger.info(f"[Analyze][ClusterOpinions] 为 {len(clusters)} 个观点簇生成三层标签")
        except Exception as e:
            logger.warning(f"[Analyze][ClusterOpinions] 三层标签生成失败: {e}")
            # 降级：添加空标签，不影响主流程
            for cluster in clusters:
                cluster.setdefault("primary_aspects", [])
                cluster.setdefault("sub_aspects", [])
                cluster.setdefault("synonym_aspects", [])

        return {"clusters": clusters}

    except asyncio.TimeoutError:
        logger.warning("[Analyze][ClusterOpinions] 聚类超时 60 秒")
        return {"clusters": existing_clusters}
    except Exception as e:
        logger.warning(f"[Analyze][ClusterOpinions] 聚类失败：{e}")
        return {"clusters": existing_clusters}


async def node_validate_clusters(state: GraphState, config: dict) -> dict[str, Any]:
    """Validate Clusters 节点：验证观点簇与意图的相关性（30 秒超时）

    功能：
      - 调用 LLM 判断每个观点簇与 intent、key_aspects、user_needs 的相关性
      - 删除相关性分数 < 0.4 的观点簇
      - 如果删除后观点簇数量 < 5，标记需要重新爬取
    """
    clusters = state.get("clusters", [])
    intent = state.get("intent", "general")
    key_aspects = state.get("key_aspects", [])
    user_needs = state.get("user_needs", [])

    if not clusters:
        logger.info("[Analyze][ValidateClusters] 无观点簇需要验证")
        return {"clusters": []}

    # 构建验证 Prompt
    prompt = VALIDATE_CLUSTERS_PROMPT.format(
        intent=intent,
        key_aspects=json.dumps(key_aspects, ensure_ascii=False),
        user_needs="、".join(user_needs) if user_needs else "无",
        clusters_json=json.dumps(clusters, ensure_ascii=False)
    )

    try:
        # 30 秒超时
        resp = await asyncio.wait_for(_llm.ainvoke(prompt), timeout=30.0)
        data = json.loads(resp.content)
        validated_clusters = data.get("clusters", [])

        # 恢复 source 字段和标签字段（根据 topic 匹配原始观点簇）
        original_clusters_map = {cl.get("topic"): cl for cl in clusters}
        for validated_cl in validated_clusters:
            topic = validated_cl.get("topic")
            if topic in original_clusters_map:
                original_cl = original_clusters_map[topic]
                # 恢复 source 字段
                validated_cl["source_note_url"] = original_cl.get("source_note_url", "")
                validated_cl["source_title"] = original_cl.get("source_title", "")
                # 恢复标签字段
                validated_cl["primary_aspects"] = original_cl.get("primary_aspects", [])
                validated_cl["sub_aspects"] = original_cl.get("sub_aspects", [])
                validated_cl["synonym_aspects"] = original_cl.get("synonym_aspects", [])

        # 统计过滤结果
        original_count = len(clusters)
        filtered_count = len(validated_clusters)
        removed_count = original_count - filtered_count

        logger.info(
            f"[Analyze][ValidateClusters] 验证完成: "
            f"原始 {original_count} 个观点簇，保留 {filtered_count} 个，删除 {removed_count} 个不相关观点"
        )

        # 如果观点簇数量 < 5，标记需要重新爬取
        need_refetch = filtered_count < 5

        return {
            "clusters": validated_clusters,
            "_need_refetch": need_refetch,
        }

    except asyncio.TimeoutError:
        logger.warning("[Analyze][ValidateClusters] 验证超时 30 秒，保留原观点簇")
        return {"clusters": clusters}
    except Exception as e:
        logger.warning(f"[Analyze][ValidateClusters] 验证失败：{e}，保留原观点簇")
        return {"clusters": clusters}


def _has_conflicting_sentiment(clusters: list) -> bool:
    """检查是否有冲突观点（正负面都有）。"""
    sentiments = {cl.get("sentiment", "中立") for cl in clusters}
    return "正面" in sentiments and "负面" in sentiments


async def node_check_quality(state: GraphState) -> dict[str, Any]:
    """Check Quality 节点：质量检查与终止判断

    功能：
      - 规则评估评论数量和质量（不调用 LLM）
      - 判断是否达到终止条件
      - 如果未终止，标记继续循环

    终止条件：
      1. 评论总数 >= 30 条 且 有冲突观点 → 结束
      2. 评论总数 >= 15 条 且 观点簇 >= 3 个 → 结束
      3. 已达到 2 轮循环上限 → 结束
    """
    comment_count = state.get("_fetched_comment_count", 0)
    clusters = state.get("clusters", [])
    round_num = state.get("_analyze_round", 0)

    # 计算质量指标
    unique_opinion_count = len(clusters)
    has_conflict = _has_conflicting_sentiment(clusters)

    # 终止条件判断
    should_stop = False
    stop_reason = ""

    # 条件 1：评论足够多且有冲突观点
    if comment_count >= _TARGET_COMMENTS and has_conflict:
        should_stop = True
        stop_reason = "评论充足且有冲突观点"

    # 条件 2：观点簇足够
    elif comment_count >= _MIN_COMMENTS and unique_opinion_count >= 3:
        should_stop = True
        stop_reason = f"观点簇足够 ({unique_opinion_count}个)"

    # 条件 3：达到轮次上限
    elif round_num >= _MAX_ANALYZE_ROUNDS:
        should_stop = True
        stop_reason = "达到最大轮次"

    # 条件 4：已无帖子可爬
    elif not state.get("screened_items") or len(state.get("_posts_to_fetch", [])) >= len(state.get("screened_items", [])):
        should_stop = True
        stop_reason = "无更多帖子可爬"

    # 条件 5：观点簇相关性不足，需要重新爬取
    if state.get("_need_refetch") and round_num < _MAX_ANALYZE_ROUNDS:
        should_stop = False
        stop_reason = "观点簇相关性不足，需要爬取更多帖子"

    # 汇总情感
    sentiment_counts: dict[str, int] = {}
    for cl in clusters:
        s = cl.get("sentiment", "中立")
        sentiment_counts[s] = sentiment_counts.get(s, 0) + cl.get("count", 1)

    # 构建证据清单
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
        "sentiment_summary": sentiment_counts,
        "evidence_ledger": evidence_ledger,
    }


def _route_analyze(state: GraphState) -> Literal["__end__"]:
    """Analyze 子图总是结束（流水线结构，非循环）

    注意：ReAct 循环逻辑已在 check_quality 节点内部处理，
    通过 _analyze_done 字段控制是否继续。
    此处固定返回结束，由外部主图决定是否重入。
    """
    return "__end__"


def build_analyze_graph():
    """构建 Analyze 子图（五节点流水线）

    完整流程：
      select_posts → fetch_comments → cluster_opinions → validate_clusters → check_quality
    """
    from langgraph.graph import StateGraph

    g = StateGraph(GraphState)

    # 添加所有节点
    g.add_node("select_posts", node_select_posts)
    g.add_node("fetch_comments", node_fetch_comments)
    g.add_node("cluster_opinions", node_cluster_opinions)
    g.add_node("validate_clusters", node_validate_clusters)
    g.add_node("check_quality", node_check_quality)

    # 设置入口点
    g.set_entry_point("select_posts")

    # 设置边连接
    g.add_edge("select_posts", "fetch_comments")
    g.add_edge("fetch_comments", "cluster_opinions")
    g.add_edge("cluster_opinions", "validate_clusters")
    g.add_edge("validate_clusters", "check_quality")
    g.add_edge("check_quality", "__end__")

    return g.compile()
