"""
记忆机制数据结构定义

基于 Karpathy Wiki 架构核心思想：
- 知识预编译：LLM 在分析阶段生成丰富的结构化标签，检索时只做简单匹配
- 纯结构化存储：不存储 embedding，只存储 LLM 生成的多层标签
- 交叉引用网络：实体 → 观点 → 证据的双向引用
- 持续演化：增量更新、自动合并、归档清理
- 证据可追溯：防止幻觉的关键
"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Evidence:
    """统一的证据结构（评论或帖子正文）"""
    evidence_id: str                    # 唯一 ID（内容哈希）
    evidence_type: str                  # 类型：comment/post_body

    # 内容
    content: str                        # 完整内容（不截断）
    content_hash: str                   # 内容哈希（用于去重）

    # 来源信息
    note_id: str                        # 帖子 ID
    note_url: str                       # 帖子 URL
    note_title: str                     # 帖子标题

    # 评论特有字段（如果是评论）
    comment_id: str | None = None       # 评论 ID
    nickname: str | None = None         # 评论者昵称
    like_count: int | None = None       # 点赞数

    # 元数据
    fetched_at: str = ""                # 抓取时间
    referenced_by: list[str] = field(default_factory=list)  # 被哪些观点簇引用（cluster_id 列表）


@dataclass
class ConsensusCluster:
    """共识观点簇（聚合多次分析的观点）"""
    # 核心内容
    topic: str                          # 观点主题（LLM 生成）
    sentiment: str                      # 情感倾向：positive/negative/neutral

    # 多层标签（知识预编译的核心）
    primary_aspects: list[str] = field(default_factory=list)    # 主标签（1-2个）：["游戏性能"]
    sub_aspects: list[str] = field(default_factory=list)        # 子标签（2-4个）：["流畅度", "帧率", "原神"]
    synonym_aspects: list[str] = field(default_factory=list)    # 同义标签（1-2个）：["性能表现", "游戏体验"]

    # 统计信息
    avg_count: float = 0.0              # 平均讨论量
    frequency: int = 1                  # 出现频率（N 次分析中出现几次）
    trend: str = "new"                  # 趋势：stable/rising/falling/new

    # 证据引用（优化后）
    evidence_ids: list[str] = field(default_factory=list)  # 证据 ID 列表（去重后）

    # 元数据
    first_seen: str = ""                # 首次出现时间
    last_seen: str = ""                 # 最后出现时间
    cluster_id: str = ""                # 观点簇唯一 ID（用于合并追踪）


@dataclass
class Contradiction:
    """矛盾检测记录"""
    topic: str                      # 矛盾主题
    conflict: str                   # 冲突类型（情感冲突/数量冲突）
    details: str                    # 详细描述
    detected_at: str                # 检测时间


@dataclass
class QueryRecord:
    """查询记录"""
    query: str
    intent: str
    timestamp: str
    request_id: str


@dataclass
class EntityMemory:
    """实体记忆（聚合单个产品的所有分析结果）"""
    entity: str
    brand: str = ""
    first_analyzed: str = ""
    last_analyzed: str = ""
    total_analyses: int = 0

    # 核心内容
    consensus_clusters: list[ConsensusCluster] = field(default_factory=list)
    aspect_coverage: dict[str, int] = field(default_factory=dict)  # 自动计算
    contradictions: list[Contradiction] = field(default_factory=list)
    recent_queries: list[QueryRecord] = field(default_factory=list)

    # 健康指标
    health_metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典（用于 JSON 序列化）"""
        return {
            "entity": self.entity,
            "brand": self.brand,
            "first_analyzed": self.first_analyzed,
            "last_analyzed": self.last_analyzed,
            "total_analyses": self.total_analyses,
            "consensus_clusters": [
                {
                    "topic": c.topic,
                    "sentiment": c.sentiment,
                    "primary_aspects": c.primary_aspects,
                    "sub_aspects": c.sub_aspects,
                    "synonym_aspects": c.synonym_aspects,
                    "avg_count": c.avg_count,
                    "frequency": c.frequency,
                    "trend": c.trend,
                    "evidence_ids": c.evidence_ids,
                    "first_seen": c.first_seen,
                    "last_seen": c.last_seen,
                    "cluster_id": c.cluster_id
                }
                for c in self.consensus_clusters
            ],
            "aspect_coverage": self.aspect_coverage,
            "contradictions": [
                {
                    "topic": c.topic,
                    "conflict": c.conflict,
                    "details": c.details,
                    "detected_at": c.detected_at
                }
                for c in self.contradictions
            ],
            "recent_queries": [
                {
                    "query": q.query,
                    "intent": q.intent,
                    "timestamp": q.timestamp,
                    "request_id": q.request_id
                }
                for q in self.recent_queries
            ],
            "health_metrics": self.health_metrics
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EntityMemory":
        """从字典创建实例"""
        return cls(
            entity=data.get("entity", ""),
            brand=data.get("brand", ""),
            first_analyzed=data.get("first_analyzed", ""),
            last_analyzed=data.get("last_analyzed", ""),
            total_analyses=data.get("total_analyses", 0),
            consensus_clusters=[
                ConsensusCluster(**c) for c in data.get("consensus_clusters", [])
            ],
            aspect_coverage=data.get("aspect_coverage", {}),
            contradictions=[
                Contradiction(**c) for c in data.get("contradictions", [])
            ],
            recent_queries=[
                QueryRecord(**q) for q in data.get("recent_queries", [])
            ],
            health_metrics=data.get("health_metrics", {})
        )
