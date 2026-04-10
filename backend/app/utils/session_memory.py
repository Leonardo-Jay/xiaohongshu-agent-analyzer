"""
短期会话记忆模块

负责管理单次会话内的记忆，包括查询历史、已用帖子、观点簇等。
"""
import time
from dataclasses import dataclass, field
from typing import Any


# 会话超时时间（秒）- 30分钟
SESSION_TIMEOUT = 1800


@dataclass
class SessionMemory:
    """短期记忆 - 单次会话"""
    session_id: str
    entity: str = ""                    # 当前产品实体
    query_history: list[str] = field(default_factory=list)  # 查询历史
    used_note_ids: set[str] = field(default_factory=set)    # 已爬取帖子
    clusters: list[dict] = field(default_factory=list)      # 观点簇
    last_intent: str = ""               # 上次意图
    last_active: float = field(default_factory=time.time)   # 最后活跃时间

    def update(self, query: str, entity: str, intent: str,
               note_ids: list[str], clusters: list[dict]) -> None:
        """更新会话记忆"""
        # 更新时间
        self.last_active = time.time()

        # 更新实体（如果新查询有实体）
        if entity and entity != self.entity:
            self.entity = entity

        # 更新查询历史（保留最近5个）
        self.query_history.append(query)
        if len(self.query_history) > 5:
            self.query_history = self.query_history[-5:]

        # 更新意图
        if intent:
            self.last_intent = intent

        # 更新已用帖子
        self.used_note_ids.update(note_ids)
        # 保留最近50个
        if len(self.used_note_ids) > 50:
            self.used_note_ids = set(list(self.used_note_ids)[-50:])

        # 更新观点簇（保留最新的）
        if clusters:
            self.clusters = clusters[:10]  # 只保留TOP10

    def is_expired(self) -> bool:
        """检查会话是否过期"""
        return (time.time() - self.last_active) > SESSION_TIMEOUT

    def is_same_entity(self, entity: str) -> bool:
        """检查是否查询同一产品"""
        if not entity or not self.entity:
            return False
        return entity.lower() == self.entity.lower()

    def is_new_aspect(self, current_query: str) -> bool:
        """检查是否在查询新角度"""
        if not self.query_history:
            return True

        # 简单判断：查询长度差异大或关键词不同
        last_query = self.query_history[-1]

        # 如果查询完全相同，认为不是新角度
        if current_query.strip() == last_query.strip():
            return False

        # 提取关键词（简单方法：长度差异超过30%）
        len_ratio = len(current_query) / max(len(last_query), 1)
        if len_ratio > 1.5 or len_ratio < 0.7:
            return True

        # 有新的关键词（简单判断：包含新词）
        common_words = set(current_query) & set(last_query)
        if len(common_words) < max(len(set(current_query)), 1) * 0.5:
            return True

        return False

    def get_exclude_note_ids(self) -> list[str]:
        """获取需要排除的帖子ID"""
        return list(self.used_note_ids)

    def get_reduction_factor(self) -> float:
        """获取爬取量减少因子"""
        if not self.used_note_ids:
            return 0.0  # 无历史，完全爬取

        # 已爬取越多，减少越多（最多减少50%）
        count = len(self.used_note_ids)
        if count >= 30:
            return 0.5
        elif count >= 15:
            return 0.3
        elif count >= 5:
            return 0.2
        else:
            return 0.1


class SessionMemoryManager:
    """短期会话记忆管理器"""

    def __init__(self):
        self._sessions: dict[str, SessionMemory] = {}

    def get_session(self, session_id: str) -> SessionMemory:
        """获取或创建会话记忆"""
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionMemory(session_id=session_id)
        else:
            # 检查是否过期
            if self._sessions[session_id].is_expired():
                logger.info(f"[SessionMemory] 会话 {session_id} 已过期，创建新会话")
                self._sessions[session_id] = SessionMemory(session_id=session_id)

        return self._sessions[session_id]

    def update_session(
        self,
        session_id: str,
        query: str,
        entity: str,
        intent: str,
        note_ids: list[str],
        clusters: list[dict]
    ) -> None:
        """更新会话记忆"""
        session = self.get_session(session_id)
        session.update(query, entity, intent, note_ids, clusters)

    def cleanup_expired(self) -> int:
        """清理过期会话，返回清理数量"""
        expired = []
        for sid, session in self._sessions.items():
            if session.is_expired():
                expired.append(sid)

        for sid in expired:
            del self._sessions[sid]

        return len(expired)

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        return {
            "active_sessions": len(self._sessions),
            "total_queries": sum(len(s.query_history) for s in self._sessions.values()),
        }


# 全局实例
_session_manager: SessionMemoryManager | None = None


def get_session_manager() -> SessionMemoryManager:
    """获取会话管理单例"""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionMemoryManager()
    return _session_manager