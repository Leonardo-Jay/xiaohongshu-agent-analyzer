import asyncio
import json
from io import BytesIO

import pytest
try:
    from pypdf import PdfReader
except ModuleNotFoundError:
    PdfReader = None

from app.agents import analyze_agent, orchestrator_agent, retrieve_agent, screen_agent, synthesis_agent
from app.memory.memory_types import ConsensusCluster, EntityMemory
from app.models.report_ir import Citation, ChartSpec, ReportIR, ReportMetadata
from app.reports.html_renderer import render_report_html
from app.reports.pdf_renderer import PdfRendererUnavailable, render_report_pdf
from app.reports.renderer import render_markdown
from app.tools.current_time import CurrentTimeClient
from app.tools.llm import LLMResponse, LongcatChatAdapter, ModelScopeChatAdapter, QianfanChatAdapter, ToolCall
import app.tools.llm as llm_tools
from app.tools.tool_schemas import SEARCH_POSTS_SCHEMA
from app.utils.memory_retrieval import MemoryRetrieval
from app.utils.temporal import infer_temporal_context, normalize_temporal_context


def run_async(coro):
    return asyncio.run(coro)


def test_current_time_tool_uses_api_when_available(monkeypatch):
    client = CurrentTimeClient(api_id="id", api_key="key")

    async def fake_fetch():
        return {
            "source": "apihz",
            "now_iso": "2026-05-17T12:30:00+08:00",
            "date": "2026-05-17",
            "timestamp": 1778992200,
            "timezone": "Asia/Shanghai",
        }

    monkeypatch.setattr(client, "_fetch_api_time", fake_fetch)

    result = run_async(client.get_current_time())

    assert result["source"] == "apihz"
    assert result["now_iso"] == "2026-05-17T12:30:00+08:00"
    assert result["timezone"] == "Asia/Shanghai"


def test_current_time_tool_falls_back_to_local_time(monkeypatch):
    monkeypatch.delenv("APIHZ_ID", raising=False)
    monkeypatch.delenv("APIHZ_KEY", raising=False)
    client = CurrentTimeClient(api_id="", api_key="")

    result = run_async(client.get_current_time())

    assert result["source"] == "local_fallback"
    assert result["timezone"] == "Asia/Shanghai"
    assert result["timestamp"] == 0
    assert "warning" in result


def test_temporal_context_infers_recent_and_evergreen_modes():
    current_time = {"date": "2026-05-17", "timezone": "Asia/Shanghai"}

    recent = infer_temporal_context("最近 nova6 怎么样", current_time=current_time)
    evergreen = infer_temporal_context("nova6 怎么样", current_time=current_time)
    half_year = infer_temporal_context("最近半年 nova6 评价", current_time=current_time)

    assert recent["mode"] == "recent"
    assert recent["window"]["start_date"] == "2026-04-17"
    assert recent["window"]["end_date"] == "2026-05-17"
    assert recent["retrieval_policy"] == "latest_first"
    assert evergreen["mode"] == "evergreen"
    assert evergreen["retrieval_policy"] == "balanced"
    assert half_year["window"]["label"] == "近半年"
    assert half_year["window"]["start_date"] == "2025-11-18"


def test_temporal_context_normalizes_sparse_llm_output():
    current_time = {"date": "2026-05-17", "timezone": "Asia/Shanghai"}
    raw = {
        "mode": "recent",
        "window": {"kind": "none", "start_date": "", "end_date": "", "label": "近期"},
        "retrieval_policy": "latest_first",
        "content_time_analysis": "auto",
        "reason": "用户询问最近评价",
    }

    normalized = normalize_temporal_context(raw, query="最近 nova6 怎么样", current_time=current_time)

    assert normalized["mode"] == "recent"
    assert normalized["window"]["kind"] == "relative"
    assert normalized["window"]["start_date"] == "2026-04-17"


def test_search_posts_schema_and_executor_pass_sort_type():
    class FakeClient:
        def __init__(self):
            self.called = {}

        async def search_posts(self, keyword, require_num=10, sort_type=0):
            self.called = {
                "keyword": keyword,
                "require_num": require_num,
                "sort_type": sort_type,
            }
            return [{"note_id": "n1", "title": "nova6 真实体验", "note_url": "https://example.com/n1"}]

    class Borrow:
        def __init__(self, client):
            self.client = client

        async def __aenter__(self):
            return self.client

        async def __aexit__(self, *args):
            return False

    class FakePool:
        def __init__(self, client):
            self.client = client

        def borrow(self):
            return Borrow(self.client)

    props = SEARCH_POSTS_SCHEMA["function"]["parameters"]["properties"]
    assert props["sort_type"]["enum"] == [0, 1, 2, 3, 4]

    client = FakeClient()
    new_posts = []
    result = run_async(
        retrieve_agent._execute_retrieve_tool(
            ToolCall(id="tc1", name="search_posts", arguments={"keyword": "nova6", "require_num": 2, "sort_type": 1}),
            FakePool(client),
            existing_ids=set(),
            exclude_set=set(),
            new_posts=new_posts,
            new_keywords=[],
            default_sort_type=0,
        )
    )

    assert result["sort_type"] == 1
    assert client.called["sort_type"] == 1
    assert new_posts[0]["sort_type_used"] == 1


