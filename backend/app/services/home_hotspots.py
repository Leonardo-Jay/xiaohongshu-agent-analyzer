from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from app.tools.hot_topics import hot_topics_client
from app.tools.llm import create_llm


_PLATFORMS = ["douyin", "weibo", "toutiao", "baidu"]
_MIN_ITEMS = 12
_MAX_ITEMS = 20
_MAX_CANDIDATES_FOR_LLM = 80


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", "", str(title or "")).lower()


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

                try:
                    groups = await self._rank_with_llm(candidates)
                except Exception as e:
                    logger.warning(f"[HomeHotspots] LLM 排序失败，使用规则降级: {e}")
                    groups = self._fallback_groups(candidates)

                if self._count_items(groups) < _MIN_ITEMS:
                    groups = self._fallback_groups(candidates)

                payload = {
                    "updated_at": _now_iso(),
                    "source": source,
                    "stale": False,
                    "raw_count": len(candidates),
                    "groups": groups,
                }
                self._save_payload(payload, source)
                self._current = payload
                logger.info(
                    f"[HomeHotspots] 刷新完成: groups={len(groups)}, "
                    f"items={self._count_items(groups)}, raw={len(candidates)}"
                )
                return payload
            except Exception as e:
                logger.warning(f"[HomeHotspots] 刷新失败: {e}")
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
                hot_value = int(raw.get("hot_value") or 0)
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
        candidates.sort(key=lambda x: (len(x["platforms"]), x["hot_value"]), reverse=True)
        return candidates

    async def _rank_with_llm(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self._llm is None:
            self._llm = create_llm(temperature=0.1, timeout=60.0)

        compact_candidates = [
            {
                "title": item["title"],
                "platforms": item["platforms"],
                "hot_value": item["hot_value"],
            }
            for item in candidates[:_MAX_CANDIDATES_FOR_LLM]
        ]
        prompt = f"""
你是一个首页热搜推荐编辑。请从候选热搜中选出最适合“小红书舆情分析系统”首页展示的 12-20 条。

选择标准：
1. 适合做小红书舆情/口碑/公众讨论分析。
2. 优先选择消费产品、科技数码、生活方式、娱乐人物、社会热点中有讨论价值的话题。
3. 避免重复、过度官方、缺少公众讨论空间、明显不适合用户点击分析的话题。
4. 分成 3-4 个分组，每组 4-5 条。
5. query 字段要适合直接触发分析，通常是“标题 大家怎么看”或“标题 真实评价”。
6. 只能使用候选列表中的 title，不要编造新标题。

候选热搜 JSON：
{json.dumps(compact_candidates, ensure_ascii=False)}

只返回 JSON：
{{
  "groups": [
    {{
      "title": "分组名，2-6个字",
      "items": [
        {{
          "title": "候选中的原始标题",
          "query": "适合直接分析的查询",
          "reason": "入选理由，20字以内"
        }}
      ]
    }}
  ]
}}
"""
        response = await self._llm.ainvoke(prompt)
        data = _extract_json_object(response.content)
        return self._validate_llm_groups(data.get("groups", []), candidates)

    def _validate_llm_groups(self, groups: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidate_by_key = {_normalize_title(item["title"]): item for item in candidates}
        used = set()
        validated_groups = []

        for group in groups[:4]:
            items = []
            for raw_item in group.get("items", [])[:5]:
                title = str(raw_item.get("title", "")).strip()
                key = _normalize_title(title)
                if not key or key in used or key not in candidate_by_key:
                    continue
                source_item = candidate_by_key[key]
                used.add(key)
                items.append({
                    "title": source_item["title"],
                    "query": str(raw_item.get("query") or f"{source_item['title']} 大家怎么看").strip(),
                    "platforms": source_item["platforms"],
                    "hot_value": source_item["hot_value"],
                    "reason": str(raw_item.get("reason", "")).strip(),
                })
            if items:
                validated_groups.append({
                    "title": str(group.get("title") or "热点").strip()[:12],
                    "items": items,
                })

        return validated_groups

    def _fallback_groups(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets = [
            ("科技消费", ("iphone", "苹果", "华为", "小米", "手机", "ai", "deepseek", "gemini", "汽车", "京东", "新能源")),
            ("娱乐人物", ("吴克群", "明星", "电影", "综艺", "演员", "歌手", "央视", "娱乐")),
            ("生活方式", ("旅游", "穿搭", "美妆", "护肤", "减肥", "外卖", "餐饮", "家居", "健康")),
            ("热门讨论", ()),
        ]
        used = set()
        groups = []

        for group_title, keywords in buckets:
            group_items = []
            for item in candidates:
                key = _normalize_title(item["title"])
                if key in used:
                    continue
                title_norm = item["title"].lower()
                if keywords and not any(keyword.lower() in title_norm for keyword in keywords):
                    continue
                group_items.append(self._format_fallback_item(item))
                used.add(key)
                if len(group_items) >= 5:
                    break
            if group_items:
                groups.append({"title": group_title, "items": group_items})

        for item in candidates:
            if self._count_items(groups) >= 16:
                break
            key = _normalize_title(item["title"])
            if key in used:
                continue
            if not groups or len(groups[-1]["items"]) >= 5:
                groups.append({"title": "热门讨论", "items": []})
            groups[-1]["items"].append(self._format_fallback_item(item))
            used.add(key)

        return groups[:4]

    def _format_fallback_item(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": item["title"],
            "query": f"{item['title']} 大家怎么看",
            "platforms": item["platforms"],
            "hot_value": item["hot_value"],
            "reason": "热度较高，适合分析",
        }

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
        }
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
