from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from app.tools.hot_topics import hot_topics_client
from app.tools.llm import create_llm


_PLATFORMS = ["douyin", "weibo", "toutiao", "baidu"]
_MIN_ITEMS = 12
_MAX_ITEMS = 20
_LAYOUT_CANDIDATES: list[tuple[int, int]] = [(4, 5), (4, 4), (3, 5), (3, 4)]
_MAX_CANDIDATES_FOR_LLM = 80
_COMPACT_LLM_CANDIDATES = 35

_CATEGORY_RULES: list[tuple[str, tuple[str, ...], int]] = [
    ("消费数码", ("iphone", "苹果", "华为", "小米", "手机", "ai", "deepseek", "gemini", "汽车", "新能源", "京东", "家电", "耳机", "电脑"), 90),
    ("娱乐人物", ("吴克群", "明星", "电影", "综艺", "演员", "歌手", "央视", "娱乐", "恋情", "代言", "剧", "演唱会"), 80),
    ("生活方式", ("旅游", "穿搭", "美妆", "护肤", "减肥", "外卖", "餐饮", "家居", "健康", "运动", "情感", "520"), 70),
    ("社会热点", ("官方", "辟谣", "政策", "学校", "彩礼", "暴雨", "高考", "就业", "医疗", "教育", "争议", "回应"), 65),
]
_DISPLAY_CATEGORY_ORDER = ["消费数码", "娱乐人物", "生活方式", "社会热点"]
_MIXED_GROUP_TITLES = ["热门趋势", "公众讨论", "热搜精选", "今日焦点"]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", "", str(title or "")).lower()


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        digits = re.sub(r"\D+", "", str(value or ""))
        return int(digits) if digits else 0


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        return json.loads(cleaned[start:end + 1])
    raise ValueError("LLM response does not contain a JSON object")