def test_retrieve_time_filter_relaxes_when_window_has_too_few_posts():
    posts = [
        {"note_id": "old", "upload_time": "2026-01-01 10:00:00"},
        {"note_id": "new", "upload_time": "2026-05-10 10:00:00"},
    ]
    temporal_context = {
        "mode": "recent",
        "window": {
            "kind": "relative",
            "start_date": "2026-05-01",
            "end_date": "2026-05-17",
            "label": "近17天",
        },
        "retrieval_policy": "latest_first",
        "content_time_analysis": "auto",
        "reason": "测试",
    }

    filtered, stats = retrieve_agent._annotate_and_filter_by_time(
        posts,
        temporal_context,
        {"date": "2026-05-17"},
        target_posts=6,
    )

    assert len(filtered) == 2
    assert stats["time_filter_applied"] is True
    assert stats["time_filter_relaxed"] is True
    assert posts[0]["matched_time_window"] is False
    assert posts[1]["matched_time_window"] is True


def test_evidence_registry_preserves_post_and_comment_time_fields():
    screened_items = [{
        "note_id": "n1",
        "title": "nova6 使用体验",
        "note_url": "https://example.com/n1",
        "upload_time": "2026-05-01 10:00:00",
    }]
    comments = [
        {
            "comment_id": "__post_body__n1",
            "content": "续航没有达到宣传预期",
            "note_id": "n1",
            "create_time": "2026-05-01 10:00:00",
            "nickname": "[博主]",
        },
        {
            "comment_id": "c1",
            "content": "发热比我预期明显",
            "note_id": "n1",
            "create_time": "2026-05-02 11:00:00",
            "like_count": 3,
        },
    ]

    registry = analyze_agent._build_evidence_registry(screened_items, comments, current_time={"date": "2026-05-17"})

    assert registry[0]["evidence_id"] == "ev_000"
    assert registry[0]["source_type"] == "post_body"
    assert registry[0]["created_at"] == "2026-05-01"
    assert registry[1]["source_type"] == "comment"
    assert registry[1]["created_at"] == "2026-05-02"
    assert registry[1]["source_url"] == "https://example.com/n1"


def test_cluster_opinions_falls_back_to_evidence_clusters_when_llm_fails(monkeypatch):
    class FailingLLM:
        async def ainvoke(self, *args, **kwargs):
            raise asyncio.TimeoutError()

    monkeypatch.setattr(analyze_agent, "_llm", FailingLLM())

    result = run_async(
        analyze_agent.node_cluster_opinions(
            {
                "user_query_raw": "Claude Opus 能力怎么看",
                "screened_items": [{
                    "note_id": "n1",
                    "title": "Claude 使用体验",
                    "note_url": "https://example.com/n1",
                }],
                "_raw_comments_for_clustering": [
                    {
                        "comment_id": "__post_body__n1",
                        "content": "复杂任务里推理能力不错，但稳定性还有人质疑。",
                        "note_id": "n1",
                        "nickname": "[博主]",
                    }
                ],
                "clusters": [],
            },
            {},
        )
    )

    assert result["clusters"]
    assert result["clusters"][0]["fallback_generated"] is True
    assert result["clusters"][0]["evidence_ids"] == ["ev_000"]
    assert result["evidence_registry"][0]["content"].startswith("复杂任务")
    assert result["_recoverable_errors"][0]["error_type"] == "cluster_generation_timeout"


def test_check_quality_preserves_summary_when_analyze_is_already_done():
    result = run_async(
        analyze_agent.node_check_quality({
            "_analyze_done": True,
            "_fetched_comment_count": 1,
            "clusters": [{
                "topic": "稳定性质疑",
                "sentiment": "负面",
                "count": 1,
                "evidence_quotes": ["稳定性还有人质疑。"],
                "source_title": "Claude 使用体验",
            }],
        })
    )

    assert result["_analyze_done"] is True
    assert result["sentiment_summary"] == {"负面": 1}
    assert result["evidence_ledger"][0]["topic"] == "稳定性质疑"


