"""
短期会话记忆模块

负责管理单次会话内的路由状态。

短期记忆只保存会话连续性所需的轻量信息：
1. 上一轮 Orchestrator 的最终意图分析结果
2. 上一轮成功写入长期记忆后的 run 快照引用
3. 最近几轮 query 元信息

观点簇、证据、帖子 ID 等事实数据只保存在长期记忆中。
"""
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


# 会话超时时间（秒）- 30分钟
SESSION_TIMEOUT = 1800


@dataclass
class SessionMemory:
    """短期记忆 - 单次会话路由状态"""
    session_id: str
    last_active: float = field(default_factory=time.time)   # 最后活跃时间
    last_intent_frame: dict[str, Any] = field(default_factory=dict)
    last_run_ref: dict[str, Any] = field(default_factory=dict)
    recent_turns: list[dict[str, Any]] = field(default_factory=list)

    def update(
        self,
        intent_frame: dict[str, Any],
        run_ref: dict[str, Any],
    ) -> None:
        """更新会话记忆"""
        self.last_active = time.time()

        if intent_frame:
            self.last_intent_frame = intent_frame

        if run_ref:
            self.last_run_ref = run_ref

        turn = {
            "query": intent_frame.get("raw_query", ""),
            "rewritten_query": intent_frame.get("rewritten_query", ""),
            "intent": intent_frame.get("intent", "general"),
            "entity": (intent_frame.get("product_entities") or [""])[0],
            "key_aspects": intent_frame.get("key_aspects", []),
            "run_id": run_ref.get("run_id", ""),
            "committed_at": run_ref.get("committed_at", ""),
        }
        self.recent_turns.append(turn)
        if len(self.recent_turns) > 5:
            self.recent_turns = self.recent_turns[-5:]

    def is_expired(self) -> bool:
        """检查会话是否过期"""
        return (time.time() - self.last_active) > SESSION_TIMEOUT

    def get_context(self) -> dict[str, Any]:
        """返回供工作流使用的会话上下文"""
        self.last_active = time.time()
        return {
            "last_intent_frame": self.last_intent_frame,
            "last_run_ref": self.last_run_ref,
            "recent_turns": self.recent_turns,
        }


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
        intent_frame: dict[str, Any],
        run_ref: dict[str, Any],
    ) -> None:
        """更新会话记忆"""
        session = self.get_session(session_id)
        session.update(intent_frame, run_ref)

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
            "total_queries": sum(len(s.recent_turns) for s in self._sessions.values()),
        }


# 全局实例
_session_manager: SessionMemoryManager | None = None


def get_session_manager() -> SessionMemoryManager:
    """获取会话管理单例"""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionMemoryManager()
    return _session_manager