class HomeHotspotsService:
    """Homepage hotspot recommender, independent from the analysis workflow."""

    def __init__(self, base_dir: str | None = None):
        self._base_dir = Path(base_dir) if base_dir else Path(__file__).parent.parent.parent / "data" / "home_hotspots"
        self._history_dir = self._base_dir / "history"
        self._latest_file = self._base_dir / "latest.json"
        self._refresh_lock = asyncio.Lock()
        self._current: dict[str, Any] | None = None
        self._llm = None

    async def start_scheduler(self) -> None:
        """Refresh once on startup, then at 07:00 and 13:00 every day."""
        await self.refresh(source="startup")
        while True:
            delay = self._seconds_until_next_refresh()
            logger.info(f"[HomeHotspots] 下次刷新将在 {delay:.0f} 秒后触发")
            await asyncio.sleep(delay)
            await self.refresh(source="schedule")

    async def refresh(self, source: str = "manual") -> dict[str, Any]:
        async with self._refresh_lock:
            logger.info(f"[HomeHotspots] 开始刷新首页热搜: source={source}")
            try:
                candidates = await self._fetch_all_candidates()
                if not candidates:
                    logger.warning("[HomeHotspots] 热搜接口未返回候选，使用旧缓存")
                    return self._mark_latest_stale(reason="no_candidates")

                ranked_items, ranking_source = await self._rank_items(candidates)
                groups, layout = self._pack_display_groups(ranked_items)

                if self._count_items(groups) < _MIN_ITEMS and ranking_source != "rule_fallback":
                    logger.warning(
                        "[HomeHotspots] LLM 有效结果不足以构建展示布局，改用规则降级: "
                        f"items={len(ranked_items)}, groups={len(groups)}"
                    )
                    ranked_items = self._fallback_rank_items(candidates)
                    ranking_source = "rule_fallback"
                    groups, layout = self._pack_display_groups(ranked_items)

                if self._count_items(groups) < _MIN_ITEMS:
                    logger.warning(
                        "[HomeHotspots] 规则降级仍无法构建有效展示布局，使用旧缓存: "
                        f"ranked_items={len(ranked_items)}, raw={len(candidates)}"
                    )
                    return self._mark_latest_stale(reason="insufficient_display_items")

                payload = {
                    "updated_at": _now_iso(),
                    "source": source,
                    "stale": False,
                    "raw_count": len(candidates),
                    "ranking_source": ranking_source,
                    "layout": layout,
                    "groups": groups,
                }
                self._save_payload(payload, source)
                self._current = payload
                logger.info(
                    "[HomeHotspots] 刷新完成: "
                    f"ranking_source={ranking_source}, groups={len(groups)}, "
                    f"item_counts={layout['items_per_block']}, raw={len(candidates)}"
                )
                return payload
            except Exception as e:
                logger.warning(f"[HomeHotspots] 刷新失败: type={type(e).__name__}, error={e!r}")
                return self._mark_latest_stale(reason=type(e).__name__)

    def get_current(self) -> dict[str, Any]:
        if self._current:
            return self._current
        if self._latest_file.exists():
            try:
                with open(self._latest_file, "r", encoding="utf-8") as f:
                    self._current = json.load(f)
                return self._current
            except Exception as e:
                logger.warning(f"[HomeHotspots] 读取 latest.json 失败: {e}")
        return {
            "updated_at": "",
            "source": "empty",
            "stale": True,
            "raw_count": 0,
            "ranking_source": "empty",
            "layout": {"block_count": 0, "items_per_block": [], "items_per_group": 0, "target": "empty"},
            "groups": [],
        }

    async def _fetch_all_candidates(self) -> list[dict[str, Any]]:
        tasks = [hot_topics_client.fetch_trending(platform, limit=30) for platform in _PLATFORMS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        merged: dict[str, dict[str, Any]] = {}
        for platform, result in zip(_PLATFORMS, results):
            if isinstance(result, Exception):
                logger.warning(f"[HomeHotspots] 获取 {platform} 热搜失败: {result}")
                continue
            for raw in result:
                title = str(raw.get("title", "")).strip()
                if not title:
                    continue
                key = _normalize_title(title)
                hot_value = _safe_int(raw.get("hot_value") or raw.get("hot"))
                item = merged.setdefault(key, {
                    "title": title,
                    "platforms": [],
                    "hot_value": 0,
                    "source_items": [],
                })
                if platform not in item["platforms"]:
                    item["platforms"].append(platform)
                item["hot_value"] = max(item["hot_value"], hot_value)
                item["source_items"].append({"platform": platform, "hot_value": hot_value})

        candidates = list(merged.values())
        for item in candidates:
            category, category_weight = self._classify_title(item["title"])
            item["category"] = category
            item["score"] = self._score_candidate(item, category_weight)

        candidates.sort(key=self._candidate_sort_key, reverse=True)
        return candidates

    async def _rank_items(self, candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        phases = [
            ("primary", "llm", _MAX_CANDIDATES_FOR_LLM, False),
            ("compact_retry", "llm_retry", _COMPACT_LLM_CANDIDATES, True),
        ]
        last_error: Exception | None = None

        for phase, ranking_source, max_candidates, compact in phases:
            started = time.perf_counter()
            try:
                ranked_items = await self._rank_with_llm(
                    candidates,
                    max_candidates=max_candidates,
                    compact=compact,
                )
                elapsed = time.perf_counter() - started
                logger.info(
                    "[HomeHotspots] LLM 排序完成: "
                    f"phase={phase}, source={ranking_source}, elapsed={elapsed:.2f}s, "
                    f"candidates={min(len(candidates), max_candidates)}, valid_items={len(ranked_items)}"
                )
                if len(ranked_items) >= _MIN_ITEMS:
                    return ranked_items, ranking_source
                last_error = ValueError(f"valid_items={len(ranked_items)} < {_MIN_ITEMS}")
                self._log_ranking_failure(
                    last_error,
                    phase=phase,
                    elapsed=elapsed,
                    candidate_count=min(len(candidates), max_candidates),
                    fallback_path="llm_retry" if phase == "primary" else "rule_fallback",
                )
            except Exception as e:
                elapsed = time.perf_counter() - started
                last_error = e
                self._log_ranking_failure(
                    e,
                    phase=phase,
                    elapsed=elapsed,
                    candidate_count=min(len(candidates), max_candidates),
                    fallback_path="llm_retry" if phase == "primary" else "rule_fallback",
                )

        if last_error:
            logger.warning(
                "[HomeHotspots] LLM 排序不可用，使用规则降级: "
                f"type={type(last_error).__name__}, error={last_error!r}"
            )
        return self._fallback_rank_items(candidates), "rule_fallback"

    async def _rank_with_llm(
        self,
        candidates: list[dict[str, Any]],
        *,
        max_candidates: int = _MAX_CANDIDATES_FOR_LLM,
        compact: bool = False,
    ) -> list[dict[str, Any]]:
        if self._llm is None:
            model = (os.getenv("HOME_HOTSPOTS_LLM_MODEL") or "LongCat-2.0-Preview").strip()
            self._llm = create_llm(temperature=0.1, timeout=60.0, model=model)

        compact_candidates = [
            {
                "title": item["title"],
                "platforms": item["platforms"],
                "hot_value": item["hot_value"],
                "category_hint": item.get("category", "热门讨论"),
            }
            for item in candidates[:max_candidates]
        ]
        prompt = self._build_llm_prompt(compact_candidates, compact=compact)
        response = await self._llm.ainvoke(prompt)
        data = _extract_json_object(response.content)
        return self._validate_llm_ranked_items(data, candidates)

    def _build_llm_prompt(self, candidates: list[dict[str, Any]], *, compact: bool) -> str:
        if compact:
            return f"""
你是首页热搜推荐编辑。请从候选热搜中选出 12-16 条最适合“小红书舆情分析系统”点击分析的话题。
只允许使用候选 title，不要编造。避免重复、过度官方、缺少讨论空间的话题。
返回 JSON，格式为 {{"items":[{{"title":"候选原始标题","query":"标题 大家怎么看","category":"消费数码|娱乐人物|生活方式|社会热点|热门讨论"}}]}}。

候选 JSON：
{json.dumps(candidates, ensure_ascii=False)}
"""

        return f"""
你是一个首页热搜推荐编辑。请从候选热搜中选出最适合“小红书舆情分析系统”首页展示的 12-20 条。

选择标准：
1. 适合做小红书舆情、口碑、公众讨论分析。
2. 优先选择消费产品、科技数码、生活方式、娱乐人物、社会热点中有讨论价值的话题。
3. 避免重复、过度官方、缺少公众讨论空间、明显不适合用户点击分析的话题。
4. query 字段要适合直接触发分析，通常是“标题 大家怎么看”或“标题 真实评价”。
5. category 只能是：消费数码、娱乐人物、生活方式、社会热点、热门讨论。
6. 只能使用候选列表中的 title，不要编造新标题。

候选热搜 JSON：
{json.dumps(candidates, ensure_ascii=False)}

只返回 JSON：
{{
  "items": [
    {{
      "title": "候选中的原始标题",
      "query": "适合直接分析的查询",
      "category": "消费数码",
      "reason": "入选理由，20字以内"
    }}
  ]
}}
"""

    def _validate_llm_ranked_items(self, data: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidate_by_key = {_normalize_title(item["title"]): item for item in candidates}
        raw_items = self._extract_ranked_raw_items(data)
        used = set()
        ranked_items = []

        for index, raw_item in enumerate(raw_items[:_MAX_ITEMS]):
            title = str(raw_item.get("title", "")).strip()
            key = _normalize_title(title)
            if not key or key in used or key not in candidate_by_key:
                continue
            source_item = candidate_by_key[key]
            used.add(key)
            category = self._normalize_category(raw_item.get("category") or source_item.get("category"))
            ranked_items.append(self._format_ranked_item(
                source_item,
                query=raw_item.get("query"),
                reason=raw_item.get("reason") or "适合首页分析",
                category=category,
                score=float(source_item.get("score", 0)) + max(0, _MAX_ITEMS - index) * 10,
            ))

        return ranked_items

    def _extract_ranked_raw_items(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(data.get("items"), list):
            return [item for item in data["items"] if isinstance(item, dict)]

        raw_items: list[dict[str, Any]] = []
        for group in data.get("groups", []):
            if not isinstance(group, dict):
                continue
            group_title = group.get("title")
            for item in group.get("items", []):
                if not isinstance(item, dict):
                    continue
                if "category" not in item:
                    item = {**item, "category": group_title}
                raw_items.append(item)
        return raw_items

    def _fallback_rank_items(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sorted_candidates = sorted(candidates, key=self._candidate_sort_key, reverse=True)
        buckets: dict[str, list[dict[str, Any]]] = {category: [] for category in [*_DISPLAY_CATEGORY_ORDER, "热门讨论"]}
        for item in sorted_candidates:
            buckets.setdefault(self._normalize_category(item.get("category")), []).append(item)

        ranked: list[dict[str, Any]] = []
        used = set()
        while len(ranked) < min(_MAX_ITEMS, len(sorted_candidates)):
            progressed = False
            for category in [*_DISPLAY_CATEGORY_ORDER, "热门讨论"]:
                bucket = buckets.get(category, [])
                while bucket:
                    item = bucket.pop(0)
                    key = _normalize_title(item["title"])
                    if key in used:
                        continue
                    used.add(key)
                    ranked.append(self._format_ranked_item(
                        item,
                        query=None,
                        reason="热度较高，适合分析",
                        category=category,
                        score=float(item.get("score", 0)),
                    ))
                    progressed = True
                    break
                if len(ranked) >= _MAX_ITEMS:
                    break
            if not progressed:
                break

        for item in sorted_candidates:
            if len(ranked) >= _MAX_ITEMS:
                break
            key = _normalize_title(item["title"])
            if key in used:
                continue
            used.add(key)
            category = self._normalize_category(item.get("category"))
            ranked.append(self._format_ranked_item(
                item,
                query=None,
                reason="热度较高，适合分析",
                category=category,
                score=float(item.get("score", 0)),
            ))
        return ranked

    def _pack_display_groups(self, ranked_items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        for group_count, items_per_group in _LAYOUT_CANDIDATES:
            groups = self._pack_for_layout(ranked_items, group_count, items_per_group)
            if groups:
                return groups, self._layout_for_groups(groups, items_per_group=items_per_group)
        return [], {"block_count": 0, "items_per_block": [], "items_per_group": 0, "target": "empty"}

    def _pack_for_layout(
        self,
        ranked_items: list[dict[str, Any]],
        group_count: int,
        items_per_group: int,
    ) -> list[dict[str, Any]]:
        items = self._dedupe_ranked_items(ranked_items)
        if len(items) < group_count * items_per_group:
            return []

        used: set[str] = set()
        used_titles: set[str] = set()
        groups: list[dict[str, Any]] = []

        for category in _DISPLAY_CATEGORY_ORDER:
            if len(groups) >= group_count:
                break
            category_items = [
                item for item in items
                if self._normalize_category(item.get("category")) == category
                and _normalize_title(item.get("title", "")) not in used
            ]
            while len(category_items) >= items_per_group and len(groups) < group_count:
                chunk = category_items[:items_per_group]
                self._mark_used(chunk, used)
                groups.append({
                    "title": self._unique_group_title(category, used_titles),
                    "items": [self._display_item(item) for item in chunk],
                })
                category_items = category_items[items_per_group:]

        mixed_title_index = 0
        while len(groups) < group_count:
            pool = [item for item in items if _normalize_title(item.get("title", "")) not in used]
            if len(pool) < items_per_group:
                break
            chunk = pool[:items_per_group]
            self._mark_used(chunk, used)
            title = self._mixed_group_title(chunk, mixed_title_index)
            mixed_title_index += 1
            groups.append({
                "title": self._unique_group_title(title, used_titles),
                "items": [self._display_item(item) for item in chunk],
            })

        if len(groups) != group_count:
            return []
        if any(len(group["items"]) != items_per_group for group in groups):
            return []
        return groups

    def _format_ranked_item(
        self,
        item: dict[str, Any],
        *,
        query: Any,
        reason: Any,
        category: str,
        score: float,
    ) -> dict[str, Any]:
        title = str(item.get("title", "")).strip()
        return {
            "title": title,
            "query": str(query or f"{title} 大家怎么看").strip(),
            "platforms": list(item.get("platforms") or []),
            "hot_value": _safe_int(item.get("hot_value")),
            "reason": str(reason or "适合首页分析").strip()[:40],
            "category": self._normalize_category(category),
            "score": score,
        }

    def _display_item(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": item["title"],
            "query": item["query"],
            "platforms": item.get("platforms", []),
            "hot_value": item.get("hot_value", 0),
            "reason": item.get("reason", ""),
            "category": self._normalize_category(item.get("category")),
        }

    def _dedupe_ranked_items(self, ranked_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        used = set()
        deduped = []
        for item in sorted(ranked_items, key=lambda x: float(x.get("score", 0)), reverse=True):
            title = str(item.get("title", "")).strip()
            key = _normalize_title(title)
            if not key or key in used:
                continue
            used.add(key)
            deduped.append(item)
        return deduped

    def _score_candidate(self, item: dict[str, Any], category_weight: int) -> float:
        hot_value = _safe_int(item.get("hot_value"))
        platform_score = len(item.get("platforms", [])) * 1000
        hot_score = min(math.log10(max(hot_value, 1)) * 100, 800)
        title = str(item.get("title", "")).lower()
        low_discussion_penalty = 120 if any(word in title for word in ("天气", "股价", "汇率", "欢迎宴会")) else 0
        return platform_score + hot_score + category_weight - low_discussion_penalty

    def _candidate_sort_key(self, item: dict[str, Any]) -> tuple[float, int, int]:
        return (
            float(item.get("score", 0)),
            len(item.get("platforms", [])),
            _safe_int(item.get("hot_value")),
        )

    def _classify_title(self, title: str) -> tuple[str, int]:
        title_norm = str(title or "").lower()
        for category, keywords, weight in _CATEGORY_RULES:
            if any(keyword.lower() in title_norm for keyword in keywords):
                return category, weight
        return "热门讨论", 55

    def _normalize_category(self, category: Any) -> str:
        text = str(category or "").strip()
        if text in [*_DISPLAY_CATEGORY_ORDER, "热门讨论"]:
            return text
        for canonical in _DISPLAY_CATEGORY_ORDER:
            if canonical in text:
                return canonical
        if any(word in text for word in ("热点", "社会", "公众", "讨论", "趋势", "焦点")):
            return "热门讨论"
        return "热门讨论"

    def _mixed_group_title(self, items: list[dict[str, Any]], index: int) -> str:
        counts: dict[str, int] = {}
        for item in items:
            category = self._normalize_category(item.get("category"))
            counts[category] = counts.get(category, 0) + 1
        dominant, count = max(counts.items(), key=lambda x: x[1])
        if dominant != "热门讨论" and count >= 3:
            return dominant
        return _MIXED_GROUP_TITLES[index % len(_MIXED_GROUP_TITLES)]

    def _unique_group_title(self, title: str, used_titles: set[str]) -> str:
        if title not in used_titles:
            used_titles.add(title)
            return title
        for fallback in _MIXED_GROUP_TITLES:
            if fallback not in used_titles:
                used_titles.add(fallback)
                return fallback
        used_titles.add(title)
        return title

    def _mark_used(self, items: list[dict[str, Any]], used: set[str]) -> None:
        for item in items:
            used.add(_normalize_title(item.get("title", "")))

    def _layout_for_groups(self, groups: list[dict[str, Any]], *, items_per_group: int | None = None) -> dict[str, Any]:
        counts = [len(group.get("items", [])) for group in groups]
        block_count = len(groups)
        if items_per_group is None:
            items_per_group = counts[0] if counts and len(set(counts)) == 1 else 0
        if block_count == 0:
            target = "empty"
        elif items_per_group:
            target = f"{block_count}x{items_per_group}"
        else:
            target = f"{block_count}-balanced"
        return {
            "block_count": block_count,
            "items_per_block": counts,
            "items_per_group": items_per_group,
            "target": target,
        }

    def _log_ranking_failure(
        self,
        error: Exception,
        *,
        phase: str,
        elapsed: float,
        candidate_count: int,
        fallback_path: str,
    ) -> None:
        provider, model = self._llm_identity()
        logger.warning(
            "[HomeHotspots] LLM 排序失败: "
            f"type={type(error).__name__}, error={error!r}, elapsed={elapsed:.2f}s, "
            f"phase={phase}, provider={provider}, model={model}, "
            f"candidates={candidate_count}, fallback={fallback_path}"
        )

    def _llm_identity(self) -> tuple[str, str]:
        provider = os.getenv("LLM_PROVIDER", "qianfan").strip().lower()
        model = getattr(self._llm, "model", "") if self._llm is not None else ""
        if not model:
            env_name = {
                "longcat": "LONGCAT_MODEL",
                "modelscope": "MODELSCOPE_MODEL",
                "qianfan": "QIANFAN_MODEL",
            }.get(provider, "QIANFAN_MODEL")
            model = os.getenv(env_name, "")
        return provider, model or "unknown"

    def _save_payload(self, payload: dict[str, Any], source: str) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._history_dir.mkdir(parents=True, exist_ok=True)
        with open(self._latest_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        history_file = self._history_dir / f"{stamp}_{source}.json"
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _mark_latest_stale(self, reason: str) -> dict[str, Any]:
        payload = self.get_current()
        payload = {
            **payload,
            "stale": True,
            "stale_reason": reason,
            "ranking_source": "stale_cache",
        }
        if "layout" not in payload:
            groups = payload.get("groups", [])
            payload["layout"] = self._layout_for_groups(groups if isinstance(groups, list) else [])
        self._current = payload
        return payload

    def _seconds_until_next_refresh(self) -> float:
        now = datetime.now()
        candidates = [
            now.replace(hour=7, minute=0, second=0, microsecond=0),
            now.replace(hour=13, minute=0, second=0, microsecond=0),
        ]
        future = next((item for item in candidates if item > now), None)
        if future is None:
            future = (now + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
        return max(1.0, (future - now).total_seconds())

    def _count_items(self, groups: list[dict[str, Any]]) -> int:
        return sum(len(group.get("items", [])) for group in groups)


home_hotspots_service = HomeHotspotsService()