def test_content_time_analysis_sanitizer_limits_events_and_banned_terms():
    payload = {
        "available": True,
        "ordering_basis": "parsed_time",
        "dominant_pattern": "多问题合流",
        "events": [
            {
                "id": "cte_bad",
                "order": 1,
                "title": "较早样本先出现续航问题",
                "summary": "使用禁用词",
                "cluster_ids": ["cl_0"],
                "evidence_ids": ["ev_001"],
            },
            *[
                {
                    "id": f"cte_{idx}",
                    "order": idx,
                    "title": f"问题表达第 {idx} 步扩展",
                    "summary": "先出现续航不满，随后扩展为发热和购买劝退。",
                    "cluster_ids": ["cl_0"],
                    "evidence_ids": ["ev_001"],
                    "confidence": 0.7,
                }
                for idx in range(2, 7)
            ],
        ],
        "limitations": [],
    }

    result = analyze_agent._sanitize_content_time_analysis(
        payload,
        allowed_cluster_ids={"cl_0"},
        allowed_evidence_ids={"ev_001"},
        ordering_basis="parsed_time",
    )

    assert result["available"] is True
    assert len(result["events"]) <= 4
    assert all("较早样本" not in event["title"] for event in result["events"])


def test_report_ir_quality_guard_requires_content_time_section():
    report = _sample_report_ir()
    context = {
        "allowed_citation_ids": ["cit_0"],
        "allowed_cluster_ids": ["cl_0"],
        "required_cluster_ids": [],
        "citations": [{"id": "cit_0", "quote": "游戏半小时掉电很快。"}],
        "content_time_analysis": {
            "available": True,
            "events": [
                {
                    "title": "续航落差先成为负面讨论入口",
                    "citation_ids": ["cit_0"],
                }
            ],
        },
    }

    issues = synthesis_agent._report_ir_issues(report, context)

    assert any("内容事件演化" in issue for issue in issues)


def test_report_context_recovers_clusters_and_citations_from_raw_comments():
    context = synthesis_agent._build_report_context({
        "user_query_raw": "Claude Opus 能力怎么看",
        "screened_items": [{
            "note_id": "n1",
            "title": "Claude 使用体验",
            "note_url": "https://example.com/n1",
        }],
        "retrieved_comments": [
            {
                "comment_id": "__post_body__n1",
                "content": "复杂任务里推理能力不错，但稳定性还有人质疑。",
                "note_id": "n1",
            }
        ],
        "clusters": [],
    })

    assert context["allowed_cluster_ids"] == ["cl_0"]
    assert context["allowed_citation_ids"] == ["cit_0"]
    assert context["clusters"][0]["citation_ids"] == ["cit_0"]
    assert len(context["default_charts"][0]["data"]) >= 3


def test_orchestrator_reasoning_falls_back_when_llm_fails(monkeypatch):
    class FailingLLM:
        async def ainvoke(self, *args, **kwargs):
            raise RuntimeError("simulated llm failure")

    monkeypatch.setattr(orchestrator_agent, "_llm_reasoning", FailingLLM())

    result = run_async(
        orchestrator_agent.node_reasoning({
            "user_query_raw": "吴克群助农卖菜争议",
            "_intent_round": 0,
            "_session_intent_frame": {},
            "_session_last_run_ref": {},
        })
    )

    assert result["intent"] == "general"
    assert result["intent_confidence"] == 0.0
    assert result["product_entities"] == ["吴克群"]
    assert result["search_context"]["primary_entity"] == "吴克群"
    assert "吴克群助农卖菜争议" in result["search_context"]["search_hints"]
    assert result["_intent_round"] == 1


def test_screen_pre_filter_removes_ads_brand_accounts_and_contact_info():
    posts = [
        {
            "note_id": "keep-1",
            "title": "真实体验：用了两周的优缺点",
            "desc": "续航不错，但晚上拍照一般，评论区也有人提到发热。",
            "user": {"level": "普通用户"},
        },
        {
            "note_id": "ad-1",
            "title": "限时优惠，下单送配件",
            "desc": "今天购买更划算。",
            "user": {},
        },
        {
            "note_id": "brand-1",
            "title": "官方新品介绍",
            "desc": "品牌官方信息。",
            "user": {"level": "品牌号"},
        },
        {
            "note_id": "contact-1",
            "title": "测评资料整理",
            "desc": "需要详细资料可以 V: test123 私信。",
            "user": {},
        },
    ]

    result = run_async(screen_agent.node_pre_filter({"retrieved_posts": posts, "_screen_round": 0}))

    passed_ids = [post["note_id"] for post in result["_pre_filter_passed"]]
    assert passed_ids == ["keep-1"]
    assert result["_pre_filter_stats"]["rejected_ad"] == 1
    assert result["_pre_filter_stats"]["rejected_brand"] == 1
    assert result["_pre_filter_stats"]["rejected_contact"] == 1
    assert "_compressed" in result["_pre_filter_passed"][0]


