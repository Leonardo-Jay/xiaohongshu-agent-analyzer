"""Synthesis Subgraph — 报告生成与质量把控的 Plan and Execute 架构
职责：接收观点聚类等数据，先规划大纲，经过自我审查合格后，严格依大纲流式输出报告，最后根据加权规则进行总评得分。

核心流程：
  1. plan_outline: 分析数据体量，利用 LLM 制定细化的撰写格式和章节大纲 (JSON)。如果数据太空则跳过大纲。
  2. observe_outline: (护栏节点) 扫描大纲，防止大纲漏掉重要簇，或幻觉造出不存在的簇。不合格则退回重写（最多2次）。
  3. execute_report: 主笔节点。拿着质检合格的大纲 JSON 和具体数据，流式生成 Markdown 报告并向队列推送 `report_chunk`。
  4. evaluate_and_score: 纯规则计算置信度（数据量、多样性、情绪对抗、引用情况）40/30/15/15 权重，汇编 references 清单。
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from loguru import logger

from app.models.schemas import GraphState
from app.prompts.templates import SYNTHESIS_PLAN_OUTLINE_PROMPT, SYNTHESIS_MODIFY_OUTLINE_PROMPT, SYNTHESIS_REPORT_PROMPT
from app.tools.llm import create_llm

# 规划用严谨模型，生成报告用略带感情色彩的模型
_llm_plan = create_llm(temperature=0.1)
_llm_report = create_llm(temperature=0.3)

_MAX_SYNTHESIS_ROUNDS = 2

def _strip_fences(text: str) -> str:
    """去除 LLM 报告输出中可能加的 markdown/代码围栏。"""
    text = re.sub(r'^```(?:markdown)?\s*', '', text.strip(), flags=re.MULTILINE)
    return re.sub(r'```\s*$', '', text.strip(), flags=re.MULTILINE).strip()

def _parse_json_response(text: str) -> dict:
    """安全地解析可能会带有包裹的 LLM JSON 输出"""
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    text = re.sub(r'```\s*$', '', text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception as e:
        logger.warning(f"[Synthesis] JSON 解析失败，尝试修复: {e}")
        # 如果彻底失败，返回一个保底的最简单的大纲结构
        return {
            "report_strategy": {
                "overall_tone": "客观中立",
                "structure": [{"chapter": "综合分析报告", "focus": "对搜索到的数据进行简单汇总概括", "use_clusters": []}]
            }
        }


async def node_plan_outline(state: GraphState) -> dict[str, Any]:
    """Plan 节点：分析数据特点，利用 LLM 生成报告撰写策略与大纲"""
    clusters = state.get("clusters", [])
    post_count = len(state.get("screened_items", []))
    comment_count = len(state.get("retrieved_comments", []))
    sentiment_summary = state.get("sentiment_summary", {})

    round_num = state.get("_synthesis_round", 0) + 1
    feedback = state.get("_outline_feedback", "")

    # 【逃生舱】如果根本没数据，直接标记结束，不走大纲规划，给一个默认的最简框架
    if not clusters and post_count == 0:
        logger.info("[Synthesis][Plan] 无可用数据，生成默认空大纲跳过审查")
        return {
            "_synthesis_round": round_num,
            "_synthesis_done": True,
            "_report_outline": {
                "report_strategy": {
                    "overall_tone": "无数据",
                    "structure": [{"chapter": "搜索为空", "focus": "提醒用户更换关键词重新搜索", "use_clusters": []}]
                }
            }
        }

    # 给 cluster 加上 #0, #1 编号供大纲引用
    labeled_clusters = [{"id": i, **cl} for i, cl in enumerate(clusters)]

    # 根据是否有反馈，选择不同的 Prompt
    feedback = state.get("_outline_feedback", "")
    if feedback:
        # 修改模式：传递原大纲 + 反馈 + 修改原则
        previous_outline = state.get("_report_outline", {})
        prompt = SYNTHESIS_MODIFY_OUTLINE_PROMPT.format(
            previous_outline_json=json.dumps(previous_outline, ensure_ascii=False),
            feedback=feedback
        )
        logger.info(f"[Synthesis][Plan] Round {round_num}: 根据反馈修改大纲...")
    else:
        # 初始生成模式：简洁的 Prompt
        prompt = SYNTHESIS_PLAN_OUTLINE_PROMPT.format(
            query=state.get("user_query_raw", ""),
            post_count=post_count,
            comment_count=comment_count,
            sentiment_summary=json.dumps(sentiment_summary, ensure_ascii=False),
            numbered_clusters_json=json.dumps(labeled_clusters, ensure_ascii=False)
        )
        logger.info(f"[Synthesis][Plan] Round {round_num}: 开始起草报告大纲...")

    try:
        resp = await _llm_plan.ainvoke(prompt)
        outline_json = _parse_json_response(resp.content)
    except Exception as e:
        logger.error(f"[Synthesis][Plan] Error: {e}")
        outline_json = _parse_json_response("")

    return {
        "_synthesis_round": round_num,
        "_report_outline": outline_json,
        "_synthesis_done": False  # 等待审查
    }


async def node_observe_outline(state: GraphState) -> dict[str, Any]:
    """Observe 节点：检查大纲质量，标记正确和错误的章节"""
    round_num = state.get("_synthesis_round", 1)
    # 如果已经达到重试上限，或者上一节点由于完全没数据主动挂旗跳过，直接放行
    if state.get("_synthesis_done", False) or round_num >= _MAX_SYNTHESIS_ROUNDS:
        logger.info(f"[Synthesis][Observe] 放行（已达终点或最大轮次 {round_num}）")
        return {"_synthesis_done": True, "_outline_feedback": ""}

    outline = state.get("_report_outline", {})
    clusters = state.get("clusters", [])

    structure = outline.get("report_strategy", {}).get("structure", [])
    if not structure:
        return {"_synthesis_done": False, "_outline_feedback": "大纲中缺少 structure 结构部分。"}

    # ── 标记章节状态 ──
    chapter_issues = {}  # {章节名: [问题列表]}
    correct_chapters = []  # 正确的章节列表

    # 提取所有被引用的 index
    referenced_indices = set()
    for i, chap in enumerate(structure):
        for idx in chap.get("use_clusters", []):
            if isinstance(idx, int):
                referenced_indices.add(idx)

    max_idx = len(clusters) - 1

    # 审查每个章节
    for i, chap in enumerate(structure):
        chap_name = chap.get("chapter", f"章节{i+1}")
        issues = []

        # 检查 1: 幻觉防范
        for idx in chap.get("use_clusters", []):
            if not isinstance(idx, int) or idx < 0 or idx > max_idx:
                issues.append(f"引用了不存在的簇编号 #{idx}（最大编号为 #{max_idx}）")

        if issues:
            chapter_issues[chap_name] = issues
        else:
            correct_chapters.append(chap_name)

    # 检查 2: 漏审防范（全局问题，不标记到具体章节）
    missing_clusters = []
    for i, cl in enumerate(clusters):
        if cl.get("count", 1) >= 2 and i not in referenced_indices:
            topic = cl.get("topic", f"簇#{i}")
            missing_clusters.append(f"#{i} {topic}（出现{cl.get('count')}次）")

    # ── 构建结构化反馈 ──
    if not chapter_issues and not missing_clusters:
        logger.info("[Synthesis][Observe] 大纲审查通过")
        return {"_synthesis_done": True, "_outline_feedback": ""}

    # 构建反馈文本
    feedback_parts = []

    # 1. 正确的章节（保留）
    if correct_chapters:
        feedback_parts.append(f"【保留章节】以下章节无需修改：\n" + "\n".join([f"  - {name}" for name in correct_chapters]))

    # 2. 有问题的章节（需修改）
    if chapter_issues:
        feedback_parts.append("【需修改章节】以下章节存在问题：")
        for chap_name, issues in chapter_issues.items():
            feedback_parts.append(f"  - 「{chap_name}」：" + "；".join(issues))

    # 3. 遗漏的观点（需补充）
    if missing_clusters:
        feedback_parts.append(f"【遗漏观点】以下重要观点未被引用，请补充到合适章节：\n" + "\n".join([f"  - {cl}" for cl in missing_clusters]))

    feedback_text = "\n\n".join(feedback_parts)
    logger.warning(f"[Synthesis][Observe] 大纲被驳回，发现 {len(chapter_issues)} 个问题章节，{len(missing_clusters)} 个遗漏观点")

    return {"_synthesis_done": False, "_outline_feedback": feedback_text}


def _route_synthesis(state: GraphState) -> Literal["execute_report", "plan_outline"]:
    """通过审查或者超限，转去撰写。否则退回重写。"""
    if state.get("_synthesis_done", False):
        return "execute_report"
    return "plan_outline"


async def node_execute_report(state: GraphState, config: dict) -> dict[str, Any]:
    """Execute 节点：主笔撰写。拿上大纲照着执行，通过流输出推向前端。"""
    queue = config.get("configurable", {}).get("queue")
    outline = state.get("_report_outline", {})
    clusters = state.get("clusters", [])
    post_count = len(state.get("screened_items", []))
    comment_count = len(state.get("retrieved_comments", []))

    if not clusters and post_count == 0:
        logger.info("[Synthesis][Execute] 无数据场景快速返回")
        ans = "## 舆情分析结果\n\n未找到与查询相关且有效的内容，请尝试更换关键词搜索。"
        if queue:
            queue.put_nowait({"event": "report_chunk", "data": {"text": ans}})
        return {"final_answer": ans}

    clusters_json = json.dumps(clusters, ensure_ascii=False)
    outline_fmt = json.dumps(outline, ensure_ascii=False, indent=2)

    prompt = SYNTHESIS_REPORT_PROMPT.format(
        query=state.get("user_query_raw", ""),
        post_count=post_count,
        comment_count=comment_count,
        report_outline=outline_fmt,
        clusters_json=clusters_json
    )
    logger.info(f"[Synthesis][Execute] 正在发出的最终提示词 (前500字): \n{prompt[:500]}...")

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            buffer = ""
            async for chunk in _llm_report.astream(prompt):
                buffer += chunk
                cleaned = _strip_fences(buffer)
                if queue:
                    queue.put_nowait({
                        "event": "report_chunk",
                        "data": {"text": cleaned},
                    })

            final_answer = _strip_fences(buffer)
            if not final_answer:
                final_answer = "## 分析完成\n\n报告内容生成失败，请重试。"

            logger.info(f"[Synthesis][Execute] 报告生成完毕，总字数 {len(final_answer)}。")
            return {"final_answer": final_answer}

        except Exception as e:
            logger.error(f"[Synthesis][Execute] 生成中途断流，第 {attempt+1} 次失败: {e}")
            import asyncio
            if attempt < max_retries:
                if queue:
                    queue.put_nowait({"event": "progress", "data": {"message": f"遭遇网络波动重试 ({attempt+1}/{max_retries})", "progress": 82}})
                await asyncio.sleep(2)
            else:
                ans = f"## 分析异常中断\n\n多次与底层大模型通信阻断，生成发生意外，请稍后再试。错误:{str(e)}"
                if queue:
                    queue.put_nowait({"event": "progress", "data": {"message": "流式生成失败", "progress": 82}})
                return {"final_answer": ans}

    return {} # Unreachable


async def node_evaluate_and_score(state: GraphState) -> dict[str, Any]:
    """Score 节点：抛弃 LLM，利用强规则进行增强型 40/30/15/15 加权打分，并梳理所有 references。"""
    post_count = len(state.get("screened_items", []))
    comment_count = len(state.get("retrieved_comments", []))
    clusters = state.get("clusters", [])
    sentiment_summary = state.get("sentiment_summary", {})
    limitations = []

    # 1. 维度1：数据底座（权重 40%）
    if post_count >= 8 and comment_count >= 30: data_score = 1.0
    elif post_count >= 5 and comment_count >= 20: data_score = 0.8
    elif post_count >= 3 and comment_count >= 10: data_score = 0.6
    elif post_count >= 2 and comment_count >= 5: data_score = 0.4
    elif post_count >= 1: data_score = 0.3
    else:
        data_score = 0.1
        limitations.append("极其缺乏有效的样本数据量")

    # 2. 维度2：观点丰富/发散度（权重 30%）
    cluster_count = len(clusters)
    if cluster_count >= 6: div_score = 1.0
    elif cluster_count >= 4: div_score = 0.8
    elif cluster_count >= 3: div_score = 0.6
    elif cluster_count >= 2: div_score = 0.4
    else:
        div_score = 0.2
        limitations.append("聚类出的痛点维度单一，不足刻画全貌")

    # 3. 维度3：情绪对抗度 / 辩证性（权重 15%）
    sentiments = set(sentiment_summary.keys())
    # 清洗掉一些可能奇怪的空格
    sentiments = {s.strip() for s in sentiments}
    if "正面" in sentiments and "负面" in sentiments:
        sent_score = 1.0
    elif len(sentiments) >= 2:
        sent_score = 0.7
    elif len(sentiments) == 1:
        sent_score = 0.4
        limitations.append("舆情面貌一边倒，缺乏对比和制衡性的反面视角")
    else:
        sent_score = 0.2

    # 4. 维度4：证据引用坚实度（权重 15%）
    total_quotes = sum(len(c.get("evidence_quotes", [])) for c in clusters)
    if total_quotes >= 10: ev_score = 1.0
    elif total_quotes >= 5: ev_score = 0.7
    elif total_quotes >= 2: ev_score = 0.4
    else:
        ev_score = 0.2
        limitations.append("报告由于缺乏来自用户端的原话佐证说服力受限")

    final_score = (data_score * 0.40) + (div_score * 0.30) + (sent_score * 0.15) + (ev_score * 0.15)
    final_score = round(final_score, 2)

    # 构建结构化的前端 references 下拉列表
    seen: set[str] = set()
    references: list[dict] = []
    for cl in clusters:
        url = cl.get("source_note_url", "")
        if not url: continue
        key = f"{url}|{cl.get('topic', '')}"
        if key in seen: continue
        seen.add(key)
        references.append({
            "topic": cl.get("topic", ""),
            "sentiment": cl.get("sentiment", "中立"),
            "source_note_url": url,
            "source_title": cl.get("source_title", "无标题"),
            "quotes": cl.get("evidence_quotes", []),
        })

    logger.info(f"[Synthesis][Score] 加权总得分={final_score} [Data:{data_score}, Div:{div_score}, Sent:{sent_score}, Evid:{ev_score}]")
    return {
        "confidence_score": final_score,
        "limitations": limitations,
        "references": references
    }


def build_synthesis_graph():
    """构建 Synthesis 子图

    流程：
      plan_outline (写大纲) -> observe_outline (质检大纲)
          -> { if 不合格 -> 回炉重写 / if 合格 -> } execute_report (一气呵成流式生成出文)
          -> evaluate_and_score (裁判员进行四维度算分和整理引用) -> END
    """
    from langgraph.graph import StateGraph

    g = StateGraph(GraphState)

    g.add_node("plan_outline", node_plan_outline)
    g.add_node("observe_outline", node_observe_outline)
    g.add_node("execute_report", node_execute_report)
    g.add_node("evaluate_and_score", node_evaluate_and_score)

    g.set_entry_point("plan_outline")
    g.add_edge("plan_outline", "observe_outline")

    # 只有通过了规则检验才能去一门心思地去写文，防止烂提纲和发散幻觉
    g.add_conditional_edges(
        "observe_outline",
        _route_synthesis,
        {
            "execute_report": "execute_report",
            "plan_outline": "plan_outline"
        }
    )

    g.add_edge("execute_report", "evaluate_and_score")
    g.add_edge("evaluate_and_score", "__end__")

    return g.compile()
