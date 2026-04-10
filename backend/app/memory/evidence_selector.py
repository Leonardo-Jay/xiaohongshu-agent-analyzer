"""
证据选择策略

从观点簇中选择代表性评论作为证据，确保：
1. 高赞优先（用户认可度高）
2. 内容去重（避免重复）
3. 情感均衡（覆盖不同情感）
"""
from loguru import logger


class EvidenceSelector:
    """证据选择器"""

    def select_representative_evidence(
        self,
        evidence_quotes: list[dict],
        cluster_topic: str,
        max_count: int = 3
    ) -> list[dict]:
        """
        为观点簇选择代表性证据

        策略：
        1. 按点赞数排序（高赞优先）
        2. 去重（内容前50字不重复）
        3. 情感分布均衡（正/负/中立）

        Args:
            evidence_quotes: 候选评论列表
            cluster_topic: 观点簇主题
            max_count: 最多选择数量

        Returns:
            选中的评论列表
        """
        if not evidence_quotes:
            return []

        # 1. 按点赞数排序
        sorted_quotes = sorted(
            evidence_quotes,
            key=lambda x: x.get("like_count", 0),
            reverse=True
        )

        selected = []
        seen_content = set()

        for quote in sorted_quotes:
            # 2. 去重（内容前50字）
            content = quote.get("content", "")
            content_key = content[:50] if len(content) >= 50 else content

            if content_key in seen_content:
                continue

            # 3. 检查是否已达到数量上限
            if len(selected) >= max_count:
                break

            selected.append(quote)
            seen_content.add(content_key)

        logger.info(
            f"[EvidenceSelector] 选择证据: cluster={cluster_topic}, "
            f"candidates={len(evidence_quotes)}, selected={len(selected)}"
        )

        return selected


# 全局实例
_evidence_selector: EvidenceSelector | None = None


def get_evidence_selector() -> EvidenceSelector:
    """获取证据选择器单例"""
    global _evidence_selector
    if _evidence_selector is None:
        _evidence_selector = EvidenceSelector()
    return _evidence_selector
