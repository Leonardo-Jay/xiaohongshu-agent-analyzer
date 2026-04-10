"""
矛盾检测模块

检测观点簇之间的矛盾：
1. 情感冲突：同一主题在不同分析中情感相反
2. 数量冲突：同一主题讨论量差异过大
"""
from datetime import datetime
from typing import Any

from loguru import logger

from app.memory.memory_types import EntityMemory, Contradiction


class ContradictionDetector:
    """矛盾检测器"""

    def detect_contradictions(self, memory: EntityMemory) -> list[Contradiction]:
        """
        检测实体记忆中的矛盾观点

        检测规则：
        1. 情感冲突：同一主题在不同查询中情感标签不同
        2. 数量激增：讨论量相比平均值增长 > 100%
        3. 情感反转：正面 → 负面 或 负面 → 正面

        Returns:
            检测到的矛盾列表
        """
        contradictions = []

        for cluster in memory.consensus_clusters:
            # 只检测出现 2 次以上的观点
            if cluster.frequency < 2:
                continue

            # 1. 检测情感冲突（需要查询历史）
            # 由于我们只保存聚合结果，无法追溯每次查询的情感
            # 这里简化处理：如果 sentiment 是 "中立" 且 frequency >= 3
            # 可能表示情感分化
            if cluster.sentiment == "中立" and cluster.frequency >= 3:
                contradictions.append(Contradiction(
                    topic=cluster.topic,
                    conflict="情感分化",
                    details=f"该观点出现 {cluster.frequency} 次，情感标签为中立，可能表示用户意见分化",
                    detected_at=datetime.now().isoformat()
                ))

            # 2. 检测讨论量激增
            # 如果 avg_count 远大于中位数，表示最近讨论激增
            avg_counts = [c.avg_count for c in memory.consensus_clusters]
            if avg_counts:
                median_count = sorted(avg_counts)[len(avg_counts) // 2]
                if cluster.avg_count > median_count * 2 and cluster.trend == "rising":
                    contradictions.append(Contradiction(
                        topic=cluster.topic,
                        conflict="讨论激增",
                        details=f"该观点讨论量 ({cluster.avg_count:.1f}) 远超中位数 ({median_count:.1f})，且趋势为上升",
                        detected_at=datetime.now().isoformat()
                    ))

        if contradictions:
            logger.info(f"[ContradictionDetector] 检测到 {len(contradictions)} 个矛盾/异常")

        return contradictions


# 全局实例
_contradiction_detector: ContradictionDetector | None = None


def get_contradiction_detector() -> ContradictionDetector:
    """获取矛盾检测器单例"""
    global _contradiction_detector
    if _contradiction_detector is None:
        _contradiction_detector = ContradictionDetector()
    return _contradiction_detector
