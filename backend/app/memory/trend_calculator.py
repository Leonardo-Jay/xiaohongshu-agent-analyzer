"""
趋势计算模块

分析观点簇的讨论趋势：
- stable: 稳定
- rising: 上升
- falling: 下降
"""
from loguru import logger

from app.memory.memory_types import EntityMemory


class TrendCalculator:
    """趋势计算器"""

    def calculate_trends(self, memory: EntityMemory) -> None:
        """
        计算观点簇趋势

        规则：
        - frequency >= 3 且 avg_count > 15: rising（高频讨论）
        - frequency >= 3 且 avg_count < 5: falling（低频讨论）
        - 其他: stable

        同时考虑：
        - 最近一次分析的讨论量 vs 平均值
        """
        for cluster in memory.consensus_clusters:
            old_trend = cluster.trend

            # 基础规则：根据出现频率和平均讨论量判断
            if cluster.frequency >= 3:
                if cluster.avg_count > 15:
                    cluster.trend = "rising"
                elif cluster.avg_count < 5:
                    cluster.trend = "falling"
                else:
                    cluster.trend = "stable"
            elif cluster.frequency == 2:
                # 出现 2 次，根据讨论量判断
                if cluster.avg_count > 12:
                    cluster.trend = "rising"
                else:
                    cluster.trend = "stable"
            else:
                # 仅出现 1 次
                cluster.trend = "new"

            # 记录趋势变化
            if old_trend != cluster.trend:
                logger.debug(
                    f"[TrendCalculator] 趋势变化: {cluster.topic} "
                    f"{old_trend} → {cluster.trend}"
                )

        logger.info(f"[TrendCalculator] 趋势计算完成: {len(memory.consensus_clusters)} 个观点簇")


# 全局实例
_trend_calculator: TrendCalculator | None = None


def get_trend_calculator() -> TrendCalculator:
    """获取趋势计算器单例"""
    global _trend_calculator
    if _trend_calculator is None:
        _trend_calculator = TrendCalculator()
    return _trend_calculator