def test_comment_filter_removes_low_information_comments():
    comments = [
        {"comment_id": "valid-1", "content": "续航用了三天还有电，通勤够用。"},
        {"comment_id": "short-1", "content": "好"},
        {"comment_id": "emoji-1", "content": "🔥🔥🔥"},
        {"comment_id": "repeat-1", "content": "哈哈哈哈"},
        {"comment_id": "valid-2", "content": "拍照不错，不过夜景噪点有点明显。"},
    ]

    valid, filtered_count = analyze_agent._filter_invalid_comments(comments)

    assert [comment["comment_id"] for comment in valid] == ["valid-1", "valid-2"]
    assert filtered_count == 3


def test_memory_retrieval_full_reuse_decision(tmp_path):
    base_dir = tmp_path / "memory"
    entity_dir = base_dir / "entities" / "PhoneX"
    entity_dir.mkdir(parents=True)

    memory = EntityMemory(
        entity="PhoneX",
        consensus_clusters=[
            ConsensusCluster(
                topic="续航和发热反馈",
                sentiment="负面",
                primary_aspects=["续航", "发热"],
                sub_aspects=["电池", "温控"],
                synonym_aspects=["电池续航", "机身发烫"],
                avg_count=18,
                frequency=3,
                evidence_ids=["ev_001"],
            )
        ],
    )
    (entity_dir / "memory.json").write_text(
        json.dumps(memory.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )

    retrieval = MemoryRetrieval(base_dir=str(base_dir))
    decision = run_async(
        retrieval.retrieve_and_decide(
            entity="PhoneX",
            current_query="PhoneX 续航和发热怎么样",
            intent="quality_issue",
            key_aspects=["续航", "发热"],
        )
    )

    assert decision.can_reuse is True
    assert decision.reuse_strategy == "full"
    assert decision.coverage_ratio == 1.0
    assert decision.matched_aspects == ["续航", "发热"]
    assert decision.reusable_clusters[0]["topic"] == "续航和发热反馈"


def test_memory_retrieval_none_when_aspects_are_not_covered(tmp_path):
    base_dir = tmp_path / "memory"
    entity_dir = base_dir / "entities" / "PhoneX"
    entity_dir.mkdir(parents=True)

    memory = EntityMemory(
        entity="PhoneX",
        consensus_clusters=[
            ConsensusCluster(
                topic="外观设计认可",
                sentiment="正面",
                primary_aspects=["外观"],
                sub_aspects=["配色"],
                avg_count=10,
                frequency=2,
            )
        ],
    )
    (entity_dir / "memory.json").write_text(
        json.dumps(memory.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )

    retrieval = MemoryRetrieval(base_dir=str(base_dir))
    decision = run_async(
        retrieval.retrieve_and_decide(
            entity="PhoneX",
            current_query="PhoneX 游戏性能怎么样",
            intent="user_experience",
            key_aspects=["游戏性能"],
        )
    )

    assert decision.can_reuse is False
    assert decision.reuse_strategy == "none"
    assert decision.coverage_ratio == 0.0
    assert decision.reusable_clusters == []


def test_synthesis_outline_guard_rejects_hallucinated_and_missing_clusters():
    state = {
        "_synthesis_round": 1,
        "_synthesis_done": False,
        "clusters": [
            {"topic": "续航焦虑", "count": 3},
            {"topic": "发热明显", "count": 4},
            {"topic": "夜景拍照噪点", "count": 2},
        ],
        "_report_outline": {
            "report_strategy": {
                "structure": [
                    {"chapter": "总体舆情", "focus": "概览", "use_clusters": [0]},
                    {"chapter": "核心负面", "focus": "问题", "use_clusters": [99]},
                    {"chapter": "用户分歧", "focus": "分歧", "use_clusters": []},
                    {"chapter": "总结", "focus": "结论", "use_clusters": [0]},
                ]
            }
        },
    }

    result = run_async(synthesis_agent.node_observe_outline(state))

    assert result["_synthesis_done"] is False
    assert "不存在的簇编号" in result["_outline_feedback"]
    assert "遗漏观点" in result["_outline_feedback"]
    assert "发热明显" in result["_outline_feedback"]


def test_synthesis_outline_guard_requires_summary_chapter():
    state = {
        "_synthesis_round": 1,
        "_synthesis_done": False,
        "clusters": [{"topic": "续航焦虑", "count": 3}],
        "_report_outline": {
            "report_strategy": {
                "structure": [
                    {"chapter": "总体舆情", "focus": "概览", "use_clusters": [0]},
                    {"chapter": "行动建议", "focus": "建议", "use_clusters": [0]},
                ]
            }
        },
    }

    result = run_async(synthesis_agent.node_observe_outline(state))

    assert result["_synthesis_done"] is False
    assert "末章必须命名" in result["_outline_feedback"]


def test_synthesis_outline_guard_rejects_generic_analysis_titles():
    state = {
        "_synthesis_round": 1,
        "_synthesis_done": False,
        "clusters": [
            {"topic": "续航焦虑", "count": 3},
            {"topic": "发热明显", "count": 3},
        ],
        "_report_outline": {
            "report_strategy": {
                    "structure": [
                        {"chapter": "整体印象", "focus": "概览", "use_clusters": []},
                        {"chapter": "续航表现", "focus": "续航、发热", "use_clusters": [0, 1]},
                        {"chapter": "发热问题", "focus": "发热、体验", "use_clusters": [1]},
                        {"chapter": "总结", "focus": "建议", "use_clusters": [0, 1]},
                    ]
                }
        },
    }

    result = run_async(synthesis_agent.node_observe_outline(state))

    assert result["_synthesis_done"] is False
    assert "标签化" in result["_outline_feedback"]
    assert "focus 过短" in result["_outline_feedback"]


def _sample_report_ir() -> ReportIR:
    return ReportIR(
        title="PhoneX 续航舆情分析报告",
        metadata=ReportMetadata(
            query="PhoneX 续航怎么样",
            intent="quality_issue",
            post_count=3,
            comment_count=12,
            confidence_score=0.78,
            limitations=["样本量仍然偏小"],
        ),
        summary_cards=[
            {
                "label": "整体倾向",
                "value": "负面反馈集中在重度使用场景",
                "supporting_cluster_ids": ["cl_0"],
            }
        ],
        sections=[
            {
                "id": "sec_overview",
                "title": "整体印象",
                "type": "overview",
                "cluster_ids": ["cl_0"],
                "blocks": [
                    {
                        "type": "paragraph",
                        "text": "用户对 PhoneX 续航的评价并不一致，轻度使用者接受度较高。",
                        "citation_ids": ["cit_0"],
                    }
                ],
            },
            {
                "id": "sec_analysis",
                "title": "续航压力来源",
                "type": "analysis",
                "cluster_ids": ["cl_0"],
                "blocks": [
                    {"type": "subheading", "text": "高负载场景掉电更明显"},
                    {
                        "type": "paragraph",
                        "text": "集中投诉来自游戏、导航等连续高亮屏场景。",
                        "citation_ids": ["cit_0"],
                    },
                ],
            },
            {
                "id": "sec_summary",
                "title": "总结",
                "type": "recommendation",
                "cluster_ids": ["cl_0"],
                "blocks": [
                    {
                        "type": "list",
                        "items": ["重度用户应重点关注真实续航口碑。"],
                        "citation_ids": [],
                    }
                ],
            },
        ],
        charts=[
            ChartSpec(
                id="chart_sentiment",
                type="bar",
                title="情绪分布",
                data=[{"label": "负面", "value": 7}, {"label": "正面", "value": 3}],
            )
        ],
        citations=[
            Citation(
                id="cit_0",
                cluster_id="cl_0",
                topic="续航焦虑",
                sentiment="负面",
                source_title="用户原话",
                quote="游戏半小时掉电很快。",
            )
        ],
    )


def _sample_report_ir_payload() -> dict:
    payload = _sample_report_ir().model_dump()
    payload.pop("metadata", None)
    payload.pop("citations", None)
    return payload


def _report_ir_generation_state() -> dict:
    return {
        "user_query_raw": "PhoneX 续航怎么样",
        "intent": "quality_issue",
        "screened_items": [{
            "note_id": "n1",
            "title": "PhoneX 续航体验",
            "note_url": "https://example.com/n1",
        }],
        "retrieved_comments": [{
            "comment_id": "c1",
            "content": "游戏半小时掉电很快。",
            "note_id": "n1",
        }],
        "clusters": [{
            "topic": "续航焦虑",
            "sentiment": "负面",
            "count": 3,
            "evidence_quotes": ["游戏半小时掉电很快。"],
            "source_title": "用户原话",
            "source_note_url": "https://example.com/n1",
        }],
        "confidence_score": 0.78,
        "limitations": ["样本量仍然偏小"],
    }


class _FakeReportLLM:
    def __init__(self, responses: list[str]):
        self.responses = responses
        self.calls: list[str] = []

    async def ainvoke(self, prompt):
        self.calls.append(prompt)
        index = len(self.calls) - 1
        content = self.responses[index] if index < len(self.responses) else self.responses[-1]
        return LLMResponse(content=content, finish_reason="stop")

    async def astream(self, prompt):
        raise AssertionError("ReportIR should not use streaming unless non-streaming transport fails")
        yield ""

    @staticmethod
    def _normalize_text(text: str) -> str:
        return text.strip()


def test_llm_stream_uses_configured_max_tokens(monkeypatch):
    captured_payloads = []

    class FakeStreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield "data: [DONE]"

    class FakeStream:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, *args):
            return False

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, headers=None, json=None):
            captured_payloads.append(json)
            return FakeStream()

    monkeypatch.setattr(llm_tools.httpx, "AsyncClient", FakeAsyncClient)

    adapters = [
        QianfanChatAdapter(
            api_url="https://example.com/qianfan",
            bearer_token="token",
            model="m",
            max_tokens=8192,
        ),
        LongcatChatAdapter(
            api_url="https://example.com/longcat",
            api_key="token",
            model="m",
            max_tokens=8192,
        ),
        ModelScopeChatAdapter(
            api_url="https://example.com/modelscope",
            api_key="token",
            model="m",
            max_tokens=8192,
        ),
    ]

    async def collect_all():
        for adapter in adapters:
            async for _ in adapter.astream("prompt"):
                pass

    run_async(collect_all())

    assert [payload["max_tokens"] for payload in captured_payloads] == [8192, 8192, 8192]


