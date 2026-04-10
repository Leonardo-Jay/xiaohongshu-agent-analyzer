"""
记忆检索与复用决策模块（重构版）

基于 Karpathy Wiki 理念：纯结构化检索
- 不使用 embedding，只用字符串匹配
- 分层匹配：主标签 > 子标签 > 同义标签 > 子串匹配
- 基于规则的复用决策（无需 LLM）
- 可解释：知道为什么匹配
"""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from app.memory.memory_types import EntityMemory, ConsensusCluster


@dataclass
class ReuseDecision:
    """复用决策结果"""
    can_reuse: bool
    coverage_ratio: float           # 观点覆盖度（0.0-1.0）
    reuse_strategy: str             # 复用策略（"full" | "incremental" | "none"）
    reusable_clusters: list[dict]   # 可复用的观点簇（带证据）
    matched_aspects: list[str]      # 匹配到的关注点
    missing_aspects: list[str]      # 缺失的关注点
    entity_memory: EntityMemory | None  # 匹配的实体记忆
    reason: str                     # 决策原因


class MemoryRetrieval:
    """记忆检索与复用决策管理器（纯结构化）"""

    def __init__(self, base_dir: str | None = None):
        self._base_dir = Path(base_dir) if base_dir else Path(__file__).parent.parent.parent / "data" / "memory"
        self._entities_dir = self._base_dir / "entities"

    async def retrieve_and_decide(
        self,
        entity: str,
        current_query: str,
        intent: str,
        key_aspects: list[str] = None,  # 用户关注点：["游戏性能", "续航"]
        use_llm: bool = False  # 不再使用 LLM
    ) -> ReuseDecision:
        """
        检索记忆并做出复用决策（纯结构化）

        Args:
            entity: 产品实体
            current_query: 当前用户查询
            intent: 当前意图类型
            key_aspects: 用户关注点列表
            use_llm: 已废弃，保留参数兼容性

        Returns:
            ReuseDecision 复用决策
        """
        if not entity:
            return self._empty_decision("无产品实体")

        # 1. 加载实体记忆
        entity_memory = self._load_entity_memory(entity)
        if not entity_memory or not entity_memory.consensus_clusters:
            return self._empty_decision("无历史记忆")

        # 2. 提取用户关注点
        if not key_aspects:
            key_aspects = []

        # 3. 纯结构化匹配观点簇
        matched_clusters = self._match_clusters(
            entity_memory.consensus_clusters,
            key_aspects
        )

        logger.info(
            f"[MemoryRetrieval] 观点簇匹配结果: "
            f"总观点簇={len(entity_memory.consensus_clusters)}, "
            f"匹配成功={len(matched_clusters)}, "
            f"key_aspects={key_aspects}"
        )

        # 4. 计算覆盖率
        coverage_ratio, matched_aspects, missing_aspects = self._calculate_coverage(
            matched_clusters,
            key_aspects
        )

        # 5. 基于规则决策复用策略
        reuse_strategy = self._decide_strategy(coverage_ratio)

        # 6. 构造返回结果
        if reuse_strategy != "none":
            return ReuseDecision(
                can_reuse=True,
                coverage_ratio=coverage_ratio,
                reuse_strategy=reuse_strategy,
                reusable_clusters=[
                    {
                        "topic": c.topic,
                        "sentiment": c.sentiment,
                        "primary_aspects": c.primary_aspects,
                        "sub_aspects": c.sub_aspects,
                        "synonym_aspects": c.synonym_aspects,
                        "avg_count": c.avg_count,
                        "frequency": c.frequency,
                        "trend": c.trend,
                        "evidence_ids": c.evidence_ids
                    }
                    for c in matched_clusters
                ],
                matched_aspects=matched_aspects,
                missing_aspects=missing_aspects,
                entity_memory=entity_memory,
                reason=f"覆盖度={coverage_ratio:.2f}, 匹配={len(matched_clusters)}个观点簇"
            )
        else:
            return ReuseDecision(
                can_reuse=False,
                coverage_ratio=coverage_ratio,
                reuse_strategy=reuse_strategy,
                reusable_clusters=[],
                matched_aspects=matched_aspects,
                missing_aspects=missing_aspects,
                entity_memory=None,
                reason=f"覆盖度过低({coverage_ratio:.2f})"
            )

    def _match_clusters(
        self,
        clusters: list[ConsensusCluster],
        key_aspects: list[str]
    ) -> list[ConsensusCluster]:
        """
        纯结构化匹配观点簇

        匹配策略（分层）：
        - Layer 1: 主标签完全匹配（score 1.0）
        - Layer 2: 子标签完全匹配（score 0.8）
        - Layer 3: 同义标签完全匹配（score 0.7）
        - Layer 4: 主标签子串匹配（score 0.6）
        - Layer 5: 子标签子串匹配（score 0.5）
        - Layer 6: topic 子串匹配（score 0.3）
        """
        if not key_aspects:
            # 如果没有指定关注点，返回所有观点簇
            return clusters

        matched_clusters_with_score = []

        for cluster in clusters:
            score = self._calculate_match_score(cluster, key_aspects)
            if score > 0:
                matched_clusters_with_score.append((cluster, score))

        # 按分数排序
        matched_clusters_with_score.sort(key=lambda x: x[1], reverse=True)

        # 返回匹配的观点簇
        return [c for c, _ in matched_clusters_with_score]

    def _calculate_match_score(
        self,
        cluster: ConsensusCluster,
        key_aspects: list[str]
    ) -> float:
        """计算匹配分数（纯字符串匹配）"""
        score = 0.0

        for aspect in key_aspects:
            # Layer 1: 主标签完全匹配
            if aspect in cluster.primary_aspects:
                score += 1.0
                continue

            # Layer 2: 子标签完全匹配
            if aspect in cluster.sub_aspects:
                score += 0.8
                continue

            # Layer 3: 同义标签完全匹配
            if aspect in cluster.synonym_aspects:
                score += 0.7
                continue

            # Layer 4: 主标签子串匹配
            if any(aspect in tag or tag in aspect for tag in cluster.primary_aspects):
                score += 0.6
                continue

            # Layer 5: 子标签子串匹配
            if any(aspect in tag or tag in aspect for tag in cluster.sub_aspects):
                score += 0.5
                continue

            # Layer 6: topic 子串匹配（兜底）
            if aspect in cluster.topic or cluster.topic in aspect:
                score += 0.3

        return score

    def _calculate_coverage(
        self,
        matched_clusters: list[ConsensusCluster],
        key_aspects: list[str]
    ) -> tuple[float, list[str], list[str]]:
        """
        计算覆盖率

        Returns:
            (coverage_ratio, matched_aspects, missing_aspects)
        """
        if not key_aspects:
            # 如果没有指定关注点，认为完全覆盖
            return 1.0, [], []

        # 收集匹配到的关注点
        matched_aspects_set = set()
        for cluster in matched_clusters:
            matched_aspects_set.update(cluster.primary_aspects)
            matched_aspects_set.update(cluster.sub_aspects)

        # 计算覆盖的用户关注点
        covered_aspects = []
        for aspect in key_aspects:
            # 检查是否被覆盖（完全匹配或子串匹配）
            if aspect in matched_aspects_set:
                covered_aspects.append(aspect)
            elif any(aspect in tag or tag in aspect for tag in matched_aspects_set):
                covered_aspects.append(aspect)

        # 计算覆盖率
        coverage_ratio = len(covered_aspects) / len(key_aspects) if key_aspects else 1.0

        # 缺失的关注点
        missing_aspects = [a for a in key_aspects if a not in covered_aspects]

        return coverage_ratio, covered_aspects, missing_aspects

    def _decide_strategy(self, coverage_ratio: float) -> str:
        """
        基于规则决策复用策略

        规则：
        - coverage_ratio >= 0.8: full（完全复用）
        - 0.4 <= coverage_ratio < 0.8: incremental（增量更新）
        - coverage_ratio < 0.4: none（从头开始）
        """
        if coverage_ratio >= 0.8:
            return "full"
        elif coverage_ratio >= 0.4:
            return "incremental"
        else:
            return "none"

    def _load_entity_memory(self, entity: str) -> EntityMemory | None:
        """加载实体记忆（支持模糊匹配）"""
        # 尝试直接匹配
        memory_file = self._entities_dir / entity / "memory.json"

        if memory_file.exists():
            try:
                with open(memory_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.debug(f"[MemoryRetrieval] 精确匹配实体: {entity}")
                return EntityMemory.from_dict(data)
            except Exception as e:
                logger.warning(f"[MemoryRetrieval] 加载记忆失败: {e}")
                return None

        # 尝试模糊匹配（忽略空格和大小写）
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
                    logger.info(f"[MemoryRetrieval] 模糊匹配实体: {entity} -> {entity_dir.name}")
                    return EntityMemory.from_dict(data)
                except Exception as e:
                    logger.warning(f"[MemoryRetrieval] 加载记忆失败: {e}")
                    return None

        logger.debug(f"[MemoryRetrieval] 未找到实体记忆: {entity}")
        return None

    def _empty_decision(self, reason: str) -> ReuseDecision:
        """返回空决策"""
        return ReuseDecision(
            can_reuse=False,
            coverage_ratio=0.0,
            reuse_strategy="none",
            reusable_clusters=[],
            matched_aspects=[],
            missing_aspects=[],
            entity_memory=None,
            reason=reason
        )


# 全局实例
_memory_retrieval: MemoryRetrieval | None = None


def get_memory_retrieval() -> MemoryRetrieval:
    """获取 MemoryRetrieval 单例"""
    global _memory_retrieval
    if _memory_retrieval is None:
        _memory_retrieval = MemoryRetrieval()
    return _memory_retrieval
