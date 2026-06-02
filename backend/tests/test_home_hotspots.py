import asyncio
import json

from app.services.home_hotspots import HomeHotspotsService


def run_async(coro):
    return asyncio.run(coro)


class FakeLLMResponse:
    def __init__(self, content):
        self.content = content


class SequenceLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.model = "fake-hotspot-model"

    async def ainvoke(self, prompt):
        self.calls += 1
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeLLMResponse(result)


def make_service(tmp_path):
    return HomeHotspotsService(base_dir=str(tmp_path / "home_hotspots"))


def make_candidates(service, count, prefix="iPhone 热搜"):
    candidates = []
    for index in range(count):
        title = f"{prefix} {index}"
        category, category_weight = service._classify_title(title)
        item = {
            "title": title,
            "platforms": ["weibo"] if index % 2 else ["weibo", "douyin"],
            "hot_value": 1000000 - index * 1000,
            "source_items": [],
            "category": category,
        }
        item["score"] = service._score_candidate(item, category_weight)
        candidates.append(item)
    candidates.sort(key=service._candidate_sort_key, reverse=True)
    return candidates


def llm_items(candidates, count):
    return json.dumps({
        "items": [
            {
                "title": item["title"],
                "query": f"{item['title']} 大家怎么看",
                "category": item["category"],
                "reason": "适合分析",
            }
            for item in candidates[:count]
        ]
    }, ensure_ascii=False)


def test_primary_llm_timeout_uses_compact_retry(tmp_path):
    service = make_service(tmp_path)
    candidates = make_candidates(service, 18)
    service._llm = SequenceLLM([
        TimeoutError(),
        llm_items(candidates, 12),
    ])

    ranked_items, ranking_source = run_async(service._rank_items(candidates))

    assert ranking_source == "llm_retry"
    assert service._llm.calls == 2
    assert len(ranked_items) == 12


def test_home_hotspots_uses_longcat_preview_model_by_default(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    candidates = make_candidates(service, 12)
    captured = {}

    class CapturingLLM:
        model = "captured"

        async def ainvoke(self, prompt):
            return FakeLLMResponse(llm_items(candidates, 12))

    def fake_create_llm(**kwargs):
        captured.update(kwargs)
        return CapturingLLM()

    monkeypatch.delenv("HOME_HOTSPOTS_LLM_MODEL", raising=False)
    monkeypatch.setattr("app.services.home_hotspots.create_llm", fake_create_llm)

    ranked_items = run_async(service._rank_with_llm(candidates))

    assert captured["model"] == "LongCat-2.0-Preview"
    assert len(ranked_items) == 12


def test_invalid_llm_json_falls_back_to_rule_ranking(tmp_path):
    service = make_service(tmp_path)
    candidates = make_candidates(service, 18)
    service._llm = SequenceLLM(["not json", "still not json"])

    ranked_items, ranking_source = run_async(service._rank_items(candidates))

    assert ranking_source == "rule_fallback"
    assert service._llm.calls == 2
    assert len(ranked_items) >= 16
    assert all(item["query"].endswith("大家怎么看") for item in ranked_items)


def assert_layout(layout, target, counts):
    assert layout["target"] == target
    assert layout["block_count"] == len(counts)
    assert layout["items_per_block"] == counts
    assert layout["items_per_group"] == (counts[0] if counts else 0)


def test_display_packer_prefers_four_groups_with_five_items(tmp_path):
    service = make_service(tmp_path)
    ranked_items = service._fallback_rank_items(make_candidates(service, 20, prefix="Hot topic"))

    groups, layout = service._pack_display_groups(ranked_items)

    assert len(groups) == 4
    assert_layout(layout, "4x5", [5, 5, 5, 5])
    assert all(len(group["items"]) == 5 for group in groups)


def test_display_packer_degrades_to_four_groups_with_four_items(tmp_path):
    service = make_service(tmp_path)
    ranked_items = service._fallback_rank_items(make_candidates(service, 18))

    groups, layout = service._pack_display_groups(ranked_items)

    assert len(groups) == 4
    assert_layout(layout, "4x4", [4, 4, 4, 4])


def test_display_packer_degrades_to_three_groups_with_five_items(tmp_path):
    service = make_service(tmp_path)
    ranked_items = service._fallback_rank_items(make_candidates(service, 15, prefix="Trend topic"))

    groups, layout = service._pack_display_groups(ranked_items)

    assert len(groups) == 3
    assert_layout(layout, "3x5", [5, 5, 5])


def test_display_packer_degrades_to_three_groups_with_four_items(tmp_path):
    service = make_service(tmp_path)
    ranked_items = service._fallback_rank_items(make_candidates(service, 12))

    groups, layout = service._pack_display_groups(ranked_items)

    assert len(groups) == 3
    assert_layout(layout, "3x4", [4, 4, 4])


def test_sparse_category_items_are_merged_into_healthy_groups(tmp_path):
    service = make_service(tmp_path)
    candidates = [
        *make_candidates(service, 2, prefix="iPhone 稀疏"),
        *make_candidates(service, 14, prefix="公众讨论"),
    ]
    ranked_items = service._fallback_rank_items(candidates)

    groups, layout = service._pack_display_groups(ranked_items)

    assert layout["items_per_block"] == [4, 4, 4, 4]
    assert layout["target"] == "4x4"
    assert all(len(group["items"]) == 4 for group in groups)


def test_refresh_returns_stale_cache_when_not_enough_items(tmp_path):
    service = make_service(tmp_path)
    service._llm = SequenceLLM(["not json", "still not json"])

    async def fake_fetch():
        return make_candidates(service, 8)

    service._fetch_all_candidates = fake_fetch

    payload = run_async(service.refresh(source="manual"))

    assert payload["stale"] is True
    assert payload["stale_reason"] == "insufficient_display_items"
    assert payload["ranking_source"] == "stale_cache"
    assert payload["groups"] == []