def test_report_ir_renderer_outputs_metadata_charts_and_citations():
    markdown = render_markdown(_sample_report_ir())

    assert markdown.startswith("# PhoneX 续航舆情分析报告")
    assert "样本：3 篇帖子，12 条评论；置信度：78%" in markdown
    assert "## 数据概览" in markdown
    assert "| 负面 | 7 |" in markdown
    assert "集中投诉来自游戏、导航等连续高亮屏场景。[1]" in markdown
    assert "## 参考证据" in markdown
    assert "[1] 用户原话：游戏半小时掉电很快。" in markdown


def test_report_html_renderer_outputs_internal_and_external_links():
    report = _sample_report_ir()
    report.citations[0].source_url = "https://example.com/post/1"

    html = render_report_html(report)

    assert 'href="#ref-1"' in html
    assert 'id="ref-1"' in html
    assert 'href="https://example.com/post/1"' in html


def test_report_renderers_output_content_time_section_as_regular_ir_section():
    raw_payload = _sample_report_ir().model_dump()
    raw_payload["sections"].insert(
        1,
        {
            "id": "sec_content_time",
            "title": "内容事件演化",
            "type": "analysis",
            "cluster_ids": ["cl_0"],
            "blocks": [
                {"type": "subheading", "text": "续航落差先成为负面讨论入口"},
                {
                    "type": "paragraph",
                    "text": "用户先集中表达续航落差，随后把这种不满扩展到可靠性判断。",
                    "citation_ids": ["cit_0"],
                },
            ],
        },
    )
    report = ReportIR.model_validate(raw_payload)

    markdown = render_markdown(report)
    html = render_report_html(report)

    assert "## 内容事件演化" in markdown
    assert "[1]" in markdown
    assert "内容事件演化" in html
    assert 'href="#ref-1"' in html


