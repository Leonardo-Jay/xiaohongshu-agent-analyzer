"""Screen Subgraph — 筛选 ReAct Agent
职责：接收 orchestrator 的意图分析结果（intent、key_aspects、user_needs），
     对 retrieve 检索到的帖子进行内容相关性筛选，过滤广告/软广，输出 5~7 篇最相关的帖子。

核心流程：
  1. Pre Filter: 规则预过滤，过滤明显广告（硬广、品牌号、联系方式）
  2. Detect Ads: LLM 检测软广，使用压缩后的帖子摘要（省 token）
  3. Rank and Select: 基于相关性评分排序，选择 top 5~7 篇

压缩策略（Token 优化）：
  - 帖子正文：前 100 字 + 结尾 100 字（而非全文）
  - 单篇帖子输入从平均 800 字降至约 200 字（省 75%+ token）

循环终止条件：
  - 筛选出 5~7 篇帖子
  - 或已达到 2 轮筛选上限（第 2 轮放宽条件）
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Literal

from loguru import logger

from app.models.schemas import GraphState
from app.prompts.templates import SCREEN_AD_DETECT_PROMPT, SCREEN_RELEVANCE_PROMPT
from app.tools.llm import create_llm

_llm_ad_detect = create_llm(temperature=0)
_llm_relevance = create_llm(temperature=0)

_MAX_SCREEN_ROUNDS = 2  # 最多 2 轮筛选
_MIN_POSTS = 5  # 最少输出帖子数
_MAX_POSTS = 7  # 最多输出帖子数

# 广告关键词列表（硬广检测）
AD_KEYWORDS = [
    "购买", "下单", "私信我", "加 V", "加微信", "微信号", "公众号", "淘宝", "微店",
    "折扣码", "优惠券", "限时优惠", "秒杀", "拼团", "代购", "包邮",
    "点击链接", "淘口令", "扫码", "二维码",
]

# 联系方式模式
CONTACT_PATTERNS = [
    r"V[：:]\s*\w+",
    r"微信 [：:]\s*\w+",
    r"Q[Q 号] [：:]\s*\d+",
    r"公众号 [：:]\s*\w+",
    r"微博 [：:]\s*\w+",
]


def _has_ad_keywords(text: str) -> bool:
    """检查文本是否包含广告关键词。"""
    text_lower = text.lower()
    return any(kw in text_lower for kw in AD_KEYWORDS)


def _has_contact_info(text: str) -> bool:
    """检查文本是否包含联系方式。"""
    for pattern in CONTACT_PATTERNS:
        if re.search(pattern, text):
            return True
    return False


def _compress_post(post: dict[str, Any]) -> dict[str, Any]:
    """压缩帖子数据，提取关键字段（省 token）。

    压缩策略：
    - 标题：最多 50 字
    - 正文：前 100 字 + 结尾 100 字（而非全文）
    - 标签：最多 5 个
    - 互动数据：保留赞/评/藏
    """
    desc = post.get("desc", "")
    if len(desc) > 200:
        # 前 100 字 + 结尾 100 字
        desc_preview = desc[:100] + "..." + desc[-100:]
    else:
        desc_preview = desc

    return {
        "note_id": post.get("note_id", ""),
        "title": (post.get("title", "") or "")[:50],
        "desc_preview": desc_preview,
        "tags": (post.get("tags") or [])[:5],
        "note_type": post.get("note_type", ""),
        "engagement": {
            "like": post.get("like_count", 0),
            "comment": post.get("comment_count", 0),
            "collect": post.get("collect_count", 0),
        },
        "user": post.get("user", {}),
    }


def _parse_json_response(text: str) -> dict[str, Any]:
    """清理并解析 LLM 返回的 JSON 结果。"""
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


async def node_pre_filter(state: GraphState) -> dict[str, Any]:
    """Pre Filter 节点：规则预过滤

    功能：
      - 过滤含广告关键词的帖子
      - 过滤品牌号帖子
      - 过滤含联系方式的帖子
      - 输出：通过初筛的帖子列表（带压缩数据）
    """
    posts = state.get("retrieved_posts", [])
    passed = []
    rejected_ad = []
    rejected_brand = []
    rejected_contact = []

    for post in posts:
        title = post.get("title", "")
        desc = post.get("desc", "")
        user = post.get("user", {})

        # 检查广告关键词
        if _has_ad_keywords(title) or _has_ad_keywords(desc[:200]):
            rejected_ad.append(post.get("note_id", ""))
            continue

        # 检查品牌号
        if user.get("level") == "品牌号" or user.get("is_brand", False):
            rejected_brand.append(post.get("note_id", ""))
            continue

        # 检查联系方式
        if _has_contact_info(desc[:200]):
            rejected_contact.append(post.get("note_id", ""))
            continue

        # 通过初筛，附加压缩数据
        post["_compressed"] = _compress_post(post)
        passed.append(post)

    round_num = state.get("_screen_round", 0) + 1

    logger.info(
        f"[Screen][PreFilter] Round {round_num}: "
        f"total={len(posts)}, passed={len(passed)}, "
        f"rejected_ad={len(rejected_ad)}, rejected_brand={len(rejected_brand)}, "
        f"rejected_contact={len(rejected_contact)}"
    )

    return {
        "retrieved_posts": posts,  # 保持原数据不变
        "_screen_round": round_num,
        "_pre_filter_passed": passed,
        "_pre_filter_stats": {
            "total": len(posts),
            "passed": len(passed),
            "rejected_ad": len(rejected_ad),
            "rejected_brand": len(rejected_brand),
            "rejected_contact": len(rejected_contact),
        },
    }


async def node_detect_ads(state: GraphState) -> dict[str, Any]:
    """Detect Ads 节点：LLM 检测软广

    功能：
      - 对初筛通过的帖子，使用压缩后的摘要检测软广
      - 批量处理：一次 LLM 调用判断一篇帖子
      - 输出：标记 is_hard_ad、is_soft_ad 的帖子列表
    """
    passed_posts = state.get("_pre_filter_passed", [])

    if not passed_posts:
        logger.warning("[Screen][DetectAds] 初筛无帖子")
        return {"_ad_detect_passed": [], "_screen_done": True}

    ad_detected = []
    genuine_posts = []

    sem = asyncio.Semaphore(5)

    async def _process_ad(post: dict[str, Any]):
        compressed = post.get("_compressed", _compress_post(post))

        prompt = SCREEN_AD_DETECT_PROMPT.format(
            title=compressed.get("title", ""),
            desc_preview=compressed.get("desc_preview", ""),
            tags="、".join([t.get("name", "") if isinstance(t, dict) else str(t) for t in compressed.get("tags", [])]),
            like=compressed.get("engagement", {}).get("like", 0),
            comment=compressed.get("engagement", {}).get("comment", 0),
            collect=compressed.get("engagement", {}).get("collect", 0),
        )

        try:
            async with sem:
                resp = await _llm_ad_detect.ainvoke(prompt)
            data = _parse_json_response(resp.content)

            is_hard_ad = data.get("is_hard_ad", False)
            is_soft_ad = data.get("is_soft_ad", False)
            is_genuine = data.get("is_genuine_share", not is_hard_ad and not is_soft_ad)

            # 附加检测结果
            post["_ad_detect"] = {
                "is_hard_ad": is_hard_ad,
                "is_soft_ad": is_soft_ad,
                "is_genuine_share": is_genuine,
                "confidence": data.get("confidence", 0.0),
                "reason": data.get("reason", ""),
            }

            if is_hard_ad or is_soft_ad:
                ad_detected.append(post.get("note_id", ""))
            else:
                genuine_posts.append(post)

        except Exception as e:
            logger.warning(f"[Screen][DetectAds] LLM failed for post {post.get('note_id')}: {e}")
            # LLM 失败时默认通过
            genuine_posts.append(post)

    await asyncio.gather(*[_process_ad(post) for post in passed_posts])

    logger.info(
        f"[Screen][DetectAds] passed={len(passed_posts)}, "
        f"ad_detected={len(ad_detected)}, genuine={len(genuine_posts)}"
    )

    return {
        "_ad_detect_passed": genuine_posts,
        "_ad_detect_stats": {
            "total": len(passed_posts),
            "ad_detected": len(ad_detected),
            "genuine": len(genuine_posts),
        },
    }


async def node_rank_and_select(state: GraphState) -> dict[str, Any]:
    """Rank and Select 节点：相关性排序与选择

    功能：
      - 基于 orchestrator 的 key_aspects、user_needs、intent 评估相关性
      - 按相关性评分排序，选择 top 8~10 篇
      - 如果通过的帖子<8 篇，降级放宽条件（允许部分软广）
      - 记忆复用模式：根据 reuse_ratio 减少筛选目标数量
    """
    query = state.get("user_query_raw", "")
    intent = state.get("intent", "general")
    key_aspects = state.get("key_aspects", [])
    user_needs = state.get("user_needs", [])

    genuine_posts = state.get("_ad_detect_passed", [])
    all_posts_after_filter = state.get("_pre_filter_passed", [])

    # 获取记忆复用参数
    reuse_ratio = state.get("_reuse_ratio", 0.0)

    # 动态调整目标数量（记忆复用时减少）
    if reuse_ratio > 0.3:
        target_min = max(5, int(_MIN_POSTS * (1 - reuse_ratio * 0.5)))
        target_max = max(6, int(_MAX_POSTS * (1 - reuse_ratio * 0.5)))
        logger.info(f"[Screen][RankSelect] 记忆复用模式: reuse_ratio={reuse_ratio}, 目标 {target_min}-{target_max} 篇")
    else:
        target_min = _MIN_POSTS
        target_max = _MAX_POSTS

    # 获取各阶段统计信息
    pre_filter_stats = state.get("_pre_filter_stats", {})
    ad_detect_stats = state.get("_ad_detect_stats", {})

    round_num = state.get("_screen_round", 0)

    # 如果真实分享 >= 目标最小值，只用真实分享；否则放宽条件
    if len(genuine_posts) >= target_min:
        candidate_posts = genuine_posts
        放宽条件 = False
    else:
        # 放宽条件：允许软广帖子参与评选
        candidate_posts = all_posts_after_filter
        放宽条件 = True
        logger.info(f"[Screen][RankSelect] 放宽条件：genuine={len(genuine_posts)} < {target_min}")

    if not candidate_posts:
        logger.warning("[Screen][RankSelect] 无候选帖子")
        return {
            "screened_items": [],
            "screening_stats": {
                "total": 0,
                "passed": 0,
                "rejected_ad": 0,
                "rejected_brand": 0,
                "rejected_contact": 0,
                "rejected_low_relevance": 0,
            },
            "_screen_done": True,
        }

    # 对每篇帖子进行相关性评分
    scored_posts = []

    # 构建 key_aspects 和 user_needs 的字符串描述
    aspects_str = "、".join([a.get("aspect", "") for a in key_aspects]) if key_aspects else "无"
    needs_str = "、".join(user_needs) if user_needs else "无"

    sem = asyncio.Semaphore(8)

    async def _process_relevance(post: dict[str, Any]):
        compressed = post.get("_compressed", _compress_post(post))

        prompt = SCREEN_RELEVANCE_PROMPT.format(
            query=query,
            intent=intent,
            key_aspects=aspects_str,
            user_needs=needs_str,
            title=compressed.get("title", ""),
            desc_preview=compressed.get("desc_preview", ""),
            tags="、".join([t.get("name", "") if isinstance(t, dict) else str(t) for t in compressed.get("tags", [])]),
        )

        try:
            async with sem:
                resp = await _llm_relevance.ainvoke(prompt)
            data = _parse_json_response(resp.content)

            score = float(data.get("relevance_score", 0.5))
            matched_aspects = data.get("matched_aspects", [])
            reason = data.get("reason", "")

            post["_relevance"] = {
                "score": score,
                "matched_aspects": matched_aspects,
                "reason": reason,
            }
            scored_posts.append((score, post))

        except Exception as e:
            logger.warning(f"[Screen][RankSelect] LLM failed for post {post.get('note_id')}: {e}")
            # LLM 失败时给默认分 0.5
            post["_relevance"] = {"score": 0.5, "matched_aspects": [], "reason": "LLM 失败，默认分"}
            scored_posts.append((0.5, post))

    await asyncio.gather(*[_process_relevance(post) for post in candidate_posts])

    # 按相关性评分降序排序
    scored_posts.sort(key=lambda x: x[0], reverse=True)

    # 选择 top 篇（使用动态目标）
    selected_count = min(len(scored_posts), target_max)
    # 如果不足目标最小值，全部选中
    selected_count = max(selected_count, min(len(scored_posts), target_min))

    selected = [post for score, post in scored_posts[:selected_count]]
    rejected_by_relevance = len(candidate_posts) - len(selected)

    # 清理临时字段（不输出到最终结果）
    for post in selected:
        post.pop("_compressed", None)
        post.pop("_ad_detect", None)
        post.pop("_relevance", None)

    # 合并各阶段统计信息
    rejected_ad = pre_filter_stats.get("rejected_ad", 0) + ad_detect_stats.get("ad_detected", 0)
    rejected_brand = pre_filter_stats.get("rejected_brand", 0)
    rejected_contact = pre_filter_stats.get("rejected_contact", 0)

    logger.info(
        f"[Screen][RankSelect] Round {round_num}: "
        f"候选={len(candidate_posts)}, 选中={len(selected)}, "
        f"排除：广告={rejected_ad}, 品牌号={rejected_brand}, 联系方式={rejected_contact}, 相关性不足={rejected_by_relevance}"
    )

    return {
        "screened_items": selected,
        "screening_stats": {
            "total": pre_filter_stats.get("total", len(candidate_posts)),
            "passed": len(selected),
            "rejected_ad": rejected_ad,
            "rejected_brand": rejected_brand,
            "rejected_contact": rejected_contact,
            "rejected_low_relevance": rejected_by_relevance,
        },
        "_screen_done": True,
    }


def _route_screen(state: GraphState) -> Literal["__end__"]:
    """Screen 子图总是结束（流水线结构，非循环）"""
    return "__end__"


def build_screen_graph():
    """构建 Screen 子图（三阶段流水线）

    完整流程：
      pre_filter -> detect_ads -> rank_and_select
    """
    from langgraph.graph import StateGraph

    g = StateGraph(GraphState)

    # 添加所有节点
    g.add_node("pre_filter", node_pre_filter)
    g.add_node("detect_ads", node_detect_ads)
    g.add_node("rank_and_select", node_rank_and_select)

    # 设置入口点
    g.set_entry_point("pre_filter")

    # 设置边连接
    g.add_edge("pre_filter", "detect_ads")
    g.add_edge("detect_ads", "rank_and_select")
    g.add_edge("rank_and_select", "__end__")

    return g.compile()
