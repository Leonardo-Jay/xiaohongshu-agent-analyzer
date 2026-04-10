"""
实体记忆聚合模块

负责：
1. 加载/保存实体记忆
2. 更新观点簇（带证据引用）
3. 检测矛盾和趋势
4. 知识累积
"""
import json
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
        skip_evidence_save: bool = False  # NEW: 是否跳过证据保存（使用异步保存）
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
            cluster_to_evidence = {}
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
            cluster_to_evidence = {}
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
        # 更新基本信息
        memory.last_analyzed = datetime.now().isoformat()
        memory.total_analyses += 1

        # 更新查询记录
        memory.recent_queries.append(QueryRecord(
            query=query,
            intent=intent,
            timestamp=datetime.now().isoformat(),
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
        for cluster_idx, new_cluster in enumerate(new_clusters):
            topic = new_cluster.get("topic", "")
            sentiment = new_cluster.get("sentiment", "")
            count = new_cluster.get("count", 0)

            # 获取该观点簇的证据 ID 列表
            cluster_key = f"cluster_{cluster_idx}"
            evidence_ids = cluster_to_evidence.get(cluster_key, [])

            # 查找是否已有相同主题的观点簇
            existing_cluster = None
            for cluster in memory.consensus_clusters:
                if cluster.topic == topic:
                    existing_cluster = cluster
                    break

            if existing_cluster:
                # 更新现有观点簇
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
                    existing_cluster.evidence_ids = existing_cluster.evidence_ids[-5:]

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
                    evidence_ids=selected_refs
                ))


# 全局实例
_memory_manager: MemoryManager | None = None


def get_memory_manager() -> MemoryManager:
    """获取记忆管理器单例"""
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager()
    return _memory_manager