def test_report_pdf_renderer_outputs_pdf_with_annotations():
    if PdfReader is None:
        pytest.skip("pypdf is not installed")

    report = _sample_report_ir()
    report.citations[0].source_url = "https://example.com/post/1"

    try:
        pdf_bytes = render_report_pdf(report)
    except PdfRendererUnavailable as exc:
        pytest.skip(str(exc))

    assert pdf_bytes.startswith(b"%PDF")
    reader = PdfReader(BytesIO(pdf_bytes))
    annotations = []
    for page in reader.pages:
        annotations.extend(page.get("/Annots") or [])
    annotation_objects = [item.get_object() for item in annotations]
    assert any(item.get("/Dest") for item in annotation_objects)
    assert any(item.get("/A", {}).get("/URI") == "https://example.com/post/1" for item in annotation_objects)


def test_report_ir_coercion_normalizes_conclusion_section_type():
    sample = _sample_report_ir()
    raw_payload = sample.model_dump()
    raw_payload.pop("metadata")
    raw_payload.pop("citations")
    raw_payload["sections"][-1]["type"] = "conclusion"

    report = synthesis_agent._coerce_report_ir(
        raw_payload,
        metadata=sample.metadata,
        citations=sample.citations,
    )

    assert report.sections[-1].type == "recommendation"


