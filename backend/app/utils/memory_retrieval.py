"""
记忆检索与复用决策模块（重构版）

基于 Karpathy Wiki 理念：结构化检索
- 不使用 embedding
- 分层字符串匹配：主标签 > 子标签 > 同义标签 > 子串匹配
- BM25 关键词匹配：补充召回自然问法和标签写法差异
- 基于规则的复用决策（无需 LLM）
- 可解释：知道为什么匹配
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from app.memory.memory_types import EntityMemory, ConsensusCluster
from app.utils.lexical_bm25 import BM25_AVAILABLE, BM25ClusterIndex


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
    source_run_ref: dict[str, Any] | None = None  # 命中的 run 快照引用
    source_note_ids: list[str] = None             # run 快照中使用过的帖子 ID


@dataclass
class ClusterMatch:
    cluster: ConsensusCluster
    string_score: float = 0.0
    string_score_norm: float = 0.0
    bm25_score: float = 0.0
    bm25_score_norm: float = 0.0
    hybrid_score: float = 0.0
    covered_aspects: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)


class MemoryRetrieval:
    """记忆检索与复用决策管理器（字符串规则 + BM25）"""

    def __init__(self, base_dir: str | None = None):
        self._base_dir = Path(base_dir) if base_dir else Path(__file__).parent.parent.parent / "data" / "memory"
        self._entities_dir = self._base_dir / "entities"
        self._bm25_cache: dict[str, dict[str, Any]] = {}

    async def retrieve_and_decide(
        self,
        entity: str,
        current_query: str,
        intent: str,
        key_aspects: list[str] = None,  # 用户关注点：["游戏性能", "续航"]
        use_llm: bool = False,  # 不再使用 LLM
        recent_run_ref: dict[str, Any] | None = None,
    ) -> ReuseDecision:
        """
        检索记忆并做出复用决策（字符串规则 + BM25）

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

        # 1. 优先尝试上一轮 run 快照。短期记忆只保存 run_ref，不保存观点簇本体。
        run_decision = self._decide_from_run_snapshot(
            recent_run_ref=recent_run_ref,
            entity=entity,
            current_query=current_query,
            key_aspects=key_aspects or [],
        )
        if run_decision and run_decision.reuse_strategy != "none":
            return run_decision

        # 2. 加载实体聚合记忆
        entity_memory = self._load_entity_memory(entity)
        if not entity_memory or not entity_memory.consensus_clusters:
            return self._empty_decision("无历史记忆")

        # 3. 提取用户关注点
        if not key_aspects:
            key_aspects = []

        # 4. 字符串规则 + BM25 混合匹配观点簇
        matches = self._match_clusters_hybrid(
            entity_memory.consensus_clusters,
            key_aspects,
            current_query=current_query,
            entity=entity_memory.entity or entity,
            source="memory",
        )
        matched_clusters = [match.cluster for match in matches]

        logger.info(
            f"[MemoryRetrieval] 观点簇匹配结果: "
            f"总观点簇={len(entity_memory.consensus_clusters)}, "
            f"匹配成功={len(matched_clusters)}, "
            f"key_aspects={key_aspects}"
        )
        self._log_hybrid_matches(matches, source="memory")

        # 5. 计算覆盖率
        coverage_ratio, matched_aspects, missing_aspects = self._calculate_hybrid_coverage(
            matches,
            key_aspects
        )

        # 6. 基于规则决策复用策略
        reuse_strategy = self._decide_strategy(coverage_ratio, matches, key_aspects)

        # 7. 构造返回结果
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
                reason=f"覆盖度={coverage_ratio:.2f}, 匹配={len(matched_clusters)}个观点簇",
                source_run_ref=None,
                source_note_ids=[],
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
                reason=f"覆盖度过低({coverage_ratio:.2f})",
                source_run_ref=None,
                source_note_ids=[],
            )

    def _decide_from_run_snapshot(
        self,
        recent_run_ref: dict[str, Any] | None,
        entity: str,
        current_query: str,
        key_aspects: list[str],
    ) -> ReuseDecision | None:
        """从上一轮 run 快照做复用决策。"""
        if not recent_run_ref:
            return None

        snapshot = self._load_run_snapshot(recent_run_ref)
        if not snapshot:
            return None

        snapshot_entity = snapshot.get("entity") or recent_run_ref.get("entity", "")
        if not self._same_entity(entity, snapshot_entity):
            return None

        clusters = self._snapshot_clusters_to_consensus(snapshot.get("clusters", []))
        if not clusters:
            return None

        matches = self._match_clusters_hybrid(
            clusters,
            key_aspects,
            current_query=current_query,
            entity=entity,
            source="run",
        )
        matched_clusters = [match.cluster for match in matches]
        coverage_ratio, matched_aspects, missing_aspects = self._calculate_hybrid_coverage(
            matches,
            key_aspects,
        )
        reuse_strategy = self._decide_strategy(coverage_ratio, matches, key_aspects)
        self._log_hybrid_matches(matches, source="run")

        if reuse_strategy == "none":
            return ReuseDecision(
                can_reuse=False,
                coverage_ratio=coverage_ratio,
                reuse_strategy="none",
                reusable_clusters=[],
                matched_aspects=matched_aspects,
                missing_aspects=missing_aspects,
                entity_memory=None,
                reason=f"上一轮 run 快照覆盖度过低({coverage_ratio:.2f})",
                source_run_ref=recent_run_ref,
                source_note_ids=[],
            )

        note_ids = snapshot.get("data_scope", {}).get("note_ids", [])
        logger.info(
            f"[MemoryRetrieval] 命中上一轮 run 快照: "
            f"run_id={recent_run_ref.get('run_id')}, coverage={coverage_ratio:.2f}, "
            f"matched_clusters={len(matched_clusters)}"
        )
        return ReuseDecision(
            can_reuse=True,
            coverage_ratio=coverage_ratio,
            reuse_strategy=reuse_strategy,
            reusable_clusters=[
                {
                    "cluster_id": c.cluster_id,
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
            entity_memory=None,
            reason=f"命中上一轮 run 快照，覆盖度={coverage_ratio:.2f}",
            source_run_ref=recent_run_ref,
            source_note_ids=note_ids,
        )

    def _match_clusters(
        self,
        clusters: list[ConsensusCluster],
        key_aspects: list[str]
    ) -> list[ConsensusCluster]:
        """
        旧版纯结构化匹配观点簇，保留给内部降级和兼容测试使用。

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

    def _match_clusters_hybrid(
        self,
        clusters: list[ConsensusCluster],
        key_aspects: list[str],
        current_query: str,
        entity: str,
        source: str,
    ) -> list[ClusterMatch]:
        """Match clusters with deterministic string rules plus BM25 lexical ranking."""
        key_aspects = key_aspects or []
        if not clusters:
            return []

        bm25_index = self._get_bm25_index(clusters, entity, source)
        query_text = " ".join([current_query or "", " ".join(key_aspects or [])]).strip()
        bm25_scores: dict[int, float] = {}
        bm25_norms: dict[int, float] = {}
        bm25_terms: dict[int, set[str]] = {}
        bm25_covered_aspects: dict[int, set[str]] = {}

        def remember_hit(hit, aspect: str | None = None) -> None:
            index = hit.cluster_index
            if hit.normalized_score > bm25_norms.get(index, 0.0):
                bm25_scores[index] = hit.raw_score
                bm25_norms[index] = hit.normalized_score
            bm25_terms.setdefault(index, set()).update(hit.matched_terms)
            if aspect:
                bm25_covered_aspects.setdefault(index, set()).add(aspect)

        if bm25_index and bm25_index.available:
            for hit in bm25_index.search(query_text, top_k=len(clusters), entity=entity):
                remember_hit(hit)

            for aspect in key_aspects or []:
                for hit in bm25_index.search(aspect, top_k=min(5, len(clusters)), entity=entity):
                    if hit.normalized_score >= 0.35:
                        remember_hit(hit, aspect=aspect)
        elif not BM25_AVAILABLE:
            logger.warning("[MemoryRetrieval] rank-bm25 未安装，降级为纯字符串记忆匹配")

        string_weight, bm25_weight = (0.75, 0.25) if source == "run" else (0.65, 0.35)
        aspect_count = max(1, len(key_aspects or []))
        matches: list[ClusterMatch] = []

        for idx, cluster in enumerate(clusters):
            string_score = self._calculate_match_score(cluster, key_aspects)
            string_score_norm = min(string_score / aspect_count, 1.0) if key_aspects else 0.0
            bm25_score = bm25_scores.get(idx, 0.0)
            bm25_score_norm = bm25_norms.get(idx, 0.0)
            hybrid_score = string_weight * string_score_norm + bm25_weight * bm25_score_norm

            covered_aspects = set(self._covered_aspects_by_string(cluster, key_aspects))
            covered_aspects.update(bm25_covered_aspects.get(idx, set()))

            methods = []
            if string_score > 0:
                methods.append("string")
            if bm25_score_norm > 0:
                methods.append("bm25")

            should_keep = (
                not key_aspects
                or string_score > 0
                or bm25_score_norm >= 0.30
            )
            if not should_keep:
                continue

            matches.append(ClusterMatch(
                cluster=cluster,
                string_score=string_score,
                string_score_norm=string_score_norm,
                bm25_score=bm25_score,
                bm25_score_norm=bm25_score_norm,
                hybrid_score=hybrid_score,
                covered_aspects=sorted(covered_aspects),
                matched_terms=sorted(bm25_terms.get(idx, set())),
                methods=methods,
            ))

        matches.sort(
            key=lambda match: (
                match.hybrid_score,
                match.string_score_norm,
                match.bm25_score_norm,
                match.cluster.frequency,
            ),
            reverse=True,
        )
        if key_aspects:
            return matches[:12]
        return matches

    def _get_bm25_index(
        self,
        clusters: list[ConsensusCluster],
        entity: str,
        source: str,
    ) -> BM25ClusterIndex | None:
        if not BM25_AVAILABLE:
            return None

        if source != "memory":
            return BM25ClusterIndex(clusters)

        memory_file = self._entities_dir / entity / "memory.json"
        try:
            memory_mtime = memory_file.stat().st_mtime_ns
        except OSError:
            memory_mtime = 0

        cache_key = entity.replace(" ", "").lower()
        cached = self._bm25_cache.get(cache_key)
        if (
            cached
            and cached.get("memory_mtime") == memory_mtime
            and cached.get("cluster_count") == len(clusters)
        ):
            return cached["index"]

        index = BM25ClusterIndex(clusters)
        self._bm25_cache[cache_key] = {
            "memory_mtime": memory_mtime,
            "cluster_count": len(clusters),
            "index": index,
        }
        logger.debug(f"[MemoryRetrieval] 构建 BM25 索引: entity={entity}, clusters={len(clusters)}")
        return index

    def _covered_aspects_by_string(
        self,
        cluster: ConsensusCluster,
        key_aspects: list[str],
    ) -> list[str]:
        covered = []
        candidate_tags = (
            list(cluster.primary_aspects)
            + list(cluster.sub_aspects)
            + list(cluster.synonym_aspects)
            + [cluster.topic]
        )

        for aspect in key_aspects or []:
            if any(self._text_overlaps(aspect, tag) for tag in candidate_tags if tag):
                covered.append(aspect)
        return covered

    def _text_overlaps(self, left: str, right: str) -> bool:
        left_norm = (left or "").replace(" ", "").lower()
        right_norm = (right or "").replace(" ", "").lower()
        if not left_norm or not right_norm:
            return False
        return left_norm == right_norm or left_norm in right_norm or right_norm in left_norm

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

    def _calculate_hybrid_coverage(
        self,
        matches: list[ClusterMatch],
        key_aspects: list[str]
    ) -> tuple[float, list[str], list[str]]:
        """Calculate aspect coverage from string hits and per-aspect BM25 hits."""
        if not key_aspects:
            return 1.0, [], []

        covered_set = set()
        for match in matches:
            covered_set.update(match.covered_aspects)

        matched_aspects = [aspect for aspect in key_aspects if aspect in covered_set]
        missing_aspects = [aspect for aspect in key_aspects if aspect not in covered_set]
        coverage_ratio = len(matched_aspects) / len(key_aspects)
        return coverage_ratio, matched_aspects, missing_aspects

    def _decide_strategy(
        self,
        coverage_ratio: float,
        matches: list[ClusterMatch] | None = None,
        key_aspects: list[str] | None = None,
    ) -> str:
        """
        基于规则决策复用策略

        规则：
        - coverage_ratio >= 0.8: full（完全复用）
        - 0.4 <= coverage_ratio < 0.8: incremental（增量更新）
        - coverage_ratio < 0.4: none（从头开始）
        """
        if coverage_ratio >= 0.8:
            if key_aspects and matches:
                top_n = min(len(key_aspects), len(matches))
                avg_hybrid_score = sum(match.hybrid_score for match in matches[:top_n]) / max(1, top_n)
                if avg_hybrid_score < 0.55:
                    logger.info(
                        f"[MemoryRetrieval] 覆盖率足够但混合置信度偏低，降级为 incremental: "
                        f"coverage={coverage_ratio:.2f}, avg_hybrid={avg_hybrid_score:.2f}"
                    )
                    return "incremental"
            return "full"
        elif coverage_ratio >= 0.4:
            return "incremental"
        else:
            return "none"

    def _log_hybrid_matches(self, matches: list[ClusterMatch], source: str) -> None:
        if not matches:
            return
        preview = []
        for match in matches[:8]:
            preview.append(
                f"{match.cluster.topic}"
                f"(hybrid={match.hybrid_score:.2f}, "
                f"string={match.string_score_norm:.2f}, "
                f"bm25={match.bm25_score_norm:.2f}, "
                f"methods={'+'.join(match.methods) or 'none'}, "
                f"terms={','.join(match.matched_terms[:6])})"
            )
        logger.info(f"[MemoryRetrieval] Hybrid matches[{source}]: " + " | ".join(preview))

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

    def _load_run_snapshot(self, run_ref: dict[str, Any]) -> dict[str, Any] | None:
        """加载 run 快照。"""
        snapshot_path = run_ref.get("snapshot_path", "")
        candidate_paths = []

        if snapshot_path:
            path = Path(snapshot_path)
            candidate_paths.append(path if path.is_absolute() else self._base_dir / snapshot_path)

        entity = run_ref.get("entity", "")
        run_id = run_ref.get("run_id", "")
        if entity and run_id:
            candidate_paths.append(self._entities_dir / entity / "runs" / f"{run_id}.json")

        for path in candidate_paths:
            try:
                if path.exists():
                    with open(path, "r", encoding="utf-8") as f:
                        return json.load(f)
            except Exception as e:
                logger.warning(f"[MemoryRetrieval] 加载 run 快照失败: {path}, error={e}")
        return None

    def _snapshot_clusters_to_consensus(self, clusters: list[dict[str, Any]]) -> list[ConsensusCluster]:
        """将 run 快照中的观点簇字典转为 ConsensusCluster，复用现有匹配逻辑。"""
        result = []
        for cluster in clusters:
            result.append(ConsensusCluster(
                topic=cluster.get("topic", ""),
                sentiment=cluster.get("sentiment", ""),
                primary_aspects=cluster.get("primary_aspects", []),
                sub_aspects=cluster.get("sub_aspects", []),
                synonym_aspects=cluster.get("synonym_aspects", []),
                avg_count=float(cluster.get("count", cluster.get("avg_count", 0)) or 0),
                frequency=1,
                trend=cluster.get("trend", "new"),
                evidence_ids=cluster.get("evidence_ids", []),
                cluster_id=cluster.get("cluster_id", ""),
            ))
        return result

    def _same_entity(self, left: str, right: str) -> bool:
        if not left or not right:
            return False
        return left.replace(" ", "").lower() == right.replace(" ", "").lower()

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
            reason=reason,
            source_run_ref=None,
            source_note_ids=[],
        )


# 全局实例
_memory_retrieval: MemoryRetrieval | None = None


def get_memory_retrieval() -> MemoryRetrieval:
    """获取 MemoryRetrieval 单例"""
    global _memory_retrieval
    if _memory_retrieval is None:
        _memory_retrieval = MemoryRetrieval()
    return _memory_retrieval
