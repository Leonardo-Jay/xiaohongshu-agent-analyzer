"""
概念记忆模块

跨实体的知识聚合，例如：
- quality_issues: 所有产品的质量问题观点
- price_value: 所有产品的性价比观点
- user_sentiment: 用户情感趋势

从 LLM Wiki 架构借鉴：概念页面可以关联多个实体
"""
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger


class ConceptMemory:
    """概念记忆管理器"""

    def __init__(self, base_dir: str | None = None):
        self._base_dir = Path(base_dir) if base_dir else Path(__file__).parent.parent.parent / "data" / "memory"
        self._concepts_dir = self._base_dir / "concepts"
        self._concepts_dir.mkdir(parents=True, exist_ok=True)

    def update_concept(
        self,
        concept_name: str,
        entity: str,
        cluster_topic: str,
        sentiment: str,
        count: int
    ) -> None:
        """
        更新概念记忆

        Args:
            concept_name: 概念名称（如 quality_issues）
            entity: 产品实体
            cluster_topic: 观点主题
            sentiment: 情感倾向
            count: 讨论量
        """
        concept_file = self._concepts_dir / f"{concept_name}.json"

        # 加载现有概念
        concept_data = {}
        if concept_file.exists():
            try:
                with open(concept_file, "r", encoding="utf-8") as f:
                    concept_data = json.load(f)
            except Exception as e:
                logger.warning(f"[ConceptMemory] 加载概念失败: {e}")

        # 更新概念数据
        if "entities" not in concept_data:
            concept_data["entities"] = {}

        if entity not in concept_data["entities"]:
            concept_data["entities"][entity] = []

        # 添加观点记录
        concept_data["entities"][entity].append({
            "topic": cluster_topic,
            "sentiment": sentiment,
            "count": count,
            "timestamp": datetime.now().isoformat()
        })

        # 只保留最近 5 次记录
        if len(concept_data["entities"][entity]) > 5:
            concept_data["entities"][entity] = concept_data["entities"][entity][-5:]

        # 更新元数据
        concept_data["concept_name"] = concept_name
        concept_data["last_updated"] = datetime.now().isoformat()
        concept_data["entity_count"] = len(concept_data["entities"])

        # 保存
        with open(concept_file, "w", encoding="utf-8") as f:
            json.dump(concept_data, f, ensure_ascii=False, indent=2)

        logger.debug(f"[ConceptMemory] 更新概念: {concept_name} ← {entity}")

    def map_topic_to_concept(self, topic: str) -> str | None:
        """
        将观点主题映射到概念

        简单规则：
        - 包含 "质量"、"品控"、"问题" → quality_issues
        - 包含 "价格"、"性价比" → price_value
        - 包含 "续航"、"电池" → battery_life
        - 包含 "外观"、"设计" → design
        """
        topic_lower = topic.lower()

        if any(kw in topic_lower for kw in ["质量", "品控", "问题", "故障", "瑕疵"]):
            return "quality_issues"
        elif any(kw in topic_lower for kw in ["价格", "性价比", "贵", "便宜"]):
            return "price_value"
        elif any(kw in topic_lower for kw in ["续航", "电池", "掉电"]):
            return "battery_life"
        elif any(kw in topic_lower for kw in ["外观", "设计", "颜值", "手感"]):
            return "design"
        elif any(kw in topic_lower for kw in ["性能", "卡顿", "发热"]):
            return "performance"
        elif any(kw in topic_lower for kw in ["拍照", "相机", "照片"]):
            return "camera"

        return None

    def update_concepts_from_clusters(
        self,
        entity: str,
        clusters: list[dict]
    ) -> None:
        """
        从观点簇批量更新概念记忆

        Args:
            entity: 产品实体
            clusters: 观点簇列表
        """
        for cluster in clusters:
            topic = cluster.get("topic", "")
            sentiment = cluster.get("sentiment", "")
            count = cluster.get("count", 0)

            # 映射到概念
            concept_name = self.map_topic_to_concept(topic)
            if concept_name:
                self.update_concept(
                    concept_name,
                    entity,
                    topic,
                    sentiment,
                    count
                )


# 全局实例
_concept_memory: ConceptMemory | None = None


def get_concept_memory() -> ConceptMemory:
    """获取概念记忆管理器单例"""
    global _concept_memory
    if _concept_memory is None:
        _concept_memory = ConceptMemory()
    return _concept_memory