def test_report_ir_quality_guard_rejects_thin_template_analysis():
    report = _sample_report_ir()
    raw_payload = report.model_dump()
    raw_payload["sections"][1]["title"] = "核心问题分析"
    raw_payload["sections"][1]["blocks"] = [
        {"type": "subheading", "text": "续航表现"},
        {"type": "paragraph", "text": "续航问题引发用户不满，实际体验不佳。", "citation_ids": ["cit_0"]},
        {"type": "subheading", "text": "品控问题"},
        {"type": "paragraph", "text": "品控问题影响用户信心。", "citation_ids": ["cit_0"]},
    ]
    report = ReportIR.model_validate(raw_payload)
    context = {
        "allowed_citation_ids": ["cit_0"],
        "allowed_cluster_ids": ["cl_0"],
        "required_cluster_ids": [],
        "citations": [{"id": "cit_0", "quote": "游戏半小时掉电很快。"}],
    }

    issues = synthesis_agent._report_ir_issues(report, context)

    assert any("标签化" in issue for issue in issues)
    assert any("过短" in issue for issue in issues)
    assert any("没有自然嵌入用户原话" in issue for issue in issues)


def test_report_ir_quality_guard_does_not_require_quote_embedding_without_citations():
    report = _sample_report_ir()
    context = {
        "allowed_citation_ids": [],
        "allowed_cluster_ids": ["cl_0"],
        "required_cluster_ids": [],
        "citations": [],
    }

    issues = synthesis_agent._report_ir_issues(report, context)

    assert not any("没有自然嵌入用户原话" in issue for issue in issues)


def test_report_ir_content_time_section_allows_single_event_subheading():
    raw_payload = _sample_report_ir().model_dump()
    raw_payload["sections"].insert(
        1,
        {
            "id": "sec_content_time",
            "title": "内容事件演化",
            "type": "analysis",
            "cluster_ids": ["cl_0"],
            "blocks": [
                {"type": "subheading", "text": "续航落差先成为负面讨论入口"},
                {
                    "type": "paragraph",
                    "text": (
                        "用户先用游戏半小时掉电很快。来描述高负载场景里的续航落差，"
                        "随后这种体验不满会被扩展成对整机可靠性的判断；在样本很少时，"
                        "这一事件更适合被写成单一演化节点，而不是硬拆成多个并不存在的阶段。"
                    ),
                    "citation_ids": ["cit_0"],
                },
            ],
        },
    )
    report = ReportIR.model_validate(raw_payload)
    context = {
        "allowed_citation_ids": ["cit_0"],
        "allowed_cluster_ids": ["cl_0"],
        "required_cluster_ids": [],
        "citations": [{"id": "cit_0", "quote": "游戏半小时掉电很快。"}],
        "content_time_analysis": {
            "available": True,
            "events": [{
                "title": "续航落差先成为负面讨论入口",
                "citation_ids": ["cit_0"],
            }],
        },
    }

    issues = synthesis_agent._report_ir_issues(report, context)

    assert not any("内容事件演化」至少需要 2 个洞察型 subheading" in issue for issue in issues)
    assert not any("续航落差先成为负面讨论入口" in issue and "120" in issue for issue in issues)


def test_report_ir_quality_guard_rejects_thin_summary_and_count_only_chart():
    report = _sample_report_ir()
    raw_payload = report.model_dump()
    raw_payload["summary_cards"] = [
        {"label": "整体倾向", "value": "负面偏多", "supporting_cluster_ids": ["cl_0"]},
    ]
    raw_payload["charts"] = [
        {
            "id": "chart_sentiment",
            "type": "bar",
            "title": "情绪分布",
            "data": [
                {"label": "负面", "value": 7},
                {"label": "正面", "value": 3},
            ],
        }
    ]
    report = ReportIR.model_validate(raw_payload)
    context = {
        "allowed_citation_ids": ["cit_0"],
        "allowed_cluster_ids": ["cl_0"],
        "required_cluster_ids": [],
        "citations": [{"id": "cit_0", "quote": "游戏半小时掉电很快。"}],
    }

    issues = synthesis_agent._report_ir_issues(report, context)

    assert any("关键摘要至少需要" in issue for issue in issues)
    assert any("关键摘要「整体倾向」过于单薄" in issue for issue in issues)
    assert any("数据概览不能只列情绪数量" in issue for issue in issues)
    assert any("舆情指标概览" in issue for issue in issues)


