"""Workflow runner — 固定外层流水线，编排各 Agent/子图并通过 asyncio.Queue 推送 SSE 进度。

执行顺序:
  1. orchestrator_subgraph（内部 ReAct 循环）
     - 意图识别，输出 intent, key_aspects, user_needs, search_context 等

  2. retrieve_subgraph（Function Calling 循环）
     - retrieve_fc（LLM 自主决策调用 search_posts）
     - 基于 orchestrator 的 search_context 生成关键词，搜索帖子
     - 目标: >= 7 篇帖子，最多 3 轮循环
     - MCP 连接池固定 1 个（避免并发爬取导致封号）

  3. screen_subgraph（三阶段流水线）
     - pre_filter → detect_ads → rank_and_select
     - 使用 orchestrator 的 key_aspects、user_needs 进行相关性筛选
     - 过滤广告/软广，输出 screened_items

  4. analyze_subgraph（Function Calling 循环）
     - fetch_comments_fc（LLM 自主决策调用 search_comments）→ cluster_opinions → validate_clusters → check_quality
     - 选择帖子爬取评论，过滤无效评论，观点聚类
     - 循环终止条件: 评论>=50且冲突观点 / 评论>=40且观点簇>=5 / 达到2轮上限
     - 每轮爬取 3 篇帖子的评论，MCP 连接池大小跟随 MCP_POOL_SIZE 环境变量

  5. synthesize_subgraph（Plan and Execute 架构）
     - 制定报告大纲，生成分析报告

记忆机制（Karpathy Wiki 架构）:
  - 知识预编译：观点簇存储时生成三层标签（主标签、子标签、同义标签）
  - 纯结构化检索：基于字符串匹配，不使用 embedding
  - 证据驱动：每个观点簇关联原始评论证据
  - 默认关闭，通过前端 enable_memory 参数开启
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from loguru import logger

from app.agents.synthesis_agent import build_synthesis_graph
from app.agents.analyze_agent import build_analyze_graph
from app.agents.orchestrator_agent import build_orchestrator_graph
from app.agents.retrieve_agent import build_retrieve_graph
from app.agents.screen_agent import build_screen_graph
from app.models.schemas import GraphState
from app.tools.mcp_client import XhsMcpClient, XhsMcpClientPool
from app.utils.daily_audit_log import append_audit_log
from app.utils.memory_storage import MemoryBlock, MemoryStorage
from app.utils.memory_retrieval import get_memory_retrieval, ReuseDecision
from app.utils.session_memory import get_session_manager

# 编译 orchestrator 子图（ReAct 循环）
_orchestrator_app = build_orchestrator_graph()
# 编译 retrieve 子图（ReAct 循环）
_retrieve_app = build_retrieve_graph()
# 编译 screen 子图（三阶段流水线）
_screen_app = build_screen_graph()
# 编译 analyze 子图（ReAct 循环）
_analyze_app = build_analyze_graph()
# 编译 synthesis 子图（Plan and Execute）
_synthesis_app = build_synthesis_graph()


def _progress(queue: asyncio.Queue, stage: str, message: str, progress: int) -> None:
    queue.put_nowait({"event": "progress", "data": {"stage": stage, "message": message, "progress": progress}})


async def run_analysis(
    query: str,
    run_id: str,
    queue: asyncio.Queue,
    cookie: str | None = None,
    enable_memory: bool | None = None
) -> None:
    """在后台 task 中执行全流程，结果/错误通过 queue 发送。"""
    state: GraphState = {
        "request_id": run_id,
        "session_id": "",
        "user_query_raw": query,
        # Orchestrator 初始化
        "user_query_rewritten": query,
        "intent": "general",
        "intent_confidence": 0.0,
        "product_entities": [],
        "aliases": [],
        "entities_confidence": 0.0,
        "key_aspects": [],
        "user_needs": [],
        "search_context": {},
        "intent_analysis_score": 0.0,
        "missing_dimensions": [],
        # Retrieve 初始化
        "query_plan": [],
        "search_attempts": 0,
        "retrieved_posts": [],
        "retrieval_coverage_score": 0.0,
        # Retrieve 内部控制字段
        "_retrieve_round": 0,
        "_retrieve_done": False,
        "_current_batch": [],
        "_used_keywords": [],
        # Analyze 内部控制字段
        "_analyze_round": 0,
        "_analyze_done": False,
        "_posts_to_fetch": [],
        "_fetched_comment_count": 0,
        "_filtered_comment_count": 0,
        "_need_refetch": False,
        "_enable_memory": enable_memory if enable_memory is not None else (os.getenv("ENABLE_MEMORY", "false").lower() == "true"),
        "_api_type": int(os.getenv("XHS_API_TYPE", "2")),
        "_reuse_strategy": "",
        "_coverage_ratio": 0.0,
        "_reusable_clusters": [],
        "_reuse_ratio": 0.0,
        "_exclude_note_ids": [],
        # 其他阶段
        "screened_items": [],
        "screening_stats": {},
        "retrieved_comments": [],
        "clusters": [],
        "sentiment_summary": {},
        "evidence_ledger": [],
        "_raw_comments_for_clustering": [],
        "memory_context": "",
        "confidence_score": 0.0,
        "limitations": [],
        "final_answer": "",
        "tool_errors": [],
        "stream_events": [],
        # Orchestrator 内部控制字段
        "_intent_round": 0,
        "_intent_done": False,
    }

    _progress(queue, "start", "分析任务已启动...", 3)

    try:
        # ── 1. Orchestrator Subgraph (ReAct: reasoning → action → observation)
        #     负责意图识别，输出高质量的意图分析结果
        _progress(queue, "orchestrator", "正在分析查询意图...", 8)
        config = {"configurable": {"queue": queue}}
        orchestrator_output = await _orchestrator_app.ainvoke(state, config=config)
        state = {**state, **orchestrator_output}
        logger.info(
            f"[Workflow][Orchestrator] finished: intent={state.get('intent')}, "
            f"confidence={state.get('intent_confidence', 0.0):.2f}, "
            f"score={state.get('intent_analysis_score', 0.0):.2f}"
        )

        # 输出传递给 Retrieve Agent 的完整意图分析结果
        logger.info(
            f"[Workflow][Orchestrator] Analysis Result:\n"
            f"  ├─ entities: {state.get('product_entities', [])}\n"
            f"  ├─ aliases: {state.get('aliases', [])}\n"
            f"  ├─ key_aspects: {state.get('key_aspects', [])}\n"
            f"  ├─ user_needs: {state.get('user_needs', [])}\n"
            f"  └─ search_context: {state.get('search_context', {})}"
        )

        _progress(
            queue,
            "orchestrator",
            f"意图：{state.get('intent')}，实体：{state.get('product_entities')}，"
            f"质量分数：{state.get('intent_analysis_score', 0.0):.2f}",
            18,
        )

        # ── 2. 记忆检索阶段 ──
        # 使用传入的配置，如果没有则使用默认值（默认关闭）
        enable_memory_flag = enable_memory if enable_memory is not None else (os.getenv("ENABLE_MEMORY", "false").lower() == "true")

        reuse_decision: ReuseDecision | None = None

        # 获取 session_id 用于短期记忆
        session_id = state.get("session_id", "")

        if enable_memory_flag:
            _progress(queue, "memory", "正在检索历史记忆...", 20)

            # 从 Orchestrator 获取准确的实体和意图
            entity = state.get("product_entities", [""])[0] if state.get("product_entities") else ""
            intent = state.get("intent", "general")
            key_aspects_raw = state.get("key_aspects", [])

            # 提取 aspect 字符串（key_aspects 是字典列表）
            key_aspects = [
                item["aspect"] if isinstance(item, dict) else item
                for item in key_aspects_raw
            ]

            # 尝试长期记忆检索
            if entity:
                memory_retrieval = get_memory_retrieval()
                reuse_decision = await memory_retrieval.retrieve_and_decide(
                    entity=entity,
                    current_query=query,
                    intent=intent,
                    key_aspects=key_aspects,  # NEW: 传入用户关注点
                    use_llm=True
                )

            if reuse_decision:
                # 判断是否有历史记忆
                has_memory = reuse_decision.entity_memory is not None
                matched_aspects = reuse_decision.matched_aspects or []

                if has_memory and matched_aspects:
                    # 有记忆且有匹配的观点
                    if reuse_decision.can_reuse:
                        # 可以复用：显示覆盖度和策略
                        logger.info(
                            f"[Workflow][Memory] LLM 决策: coverage={reuse_decision.coverage_ratio:.2f}, "
                            f"strategy={reuse_decision.reuse_strategy}, "
                            f"reusable_clusters={len(reuse_decision.reusable_clusters)}"
                        )

                        # 将决策结果注入 state
                        state["_reuse_strategy"] = reuse_decision.reuse_strategy
                        state["_coverage_ratio"] = reuse_decision.coverage_ratio
                        state["_reusable_clusters"] = reuse_decision.reusable_clusters

                        progress_msg = f"发现历史记忆（覆盖度 {reuse_decision.coverage_ratio*100:.0f}%），策略：{reuse_decision.reuse_strategy}"
                        logger.info(f"[Workflow][Memory] 发送进度消息: {progress_msg}")
                        _progress(
                            queue,
                            "memory",
                            progress_msg,
                            25
                        )
                    else:
                        # 覆盖度低，不复用：显示匹配的部分，但说明采用全新分析
                        matched_str = "、".join(matched_aspects[:4])  # 最多显示4个
                        if len(matched_aspects) > 4:
                            matched_str += f"等{len(matched_aspects)}个方面"
                        progress_msg = f"历史记忆匹配：{matched_str}（覆盖度较低，采用全新分析）"
                        logger.info(f"[Workflow][Memory] 发送进度消息: {progress_msg}")
                        _progress(queue, "memory", progress_msg, 25)
                elif has_memory and not matched_aspects:
                    # 有记忆但无匹配观点
                    _progress(queue, "memory", "历史记忆无相关观点，从头开始分析", 25)
                else:
                    # 真正无历史记忆
                    _progress(queue, "memory", "无历史记忆，从头开始分析", 25)
            else:
                # reuse_decision 为 None（异常情况）
                _progress(queue, "memory", "无历史记忆，从头开始分析", 25)

        # ── 根据复用策略决定后续流程 ──
        if reuse_decision and reuse_decision.reuse_strategy == "full":
            # 完全复用模式：跳过 Retrieve/Screen/Analyze
            logger.info("[Workflow] 完全复用模式：跳过爬取和聚类")

            # 直接使用历史观点簇
            state["clusters"] = reuse_decision.reusable_clusters

            # DEBUG: 打印 reusable_clusters 的结构
            logger.info(f"[Workflow] DEBUG: reusable_clusters count = {len(reuse_decision.reusable_clusters)}")
            if reuse_decision.reusable_clusters:
                first_cluster = reuse_decision.reusable_clusters[0]
                logger.info(f"[Workflow] DEBUG: first cluster keys = {list(first_cluster.keys())}")
                logger.info(f"[Workflow] DEBUG: first cluster topic = {first_cluster.get('topic')}")
                logger.info(f"[Workflow] DEBUG: first cluster evidence_ids = {first_cluster.get('evidence_ids', [])}")

            # 为完全复用模式生成 references
            entity = state.get("product_entities", [""])[0] if state.get("product_entities") else ""
            references = []

            for cluster in reuse_decision.reusable_clusters:
                cluster_topic = cluster.get("topic", "历史观点")
                cluster_sentiment = cluster.get("sentiment", "中立")
                evidence_ids = cluster.get("evidence_ids", [])

                # 收集该簇的评论内容
                quotes = []
                source_note_id = None
                source_note_url = None
                source_title = None

                for evidence_id in evidence_ids[:3]:  # 最多取3条
                    # 加载证据文件
                    evidence_file = Path(__file__).parent.parent.parent / "data" / "memory" / "entities" / entity / "evidence" / f"{evidence_id}.json"
                    if evidence_file.exists():
                        try:
                            with open(evidence_file, "r", encoding="utf-8") as f:
                                evidence = json.load(f)

                            # 提取内容
                            content = evidence.get("content", "")
                            if content:
                                quotes.append(content)

                            # 提取来源信息（使用第一个证据的来源）
                            if not source_note_id:
                                source_note_id = evidence.get("note_id", "")
                                source_note_url = evidence.get("note_url", "")
                                source_title = evidence.get("note_title", "无标题")
                        except Exception as e:
                            logger.warning(f"[Workflow] 加载证据失败: {evidence_id}, error={e}")

                if quotes and source_note_url:
                    references.append({
                        "topic": cluster_topic,
                        "sentiment": cluster_sentiment,
                        "source_note_url": source_note_url,
                        "source_title": source_title,
                        "quotes": quotes
                    })
                    logger.info(
                        f"[Workflow] 生成 reference: cluster={cluster_topic}, "
                        f"title={source_title}, quotes={len(quotes)}"
                    )

            # 注入到 state，供 Synthesis Agent 使用
            state["references"] = references
            logger.info(f"[Workflow] 生成了 {len(references)} 个 references")
            state["retrieved_comments"] = []
            state["screened_items"] = []
            state["retrieved_posts"] = []

            # 跳过 Retrieve/Screen/Analyze，显示进度
            _progress(queue, "retrieve", "复用历史记忆，跳过爬取", 50)
            _progress(queue, "screen", "复用历史记忆，跳过筛选", 60)
            _progress(queue, "analyze", "复用历史记忆，跳过聚类", 70)

        else:
            # 增量更新或从头开始：执行 Retrieve/Screen/Analyze
            api_type = int(os.getenv("XHS_API_TYPE", "2"))  # 默认为 2

            # ── 2. Retrieve Subgraph ──
            if reuse_decision and reuse_decision.reuse_strategy == "incremental":
                # 增量模式：缩减爬取量
                target_posts = max(3, int(7 * (1 - reuse_decision.coverage_ratio * 0.7)))
                state["_target_posts"] = target_posts
                logger.info(f"[Workflow] 增量更新模式：目标 {target_posts} 篇帖子")
                _progress(queue, "retrieve", f"增量模式：爬取 {target_posts} 篇帖子", 25)
            else:
                # 从头开始：正常流程
                # 注：不设置 state["_target_posts"]，由 retrieve_agent 使用默认值 _MIN_POSTS=7
                _progress(queue, "retrieve", "正在检索相关帖子...", 25)

            # 执行检索（固定使用 1 个连接，避免并发爬取导致封号）
            # 传入 api_type：当 api_type=1 时跳过详情拉取，后续用 apihz.cn 补全
            async with XhsMcpClientPool(size=1, cookie=cookie) as retrieve_pool:
                config = {"configurable": {"pool": retrieve_pool, "queue": queue, "api_type": api_type}}
                retrieve_output = await _retrieve_app.ainvoke(state, config=config)
            state = {**state, **retrieve_output}
            logger.info(
                f"[Workflow][Retrieve] finished: posts={len(state.get('retrieved_posts', []))}, "
                f"attempts={state.get('search_attempts', 0)}, "
                f"coverage={state.get('retrieval_coverage_score', 0.0):.2f}"
            )
            _progress(queue, "retrieve", f"检索到 {len(state.get('retrieved_posts', []))} 篇帖子", 28)

            # ── apihz.cn 补全正文（仅 api_type=1，在 Screen 之前！）──
            if api_type == 1:
                _progress(queue, "retrieve", "正在获取完整帖子正文...", 30)
                try:
                    from app.tools.xhs_apihz import fetch_posts_detail_batch, is_apihz_configured

                    if not is_apihz_configured():
                        logger.warning("[Workflow] apihz.cn 未配置，使用截断的帖子正文")
                    else:
                        retrieved_posts = state.get("retrieved_posts", [])
                        note_urls = [p.get("note_url", "") for p in retrieved_posts]
                        full_details = await fetch_posts_detail_batch(note_urls)

                        # 更新 retrieved_posts 中的 desc 为完整正文
                        detail_map = {d.get("note_id"): d for d in full_details if d.get("note_id")}
                        for post in retrieved_posts:
                            note_id = post.get("note_id")
                            if note_id in detail_map:
                                post["desc"] = detail_map[note_id].get("desc", post.get("desc", ""))
                                post["title"] = detail_map[note_id].get("title", post.get("title", ""))

                        logger.info(f"[Workflow] apihz.cn 获取了 {len(full_details)} 篇帖子的完整正文")
                except Exception as e:
                    logger.warning(f"[Workflow] apihz.cn 调用失败: {e}，使用截断的帖子正文")

            # ── 3. Screen Subgraph ──
            _progress(queue, "screen", "正在筛选相关帖子（过滤广告/软广）...", 35)
            screen_output = await _screen_app.ainvoke(state)
            state = {**state, **screen_output}

            screened = state.get("screened_items", [])
            if not screened:
                raise RuntimeError("筛选后无相关帖子，请尝试更换关键词")

            # 构建详细的筛选消息
            stats = state.get("screening_stats", {})
            rejected_ad = stats.get("rejected_ad", 0)
            rejected_brand = stats.get("rejected_brand", 0)
            rejected_contact = stats.get("rejected_contact", 0)
            rejected_low_relevance = stats.get("rejected_low_relevance", 0)

            screen_msg = f"筛选出 {len(screened)} 篇相关帖子"
            exclude_details = []
            if rejected_ad > 0:
                exclude_details.append(f"{rejected_ad} 篇广告/软广")
            if rejected_brand > 0:
                exclude_details.append(f"{rejected_brand} 篇品牌号")
            if rejected_contact > 0:
                exclude_details.append(f"{rejected_contact} 篇含联系方式")
            if rejected_low_relevance > 0:
                exclude_details.append(f"{rejected_low_relevance} 篇相关性不足")
            if exclude_details:
                screen_msg += f"，排除 {'、'.join(exclude_details)}"

            _progress(queue, "screen", screen_msg, 38)
            logger.info(f"[Workflow][Screen] {screen_msg}")

            # ── 4. Analyze ──
            # 注意：api_type=1 时，正文已在 Retrieve 后通过 apihz.cn 补全
            if api_type == 1:
                # 跳过评论爬取模式：直接使用帖子正文进行聚类
                _progress(queue, "analyze", "使用帖子正文进行观点分析...", 56)
                logger.info(f"[Workflow][Analyze] api_type=1: 使用 {len(screened)} 篇帖子正文进行聚类")
                config = {"configurable": {"queue": queue, "api_type": 1}}
                analyze_output = await _analyze_app.ainvoke(state, config=config)
            else:
                # 正常模式：爬取评论
                _progress(queue, "analyze", "正在获取评论并分析舆情（最长约 1 分钟）...", 56)
                pool_size = int(os.getenv("MCP_POOL_SIZE", "1"))
                pool_size = max(1, min(pool_size, len(state.get("screened_items", [])) or 1))
                async with XhsMcpClientPool(size=pool_size, cookie=cookie) as pool:
                    config = {"configurable": {"pool": pool, "queue": queue, "api_type": 2}}
                    analyze_output = await _analyze_app.ainvoke(state, config=config)
            state = {**state, **analyze_output}

            # 构建详细的分析消息
            comment_count = len(state.get("retrieved_comments", []))
            cluster_count = len(state.get("clusters", []))
            filtered_comment_count = state.get("_filtered_comment_count", 0)

            if api_type == 1:
                analyze_msg = f"已分析 {comment_count} 篇帖子正文，生成 {cluster_count} 个观点簇"
            else:
                analyze_msg = f"已分析 {comment_count} 条评论，生成 {cluster_count} 个观点簇"
            if filtered_comment_count > 0:
                analyze_msg += f"，过滤 {filtered_comment_count} 条无效评论"

            _progress(queue, "analyze", analyze_msg, 78)
            logger.info(f"[Workflow][Analyze] {analyze_msg}")

        # ── 5. Synthesize (Plan and Execute 架构)
        _progress(queue, "synthesize", "正在制定报告大纲与生成分析报告...", 82)
        config = {"configurable": {"queue": queue}}
        synthesis_output = await _synthesis_app.ainvoke(state, config=config)

        state = {**state, **synthesis_output}
        _progress(queue, "synthesize", "报告生成完毕", 97)

        # ── 推送最终结果（references 由 synthesis_agent 生成）
        queue.put_nowait({
            "event": "result",
            "data": {
                "final_answer": state.get("final_answer", ""),
                "confidence_score": state.get("confidence_score", 0.0),
                "clusters": state.get("clusters", []),
                "sentiment_summary": state.get("sentiment_summary", {}),
                "screened_count": len(state.get("screened_items", [])),
                "comment_count": len(state.get("retrieved_comments", [])),
                "limitations": state.get("limitations", []),
                "intent": state.get("intent", "general"),
                "query_plan": state.get("query_plan", []),
                "references": state.get("references", []),
            },
        })
        append_audit_log(
            "analysis_result",
            run_id=run_id,
            query=query,
            status="success",
            intent=state.get("intent", "general"),
            query_plan=state.get("query_plan", []),
            retrieved_post_count=len(state.get("retrieved_posts", [])),
            screened_count=len(state.get("screened_items", [])),
            comment_count=len(state.get("retrieved_comments", [])),
            cluster_count=len(state.get("clusters", [])),
            confidence_score=state.get("confidence_score", 0.0),
            limitations=state.get("limitations", []),
        )

    except BaseException as e:
        if isinstance(e, asyncio.CancelledError):
            raise
        exc_type = type(e).__name__
        exc_msg = repr(e)
        if "COOKIE_EXPIRED" in str(e):
            queue.put_nowait({
                "event": "error",
                "data": {"code": "COOKIE_EXPIRED", "message": "小红书 Cookie 已过期，请重新配置"},
            })
        else:
            try:
                tb_text = traceback.format_exc() or ""
                if tb_text.strip() and "NoneType: None" not in tb_text:
                    message = tb_text.strip()
                else:
                    message = f"[{exc_type}] {exc_msg}"
            except Exception:
                message = f"[{exc_type}] {exc_msg}"
            if not message:
                message = f"[{exc_type}] (no details)"
            print(f"[WORKFLOW EXCEPT] {message}", file=sys.stderr, flush=True)
            logger.error(f"[Workflow] run_id={run_id} FAILED: {message}")
            append_audit_log(
                "analysis_workflow_failed",
                run_id=run_id,
                query=query,
                status="failed",
                error_message=message,
                retrieved_post_count=len(state.get("retrieved_posts", [])),
                screened_count=len(state.get("screened_items", [])),
                comment_count=len(state.get("retrieved_comments", [])),
                cluster_count=len(state.get("clusters", [])),
            )
            queue.put_nowait({
                "event": "error",
                "data": {"code": "ANALYSIS_FAILED", "message": message},
            })
        if not isinstance(e, Exception):
            raise
    finally:
        # ── 在 finally 中保存证据（即使任务被取消也会执行）──
        if enable_memory and state.get("product_entities"):
            try:
                from app.memory import get_memory_manager, get_evidence_saver

                entity = state.get("product_entities", [""])[0]
                intent = state.get("intent", "general")
                clusters = state.get("clusters", [])
                screened_items = state.get("screened_items", [])
                retrieved_comments = state.get("retrieved_comments", [])
                reuse_strategy = state.get("_reuse_strategy", "none")

                # 调用记忆集成（异步保存证据）
                memory_manager = get_memory_manager()

                # 异步保存证据（使用全局线程池）
                evidence_saver = get_evidence_saver()
                await evidence_saver.save_evidence_async(
                    entity=entity,
                    screened_items=screened_items,
                    clusters=clusters,
                    retrieved_comments=retrieved_comments
                )

                # 立即更新记忆（不等证据保存完成）
                memory_manager.ingest_analysis_result(
                    entity=entity,
                    clusters=clusters,
                    screened_items=screened_items,
                    retrieved_comments=retrieved_comments,
                    query=query,
                    intent=intent,
                    request_id=run_id,
                    reuse_strategy=reuse_strategy,
                    skip_evidence_save=True  # NEW: 跳过同步保存，使用异步保存
                )

                logger.info(f"[Workflow][Memory] 记忆集成完成: entity={entity}")

            except asyncio.CancelledError:
                logger.warning(f"[Workflow] 证据保存被取消")
            except Exception as e:
                logger.warning(f"[Workflow][Memory] 记忆集成失败: {e}")

        # ── 更新短期会话记忆 ──
        if enable_memory and session_id and state.get("product_entities"):
            try:
                session_manager = get_session_manager()
                session_manager.update_session(
                    session_id=session_id,
                    query=query,
                    entity=state.get("product_entities", [""])[0],
                    intent=state.get("intent", "general"),
                    note_ids=[p.get("note_id", "") for p in state.get("screened_items", [])],
                    clusters=state.get("clusters", [])
                )
                logger.info(f"[Workflow][Memory] 已更新短期会话记忆: session_id={session_id}")
            except Exception as e:
                logger.warning(f"[Workflow][Memory] 更新会话记忆失败: {e}")

        # ── 清理未完成的 asyncio 任务 ──
        try:
            tasks = asyncio.all_tasks()
            current_task = asyncio.current_task()

            cancelled_count = 0
            for task in tasks:
                if task is not current_task and not task.done():
                    task.cancel()
                    cancelled_count += 1

            if cancelled_count > 0:
                logger.info(f"[Workflow] 取消了 {cancelled_count} 个未完成的 asyncio 任务")
        except Exception as e:
            logger.warning(f"[Workflow] 清理任务失败: {e}")

        queue.put_nowait(None)  # 哨兵：通知 SSE 生成器流结束
