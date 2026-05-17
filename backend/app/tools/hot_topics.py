"""意图 Agent 外部工具客户端 — 调用 apihz.cn 接口，提供热搜、百度相关搜索、百度百科。"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from loguru import logger

import os


class HotTopicsClient:
    BASE_URL = "https://cn.apihz.cn/api/xinwen"
    PLATFORMS = {
        "douyin": "douyin.php",
        "weibo": "weibo.php",
        "toutiao": "toutiao.php",
        "baidu": "baidu.php",
    }
    CACHE_TTL = 300  # 5 分钟缓存

    def __init__(self, api_id: str | None = None, api_key: str | None = None) -> None:
        self._api_id = api_id or os.getenv("APIHZ_ID", "")
        self._api_key = api_key or os.getenv("APIHZ_KEY", "")
        self._cache: dict[str, tuple[float, Any]] = {}

    # ── 通用 API 请求 ────────────────────────────────────────────────

    async def _fetch_api(self, url: str, params: dict, method: str = "GET") -> dict | None:
        """通用 apihz API 请求，失败返回 None。"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if method == "POST":
                    resp = await client.post(url, data=params)
                else:
                    resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            if data.get("code") != 200:
                logger.warning(f"[ApiHzClient] API 返回错误: url={url}, msg={data.get('msg', 'unknown')}")
                return None
            return data
        except Exception as e:
            logger.warning(f"[ApiHzClient] 请求异常: url={url}, error={e}")
            return None

    # ── 热搜接口 ────────────────────────────────────────────────────

    # ── 公共接口 ──────────────────────────────────────────────────────

    async def fetch_trending(self, platform: str, limit: int = 20) -> list[dict]:
        """获取指定平台的实时热搜榜单（归一化后）。"""
        if platform not in self.PLATFORMS:
            logger.warning(f"[HotTopics] 不支持的平台: {platform}")
            return []

        cache_key = f"trending:{platform}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached[:limit]

        raw_list = await self._fetch_platform(platform)
        normalized = [self._normalize_item(item, platform) for item in raw_list]

        self._set_cache(cache_key, normalized)
        return normalized[:limit]

    async def search_topics(self, keyword: str, platform: str = "all") -> dict[str, Any]:
        """搜索与关键词相关的热搜话题。"""
        if platform == "all":
            platforms = list(self.PLATFORMS.keys())
        elif platform in self.PLATFORMS:
            platforms = [platform]
        else:
            return {"matched_topics": [], "total_matched": 0, "search_keyword": keyword}

        # 并发请求各平台
        tasks = [self._get_or_fetch(p) for p in platforms]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 聚合所有平台的标题
        all_items: list[dict] = []
        for p, result in zip(platforms, results):
            if isinstance(result, Exception):
                logger.warning(f"[HotTopics] {p} 请求失败: {result}")
                continue
            all_items.extend(result)

        # 模糊匹配
        matched = self._fuzzy_match(keyword, all_items)

        return {
            "matched_topics": matched,
            "total_matched": len(matched),
            "search_keyword": keyword,
        }

    # ── 内部方法 ──────────────────────────────────────────────────────

    async def _get_or_fetch(self, platform: str) -> list[dict]:
        """带缓存的单平台获取。"""
        cache_key = f"trending:{platform}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        raw_list = await self._fetch_platform(platform)
        normalized = [self._normalize_item(item, platform) for item in raw_list]
        self._set_cache(cache_key, normalized)
        return normalized

    async def _fetch_platform(self, platform: str) -> list[dict]:
        """请求单个平台的热搜 API，失败返回空列表。"""
        endpoint = self.PLATFORMS[platform]
        url = f"{self.BASE_URL}/{endpoint}"
        params = {"id": self._api_id, "key": self._api_key}
        data = await self._fetch_api(url, params)
        return data.get("data", []) if data else []

    def _normalize_item(self, raw: dict, platform: str) -> dict:
        """将各平台返回的字段归一化为统一格式。"""
        title = raw.get("title", "")
        # 热度值：不同平台字段名不同
        hot_value = (
            raw.get("hot_value")
            or raw.get("hot")
            or self._extract_hot_from_desc(raw.get("desc_extr", ""))
            or 0
        )
        try:
            hot_value = int(hot_value)
        except (ValueError, TypeError):
            hot_value = 0

        return {
            "title": title,
            "hot_value": hot_value,
            "platform": platform,
        }

    @staticmethod
    def _extract_hot_from_desc(desc_extr: str) -> int:
        """从微博的 desc_extr 字段中提取热度数字。"""
        if not desc_extr:
            return 0
        import re
        m = re.search(r"(\d+)", str(desc_extr))
        return int(m.group(1)) if m else 0

    def _fuzzy_match(self, keyword: str, items: list[dict]) -> list[dict]:
        """对热搜标题做关键词匹配，优先子串命中，其次拆词匹配。"""
        keyword_lower = keyword.lower()
        # 按空格拆分关键词（处理 "deepseek 融资" 这类输入）
        keyword_parts = [p.lower() for p in keyword.split() if len(p) > 1]
        # 对中文关键词，按 2-gram 拆分（处理 "deepseek融资" 无法按空格拆的情况）
        if len(keyword_parts) <= 1 and len(keyword_lower) > 2:
            bigrams = [keyword_lower[i:i+2] for i in range(len(keyword_lower) - 1)]
            keyword_parts = [p for p in bigrams if any(c.isalpha() or '一' <= c <= '鿿' for c in p)]

        scored: list[tuple[float, dict]] = []
        for item in items:
            title = item.get("title", "")
            title_lower = title.lower()
            score = 0.0

            # 1. 完整关键词子串匹配（最高优先级）
            if keyword_lower in title_lower:
                score = 1.0
            # 2. 拆分后逐个匹配
            elif keyword_parts:
                matched = sum(1 for p in keyword_parts if p in title_lower)
                if matched > 0:
                    score = matched / len(keyword_parts) * 0.8
            # 3. 逐字符匹配（对纯中文关键词有效）
            if score == 0.0:
                chars = [c for c in keyword_lower if c.strip() and ('一' <= c <= '鿿' or c.isalpha())]
                if chars:
                    overlap = sum(1 for c in chars if c in title_lower)
                    if overlap > 0:
                        score = overlap / len(chars) * 0.5

            if score >= 0.2:
                relevance = "high" if score >= 0.6 else "medium"
                scored_item = {
                    **item,
                    "rank": len(scored) + 1,
                    "relevance": relevance,
                }
                scored.append((score, scored_item))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored]

    # ── 百度相关搜索 ────────────────────────────────────────────────

    async def search_baidu_related(self, words: str) -> dict[str, Any]:
        """查询百度相关搜索推荐词。"""
        cache_key = f"baidu_related:{words}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return {"related_searches": cached, "source": "baidu", "keyword": words}

        url = "https://cn.apihz.cn/api/wangzhan/soubaiduxg.php"
        params = {
            "id": self._api_id,
            "key": self._api_key,
            "words": words,
            "tn": "98010089_dg",
            "ck": "",
        }
        data = await self._fetch_api(url, params)
        if not data:
            return {"related_searches": [], "source": "baidu", "keyword": words}

        related = data.get("datas", [])
        self._set_cache(cache_key, related)
        return {"related_searches": related, "source": "baidu", "keyword": words}

    # ── 百度百科 ────────────────────────────────────────────────────

    async def search_baidu_baike(self, words: str) -> dict[str, Any]:
        """查询百度百科摘要。"""
        cache_key = f"baike:{words}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return {"summary": cached, "source": "baidu_baike", "keyword": words}

        url = "https://cn.apihz.cn/api/zici/baikebaidu.php"
        params = {
            "id": self._api_id,
            "key": self._api_key,
            "words": words,
            "ck": "",
        }

        # 重试 2 次（文档说"如果失败可多次尝试，系统会自动切换通道"）
        data = None
        for attempt in range(2):
            data = await self._fetch_api(url, params, method="POST")
            if data:
                break
            if attempt < 1:
                logger.info(f"[ApiHzClient] baikebaidu 重试 {attempt + 1}/2, words={words}")

        if not data:
            return {"summary": "", "source": "baidu_baike", "keyword": words}

        summary = data.get("msg", "")
        self._set_cache(cache_key, summary)
        return {"summary": summary, "source": "baidu_baike", "keyword": words}

    # ── 缓存 ──────────────────────────────────────────────────────────

    def _get_cache(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, data = entry
        if time.time() - ts > self.CACHE_TTL:
            del self._cache[key]
            return None
        return data

    def _set_cache(self, key: str, data: Any) -> None:
        self._cache[key] = (time.time(), data)


# 全局单例
hot_topics_client = HotTopicsClient()
