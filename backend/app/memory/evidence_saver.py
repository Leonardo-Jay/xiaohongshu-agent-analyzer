"""
证据保存模块（重构版）

基于 Karpathy Wiki 理念：
- 证据按内容哈希去重（相同内容只存一份）
- 统一 Evidence 结构（评论和帖子正文）
- 证据文件按 evidence_id 存储
- 支持双向引用（证据 → 观点簇）
- 支持异步后台保存（不阻塞用户体验）
"""
import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.memory.memory_types import Evidence


# 全局线程池（复用，避免创建多个）
_evidence_thread_pool: ThreadPoolExecutor | None = None


def _get_thread_pool() -> ThreadPoolExecutor:
    """获取全局线程池（懒加载）"""
    global _evidence_thread_pool
    if _evidence_thread_pool is None:
        # 创建 daemon 线程池，程序退出时自动清理
        _evidence_thread_pool = ThreadPoolExecutor(
            max_workers=2,  # 最多 2 个线程
            thread_name_prefix="evidence_saver",
        )
        logger.info("[EvidenceSaver] 创建全局线程池: max_workers=2")
    return _evidence_thread_pool


class EvidenceSaver:
    """证据保存器（重构版）"""

    def __init__(self, base_dir: str | None = None):
        self._base_dir = Path(base_dir) if base_dir else Path(__file__).parent.parent.parent / "data" / "memory"
        # 内存缓存：content_hash -> evidence_id（避免重复计算哈希）
        self._hash_cache: dict[str, str] = {}

    async def save_evidence_async(
        self,
        entity: str,
        screened_items: list[dict],
        clusters: list[dict],
        retrieved_comments: list[dict] | None = None
    ) -> None:
        """
        异步保存证据（使用全局线程池）

        使用场景：报告生成完成后，后台异步保存证据，不阻塞用户看到报告
        """
        loop = asyncio.get_event_loop()
        pool = _get_thread_pool()

        try:
            # 使用全局线程池执行同步 I/O
            await loop.run_in_executor(
                pool,
                self._save_evidence_batch_sync,
                entity, screened_items, clusters, retrieved_comments
            )
            logger.info(f"[EvidenceSaver] 后台保存证据完成: entity={entity}")
        except asyncio.CancelledError:
            # 任务被取消，记录但不阻塞
            logger.warning(f"[EvidenceSaver] 证据保存被取消: entity={entity}")
            # 注意：线程池中的任务会继续执行，但不阻塞主流程
            raise  # 重新抛出，让调用方知道
        except Exception as e:
            logger.error(f"[EvidenceSaver] 后台保存证据失败: {e}")

    def save_evidence_batch(
        self,
        entity: str,
        screened_items: list[dict],
        clusters: list[dict],
        retrieved_comments: list[dict] | None = None
    ) -> dict[str, list[str]]:
        """
        批量保存证据（帖子+评论）- 同步版本

        Args:
            entity: 产品实体名
            screened_items: 筛选后的帖子列表
            clusters: 观点簇列表（包含 evidence_quotes）
            retrieved_comments: 检索到的评论列表

        Returns:
            {
                "evidence_ids": ["ev_abc123", "ev_def456", ...],
                "cluster_to_evidence": {
                    "cluster_0": ["ev_abc123", "ev_def456"],
                    "cluster_1": ["ev_def456", "ev_ghi789"]
                }
            }
        """
        return self._save_evidence_batch_sync(entity, screened_items, clusters, retrieved_comments)

    def _save_evidence_batch_sync(
        self,
        entity: str,
        screened_items: list[dict],
        clusters: list[dict],
        retrieved_comments: list[dict] | None = None
    ) -> dict[str, list[str]]:
        """同步版本的证据保存（后台线程调用）"""
        # 创建实体证据目录
        entity_dir = self._base_dir / "entities" / entity / "evidence"
        entity_dir.mkdir(parents=True, exist_ok=True)

        # 加载现有证据的哈希缓存
        self._load_hash_cache(entity_dir)

        all_evidence_ids = []
        cluster_to_evidence = {}

        # 1. 保存帖子（作为证据）
        post_id_to_info = {}
        for post in screened_items:
            note_id = post.get("note_id", "")
            if note_id:
                post_id_to_info[note_id] = post

        # 2. 构建评论映射（content -> comment）
        content_to_comment = {}
        if retrieved_comments:
            for comment in retrieved_comments:
                content = comment.get("content", "")
                if content:
                    content_to_comment[content] = comment

        # 3. 遍历观点簇，保存证据
        for cluster_idx, cluster in enumerate(clusters):
            cluster_key = f"cluster_{cluster_idx}"
            cluster_evidence_ids = []

            evidence_quotes = cluster.get("evidence_quotes", [])
            cluster_topic = cluster.get("topic", "")

            logger.debug(f"[EvidenceSaver] 观点簇 {cluster_idx}: topic={cluster_topic}, evidence_quotes={len(evidence_quotes)}")

            for quote_content in evidence_quotes:
                if not isinstance(quote_content, str) or not quote_content:
                    continue

                # 避免误匹配：引用太短时跳过
                if len(quote_content) < 10:
                    logger.debug(f"[EvidenceSaver] 引用过短，跳过: {quote_content[:30]}...")
                    continue

                # 尝试匹配评论（优先精确匹配，其次包含匹配）
                matched_comment = content_to_comment.get(quote_content)  # 精确匹配

                if not matched_comment:
                    # 包含匹配：遍历所有评论
                    best_match = None
                    best_match_score = 0

                    for content, comment in content_to_comment.items():
                        if quote_content in content:
                            # 引用包含在评论中，计算匹配度
                            match_score = len(quote_content) / len(content)
                            if match_score > best_match_score:
                                best_match = comment
                                best_match_score = match_score
                        elif content in quote_content:
                            # 评论包含在引用中（罕见情况）
                            match_score = len(content) / len(quote_content)
                            if match_score > best_match_score:
                                best_match = comment
                                best_match_score = match_score

                    # 检查匹配质量
                    if best_match and best_match_score >= 0.2:  # 匹配度 >= 20%
                        matched_comment = best_match
                        logger.debug(f"[EvidenceSaver] 包含匹配成功: score={best_match_score:.2f}, quote={quote_content[:30]}...")

                if matched_comment:
                    # 保存评论证据
                    logger.debug(f"[EvidenceSaver] 匹配到评论: {quote_content[:50]}...")
                    evidence_id = self._save_comment_evidence(
                        entity_dir,
                        matched_comment,
                        cluster_topic
                    )
                    if evidence_id:
                        cluster_evidence_ids.append(evidence_id)
                        all_evidence_ids.append(evidence_id)
                else:
                    # 可能是帖子正文，尝试匹配
                    # 检查是否包含在某个帖子的 desc 中
                    matched = False
                    for note_id, post_info in post_id_to_info.items():
                        post_desc = post_info.get("desc", "")
                        if quote_content in post_desc or post_desc in quote_content:
                            # 保存帖子正文作为证据
                            logger.debug(f"[EvidenceSaver] 匹配到帖子正文: {quote_content[:50]}...")
                            evidence_id = self._save_post_body_evidence(
                                entity_dir,
                                post_info,
                                cluster_topic
                            )
                            if evidence_id:
                                cluster_evidence_ids.append(evidence_id)
                                all_evidence_ids.append(evidence_id)
                            matched = True
                            break

                    if not matched:
                        logger.warning(f"[EvidenceSaver] 未匹配到证据: {quote_content[:50]}...")

            cluster_to_evidence[cluster_key] = cluster_evidence_ids

        # 去重
        all_evidence_ids = list(set(all_evidence_ids))

        logger.info(
            f"[EvidenceSaver] 保存证据完成: entity={entity}, "
            f"total_evidence={len(all_evidence_ids)}, clusters={len(clusters)}"
        )

        return {
            "evidence_ids": all_evidence_ids,
            "cluster_to_evidence": cluster_to_evidence
        }

    def _save_comment_evidence(
        self,
        entity_dir: Path,
        comment: dict,
        cluster_topic: str
    ) -> str | None:
        """保存评论证据"""
        try:
            content = comment.get("content", "")
            if not content:
                return None

            # 计算内容哈希
            content_hash = self._compute_hash(content)

            # 检查是否已存在
            if content_hash in self._hash_cache:
                logger.debug(f"[EvidenceSaver] 证据已存在，跳过: {content_hash[:8]}")
                return self._hash_cache[content_hash]

            # 生成 evidence_id
            evidence_id = f"ev_{content_hash[:16]}"

            # 创建 Evidence 对象
            evidence = Evidence(
                evidence_id=evidence_id,
                evidence_type="comment",
                content=content,  # 不截断
                content_hash=content_hash,
                note_id=comment.get("note_id", ""),
                note_url=comment.get("note_url", ""),
                note_title="",  # 评论没有标题
                comment_id=comment.get("comment_id", ""),
                nickname=comment.get("nickname", ""),
                like_count=comment.get("like_count", 0),
                fetched_at=datetime.now().isoformat(),
                referenced_by=[]  # 稍后更新
            )

            # 保存到文件
            filepath = entity_dir / f"{evidence_id}.json"
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(evidence.__dict__, f, ensure_ascii=False, indent=2)

            # 更新缓存
            self._hash_cache[content_hash] = evidence_id

            return evidence_id

        except Exception as e:
            logger.warning(f"[EvidenceSaver] 保存评论证据失败: {e}")
            return None

    def _save_post_body_evidence(
        self,
        entity_dir: Path,
        post: dict,
        cluster_topic: str
    ) -> str | None:
        """保存帖子正文作为证据"""
        try:
            content = post.get("desc", "")
            if not content:
                return None

            # 计算内容哈希
            content_hash = self._compute_hash(content)

            # 检查是否已存在
            if content_hash in self._hash_cache:
                logger.debug(f"[EvidenceSaver] 证据已存在，跳过: {content_hash[:8]}")
                return self._hash_cache[content_hash]

            # 生成 evidence_id
            evidence_id = f"ev_{content_hash[:16]}"

            # 创建 Evidence 对象
            evidence = Evidence(
                evidence_id=evidence_id,
                evidence_type="post_body",
                content=content,  # 不截断
                content_hash=content_hash,
                note_id=post.get("note_id", ""),
                note_url=post.get("note_url", ""),
                note_title=post.get("title", ""),
                comment_id=None,
                nickname=None,
                like_count=None,
                fetched_at=datetime.now().isoformat(),
                referenced_by=[]  # 稍后更新
            )

            # 保存到文件
            filepath = entity_dir / f"{evidence_id}.json"
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(evidence.__dict__, f, ensure_ascii=False, indent=2)

            # 更新缓存
            self._hash_cache[content_hash] = evidence_id

            return evidence_id

        except Exception as e:
            logger.warning(f"[EvidenceSaver] 保存帖子正文证据失败: {e}")
            return None

    def _compute_hash(self, content: str) -> str:
        """计算内容哈希（SHA256）"""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _load_hash_cache(self, entity_dir: Path):
        """加载现有证据的哈希缓存"""
        self._hash_cache.clear()

        if not entity_dir.exists():
            return

        for filepath in entity_dir.glob("ev_*.json"):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    content_hash = data.get("content_hash")
                    evidence_id = data.get("evidence_id")
                    if content_hash and evidence_id:
                        self._hash_cache[content_hash] = evidence_id
            except Exception as e:
                logger.warning(f"[EvidenceSaver] 加载证据缓存失败: {filepath}, {e}")

    def load_evidence(self, entity: str, evidence_id: str) -> Evidence | None:
        """加载单个证据"""
        try:
            entity_dir = self._base_dir / "entities" / entity / "evidence"
            filepath = entity_dir / f"{evidence_id}.json"

            if not filepath.exists():
                return None

            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                return Evidence(**data)

        except Exception as e:
            logger.warning(f"[EvidenceSaver] 加载证据失败: {evidence_id}, {e}")
            return None

    def load_evidence_batch(self, entity: str, evidence_ids: list[str]) -> list[Evidence]:
        """批量加载证据"""
        evidences = []
        for evidence_id in evidence_ids:
            evidence = self.load_evidence(entity, evidence_id)
            if evidence:
                evidences.append(evidence)
        return evidences

    def update_referenced_by(
        self,
        entity: str,
        evidence_id: str,
        cluster_id: str,
        operation: str = "add"
    ):
        """更新证据的 referenced_by 字段"""
        try:
            evidence = self.load_evidence(entity, evidence_id)
            if not evidence:
                return

            if operation == "add":
                if cluster_id not in evidence.referenced_by:
                    evidence.referenced_by.append(cluster_id)
            elif operation == "remove":
                if cluster_id in evidence.referenced_by:
                    evidence.referenced_by.remove(cluster_id)

            # 保存回文件
            entity_dir = self._base_dir / "entities" / entity / "evidence"
            filepath = entity_dir / f"{evidence_id}.json"
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(evidence.__dict__, f, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.warning(f"[EvidenceSaver] 更新 referenced_by 失败: {evidence_id}, {e}")


# 全局实例
_evidence_saver: EvidenceSaver | None = None


def get_evidence_saver() -> EvidenceSaver:
    """获取 EvidenceSaver 单例"""
    global _evidence_saver
    if _evidence_saver is None:
        _evidence_saver = EvidenceSaver()
    return _evidence_saver