def test_report_renderer_outputs_overview_table_with_insight_column():
    report = _sample_report_ir()
    report.charts = [
        ChartSpec(
            id="chart_overview",
            type="table",
            title="舆情指标概览",
            data=[
                {
                    "label": "样本规模",
                    "value": "3 篇帖子 / 12 条评论",
                    "insight": "样本量偏小，适合判断方向，不宜直接外推为全网比例。",
                },
                {
                    "label": "高频议题",
                    "value": "续航焦虑",
                    "insight": "该议题直接影响重度用户的购买信心。",
                },
            ],
        )
    ]

    markdown = render_markdown(report)
    html = render_report_html(report)

    assert "| 指标 | 当前表现 | 解读 |" in markdown
    assert "| 样本规模 | 3 篇帖子 / 12 条评论 | 样本量偏小" in markdown
    assert "<th>解读</th>" in html
    assert "该议题直接影响重度用户的购买信心" in html


def test_report_ir_unparseable_raw_text_uses_raw_repair(monkeypatch):
    payload = _sample_report_ir_payload()
    fake_llm = _FakeReportLLM([
        "这是一段不是 JSON 的结构化报告草稿，长度足够长，但无法被 JSON 解析器直接抽取。",
        json.dumps(payload, ensure_ascii=False),
    ])
    monkeypatch.setattr(synthesis_agent, "_llm_plan", fake_llm)
    monkeypatch.setattr(synthesis_agent, "_report_ir_issues", lambda report, context: [])

    report = run_async(synthesis_agent._generate_report_ir(_report_ir_generation_state()))

    assert report.title == "PhoneX 续航舆情分析报告"
    assert len(fake_llm.calls) == 2
    assert "原始输出前段" in fake_llm.calls[1]


def test_report_ir_schema_validation_still_uses_repair_prompt(monkeypatch):
    payload = _sample_report_ir_payload()
    fake_llm = _FakeReportLLM([
        json.dumps({"version": "1.0", "sections": []}, ensure_ascii=False),
        json.dumps(payload, ensure_ascii=False),
    ])
    monkeypatch.setattr(synthesis_agent, "_llm_plan", fake_llm)
    monkeypatch.setattr(synthesis_agent, "_report_ir_issues", lambda report, context: [])

    report = run_async(synthesis_agent._generate_report_ir(_report_ir_generation_state()))

    assert report.title == "PhoneX 续航舆情分析报告"
    assert len(fake_llm.calls) == 2
    assert "校验问题" in fake_llm.calls[1]


def test_synthesis_execute_returns_report_ir_and_compatible_markdown(monkeypatch):
    report_ir = _sample_report_ir()

    async def fake_generate_report_ir(state):
        return report_ir

    monkeypatch.setattr(synthesis_agent, "_generate_report_ir", fake_generate_report_ir)

    result = run_async(
        synthesis_agent.node_execute_report(
            {
                "user_query_raw": "PhoneX 续航怎么样",
                "screened_items": [{"note_id": "1"}],
                "retrieved_comments": [{"comment_id": "c1"}],
                "clusters": [{"topic": "续航焦虑", "count": 3}],
                "confidence_score": 0.78,
                "limitations": [],
            },
            {"configurable": {}},
        )
    )

    assert result["report_ir"]["version"] == "1.0"
    assert result["report_ir"]["sections"][0]["title"] == "整体印象"
    assert result["final_answer"].startswith("# PhoneX 续航舆情分析报告")
    assert "## 参考证据" in result["final_answer"]


def test_synthesis_execute_returns_empty_report_ir_when_falling_back(monkeypatch):
    async def failing_generate_report_ir(state):
        raise ValueError("schema failed")

    async def fake_legacy_markdown(state, config):
        return {"final_answer": "# 旧版报告\n\n内容"}

    monkeypatch.setattr(synthesis_agent, "_generate_report_ir", failing_generate_report_ir)
    monkeypatch.setattr(synthesis_agent, "_legacy_execute_markdown_report", fake_legacy_markdown)

    result = run_async(
        synthesis_agent.node_execute_report(
            {
                "user_query_raw": "PhoneX 续航怎么样",
                "screened_items": [{"note_id": "1"}],
                "retrieved_comments": [{"comment_id": "c1"}],
                "clusters": [{"topic": "续航焦虑", "count": 3}],
            },
            {"configurable": {}},
        )
    )

    assert result["final_answer"].startswith("# 旧版报告")
    assert result["report_ir"] == {}
