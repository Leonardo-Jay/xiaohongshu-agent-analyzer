"""
实体记忆聚合模块

负责：
1. 加载/保存实体记忆
2. 更新观点簇（带证据引用）
3. 检测矛盾和趋势
4. 知识累积
"""
import json
import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.memory.memory_types import EntityMemory, ConsensusCluster, Contradiction, QueryRecord
from app.memory.evidence_saver import get_evidence_saver
from app.memory.contradiction_detector import get_contradiction_detector
from app.memory.trend_calculator import get_trend_calculator
from app.memory.concept_memory import get_concept_memory


class MemoryManager:
    """实体记忆管理器"""

    def __init__(self, base_dir: str | None = None):
        self._base_dir = Path(base_dir) if base_dir else Path(__file__).parent.parent.parent / "data" / "memory"
        self._entities_dir = self._base_dir / "entities"
        self._entities_dir.mkdir(parents=True, exist_ok=True)

    def ingest_analysis_result(
        self,
        entity: str,
        clusters: list[dict],
        screened_items: list[dict],
        retrieved_comments: list[dict],
        query: str,
        intent: str,
        request_id: str,
        reuse_strategy: str = "none",  # 复用策略
        skip_evidence_save: bool = False,  # 是否跳过证据保存
        cluster_to_evidence: dict[str, list[str]] | None = None,
    ) -> None:
        """
        将分析结果集成到记忆库（Ingest 操作）

        流程：
        1. 保存原始证据（帖子+评论）
        2. 加载现有记忆
        3. 更新观点簇（带证据引用）
        4. 检测矛盾
        5. 计算趋势
        6. 保存记忆
        """
        if not entity:
            logger.warning("[MemoryManager] 无实体，跳过记忆集成")
            return

        # 1. 加载现有记忆
        memory = self.load_entity_memory(entity)

        # 2. 根据复用策略调整更新逻辑
        if reuse_strategy == "full":
            # 完全复用：只更新查询记录
            memory.last_analyzed = datetime.now().isoformat()
            memory.recent_queries.append(QueryRecord(
                query=query,
                intent=intent,
                timestamp=datetime.now().isoformat(),
                request_id=request_id
            ))
            if len(memory.recent_queries) > 10:
                memory.recent_queries = memory.recent_queries[-10:]
            logger.info(f"[MemoryManager] 完全复用模式：仅更新查询记录")

        elif reuse_strategy == "incremental":
            # 增量更新：合并新旧观点簇
            # 保存证据
            cluster_to_evidence = cluster_to_evidence or {}
            if not skip_evidence_save:
                evidence_saver = get_evidence_saver()
                evidence_result = evidence_saver.save_evidence_batch(
                    entity,
                    screened_items,
                    clusters,
                    retrieved_comments
                )
                cluster_to_evidence = evidence_result.get("cluster_to_evidence", {})

            # 更新记忆（增量合并）
            self._update_memory(memory, clusters, cluster_to_evidence, query, intent, request_id)

            # 更新概念记忆
            concept_memory = get_concept_memory()
            concept_memory.update_concepts_from_clusters(entity, clusters)

            logger.info(f"[MemoryManager] 增量更新模式：合并新旧观点簇")

        else:
            # 全新分析：正常更新
            # 保存证据
            cluster_to_evidence = cluster_to_evidence or {}
            if not skip_evidence_save:
                evidence_saver = get_evidence_saver()
                evidence_result = evidence_saver.save_evidence_batch(
                    entity,
                    screened_items,
                    clusters,
                    retrieved_comments
                )
                cluster_to_evidence = evidence_result.get("cluster_to_evidence", {})

            # 更新记忆
            self._update_memory(memory, clusters, cluster_to_evidence, query, intent, request_id)

            # 更新概念记忆
            concept_memory = get_concept_memory()
            concept_memory.update_concepts_from_clusters(entity, clusters)

            logger.info(f"[MemoryManager] 全新分析模式：正常更新记忆")

        # 3. 保存记忆
        self.save_entity_memory(entity, memory)

    def load_entity_memory(self, entity: str) -> EntityMemory:
        """加载实体记忆（支持模糊匹配）"""
        # 尝试直接匹配
        memory_file = self._entities_dir / entity / "memory.json"

        if memory_file.exists():
            try:
                with open(memory_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return EntityMemory.from_dict(data)
            except Exception as e:
                logger.warning(f"[MemoryManager] 加载记忆失败: {e}, 创建新记忆")
                return EntityMemory(
                    entity=entity,
                    first_analyzed=datetime.now().isoformat()
                )

        # 尝试模糊匹配已有实体目录
        normalized_entity = entity.replace(" ", "").lower()
        for entity_dir in self._entities_dir.iterdir():
            if not entity_dir.is_dir():
                continue
            normalized_dir_name = entity_dir.name.replace(" ", "").lower()
            if normalized_dir_name == normalized_entity:
                memory_file = entity_dir / "memory.json"
                try:
                    with open(memory_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    logger.info(f"[MemoryManager] 模糊匹配实体: {entity} -> {entity_dir.name}")
                    return EntityMemory.from_dict(data)
                except Exception as e:
                    logger.warning(f"[MemoryManager] 加载记忆失败: {e}, 创建新记忆")
                    return EntityMemory(
                        entity=entity_dir.name,
                        first_analyzed=datetime.now().isoformat()
                    )

        # 未找到匹配，创建新记忆
        return EntityMemory(
            entity=entity,
            first_analyzed=datetime.now().isoformat()
        )

    def save_entity_memory(self, entity: str, memory: EntityMemory) -> None:
        """保存实体记忆（支持模糊匹配已有实体）"""
        # 尝试模糊匹配已有实体目录
        normalized_entity = entity.replace(" ", "").lower()
        matched_entity = None

        for entity_dir in self._entities_dir.iterdir():
            if not entity_dir.is_dir():
                continue
            normalized_dir_name = entity_dir.name.replace(" ", "").lower()
            if normalized_dir_name == normalized_entity:
                matched_entity = entity_dir.name
                logger.info(f"[MemoryManager] 匹配已有实体目录: {entity} -> {matched_entity}")
                break

        # 使用匹配到的实体名或原名
        final_entity = matched_entity if matched_entity else entity
        memory_file = self._entities_dir / final_entity / "memory.json"
        memory_file.parent.mkdir(parents=True, exist_ok=True)

        # 更新 memory 对象的 entity 字段
        memory.entity = final_entity

        with open(memory_file, "w", encoding="utf-8") as f:
            json.dump(memory.to_dict(), f, ensure_ascii=False, indent=2)

        logger.info(f"[MemoryManager] 保存记忆: {memory_file}")

    def save_run_snapshot(
        self,
        entity: str,
        request_id: str,
        query: str,
        intent_frame: dict[str, Any],
        clusters: list[dict],
        screened_items: list[dict],
        retrieved_comments: list[dict],
        final_answer: str,
        report_outline: dict[str, Any] | None = None,
        reuse_strategy: str = "none",
        cluster_to_evidence: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """
        保存单次分析的不可变 run 快照。

        现有长期记忆结构保持不变：
        - memory.json 仍保存实体聚合记忆
        - evidence/ 仍保存证据
        - runs/ 只新增每次分析的快照，供短期记忆通过 run_ref 精确回指
        """
        if not entity:
            return {}

        final_entity = self._resolve_entity_name(entity)
        runs_dir = self._entities_dir / final_entity / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now().isoformat()
        safe_request_id = re.sub(r"[^A-Za-z0-9_-]", "", request_id or "")[:12] or "request"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        run_id = f"run_{timestamp}_{safe_request_id}"

        cluster_to_evidence = cluster_to_evidence or {}
        memory = self.load_entity_memory(final_entity)
        memory_clusters_by_topic = {
            cluster.topic: cluster
            for cluster in memory.consensus_clusters
        }

        snapshot_clusters = []
        for idx, cluster in enumerate(clusters):
            topic = cluster.get("topic", "")
            memory_cluster = memory_clusters_by_topic.get(topic)

            evidence_ids = (
                cluster.get("evidence_ids")
                or cluster_to_evidence.get(f"cluster_{idx}")
                or (memory_cluster.evidence_ids if memory_cluster else [])
                or []
            )
            evidence_ids = self._unique(evidence_ids)

            snapshot_clusters.append({
                "cluster_id": cluster.get("cluster_id") or (memory_cluster.cluster_id if memory_cluster else self._make_cluster_id(topic)),
                "topic": topic,
                "sentiment": cluster.get("sentiment", ""),
                "count": cluster.get("count", cluster.get("avg_count", 0)),
                "primary_aspects": cluster.get("primary_aspects", []) or (memory_cluster.primary_aspects if memory_cluster else []),
                "sub_aspects": cluster.get("sub_aspects", []) or (memory_cluster.sub_aspects if memory_cluster else []),
                "synonym_aspects": cluster.get("synonym_aspects", []) or (memory_cluster.synonym_aspects if memory_cluster else []),
                "evidence_ids": evidence_ids,
                "trend": cluster.get("trend", memory_cluster.trend if memory_cluster else "new"),
            })

        note_ids = self._unique([
            item.get("note_id", "")
            for item in screened_items
            if item.get("note_id")
        ])

        snapshot = {
            "run_id": run_id,
            "request_id": request_id,
            "query": query,
            "analyzed_at": now,
            "entity": final_entity,
            "reuse_strategy": reuse_strategy,
            "intent_frame": intent_frame,
            "data_scope": {
                "post_count": len(screened_items),
                "comment_count": len(retrieved_comments),
                "note_ids": note_ids,
            },
            "clusters": snapshot_clusters,
            "report": {
                "outline": report_outline or {},
                "final_answer_digest": (final_answer or "")[:500],
            },
        }

        snapshot_file = runs_dir / f"{run_id}.json"
        with open(snapshot_file, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        rel_snapshot_path = snapshot_file.relative_to(self._base_dir).as_posix()
        rel_memory_path = (self._entities_dir / final_entity / "memory.json").relative_to(self._base_dir).as_posix()
        run_ref = {
            "entity": final_entity,
            "run_id": run_id,
            "snapshot_path": rel_snapshot_path,
            "memory_file": rel_memory_path,
            "committed_at": now,
        }
        logger.info(f"[MemoryManager] 保存 run 快照: {snapshot_file}")
        return run_ref

    def _update_memory(
        self,
        memory: EntityMemory,
        clusters: list[dict],
        cluster_to_evidence: dict[str, list[str]],
        query: str,
        intent: str,
        request_id: str
    ) -> None:
        """更新记忆"""
        now = datetime.now().isoformat()
        # 更新基本信息
        memory.last_analyzed = now
        memory.total_analyses += 1

        # 更新查询记录
        memory.recent_queries.append(QueryRecord(
            query=query,
            intent=intent,
            timestamp=now,
            request_id=request_id
        ))

        # 保留最近 10 次查询
        if len(memory.recent_queries) > 10:
            memory.recent_queries = memory.recent_queries[-10:]

        # 更新观点簇（带证据）
        self._update_clusters(memory, clusters, cluster_to_evidence)

        # 检测矛盾
        detector = get_contradiction_detector()
        contradictions = detector.detect_contradictions(memory)
        memory.contradictions.extend(contradictions)

        # 保留最近 10 个矛盾记录
        if len(memory.contradictions) > 10:
            memory.contradictions = memory.contradictions[-10:]

        # 计算趋势
        calculator = get_trend_calculator()
        calculator.calculate_trends(memory)

    def _update_clusters(
        self,
        memory: EntityMemory,
        new_clusters: list[dict],
        cluster_to_evidence: dict[str, list[str]]
    ) -> None:
        """更新观点簇（带证据引用）"""
        now = datetime.now().isoformat()
        for cluster_idx, new_cluster in enumerate(new_clusters):
            topic = new_cluster.get("topic", "")
            sentiment = new_cluster.get("sentiment", "")
            count = new_cluster.get("count", 0)

            # 获取该观点簇的证据 ID 列表
            cluster_key = f"cluster_{cluster_idx}"
            evidence_ids = self._unique(cluster_to_evidence.get(cluster_key, []))

            # 查找是否已有相同主题的观点簇
            existing_cluster = None
            for cluster in memory.consensus_clusters:
                if cluster.topic == topic:
                    existing_cluster = cluster
                    break

            if existing_cluster:
                # 更新现有观点簇
                if not existing_cluster.cluster_id:
                    existing_cluster.cluster_id = self._make_cluster_id(existing_cluster.topic)
                if not existing_cluster.first_seen:
                    existing_cluster.first_seen = now
                existing_cluster.last_seen = now
                existing_cluster.frequency += 1
                existing_cluster.avg_count = (
                    (existing_cluster.avg_count * (existing_cluster.frequency - 1) + count) /
                    existing_cluster.frequency
                )

                # 添加新证据
                for evidence_id in evidence_ids:
                    if evidence_id not in existing_cluster.evidence_ids:
                        existing_cluster.evidence_ids.append(evidence_id)

                # 保留最近 5 个
                if len(existing_cluster.evidence_ids) > 5:
                    existing_cluster.evidence_ids = self._unique(existing_cluster.evidence_ids)[-5:]

            else:
                # 创建新观点簇
                selected_refs = evidence_ids[:3] if evidence_ids else []

                memory.consensus_clusters.append(ConsensusCluster(
                    topic=topic,
                    sentiment=sentiment,
                    avg_count=float(count),
                    frequency=1,
                    trend="new",
                    primary_aspects=new_cluster.get("primary_aspects", []),
                    sub_aspects=new_cluster.get("sub_aspects", []),
                    synonym_aspects=new_cluster.get("synonym_aspects", []),
                    evidence_ids=selected_refs,
                    first_seen=now,
                    last_seen=now,
                    cluster_id=self._make_cluster_id(topic)
                ))

    def _resolve_entity_name(self, entity: str) -> str:
        """复用已有实体目录名，避免大小写/空格轻微差异导致目录分裂。"""
        normalized_entity = entity.replace(" ", "").lower()
        for entity_dir in self._entities_dir.iterdir():
            if not entity_dir.is_dir():
                continue
            normalized_dir_name = entity_dir.name.replace(" ", "").lower()
            if normalized_dir_name == normalized_entity:
                return entity_dir.name
        return entity

    def _make_cluster_id(self, topic: str) -> str:
        """基于 topic 生成稳定的观点簇 ID。"""
        normalized = (topic or "unknown").strip().lower()
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
        return f"cl_{digest}"

    def _unique(self, values: list[str]) -> list[str]:
        """保持顺序去重并过滤空值。"""
        result = []
        seen = set()
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result


# 全局实例
_memory_manager: MemoryManager | None = None


def get_memory_manager() -> MemoryManager:
    """获取记忆管理器单例"""
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager()
    return _memory_manager
