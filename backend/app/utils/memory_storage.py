"""
长期记忆存储模块

负责将分析结果保存到本地文件系统，支持按产品实体和意图类型分类存储。
"""
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


# 基础目录
BASE_DIR = Path(__file__).parent.parent.parent / "data" / "long_term_memory"


@dataclass
class MemoryBlock:
    """长期记忆块"""
    entity: str                    # 产品实体（如 iPhone16）
    intent: str                   # 意图类型
    query: str                    # 原始查询
    analyzed_at: str              # 分析时间

    # 可复用内容
    key_aspects: list[str]        # 用户关注方面
    clusters: list[dict]          # 观点簇（精简版）
    note_ids: list[str]           # 用过的帖子 ID

    # 摘要（用于快速 grep/匹配）
    summary: str                  # 摘要
    keywords: list[str]           # 关键词


class MemoryStorage:
    """长期记忆存储管理器"""

    def __init__(self, base_dir: str | None = None):
        self._base_dir = Path(base_dir) if base_dir else BASE_DIR
        self._entities_dir = self._base_dir / "entities"
        self._index_file = self._base_dir / "index.json"

        # 确保目录存在
        self._entities_dir.mkdir(parents=True, exist_ok=True)

    def save(self, memory: MemoryBlock) -> str:
        """
        保存记忆块到本地文件系统

        Returns:
            保存的文件路径
        """
        # 清理实体名作为目录名
        entity_dir_name = self._sanitize_filename(memory.entity)

        # 创建实体目录
        entity_dir = self._entities_dir / entity_dir_name
        entity_dir.mkdir(parents=True, exist_ok=True)

        # 创建时间戳文件名
        timestamp = memory.analyzed_at.replace(":", "-").replace(".", "-")
        filename = f"{timestamp}.json"
        filepath = entity_dir / filename

        # 写入文件
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(asdict(memory), f, ensure_ascii=False, indent=2)

        # 更新索引
        self._update_index(memory, str(filepath))

        return str(filepath)

    def load(self, entity: str, max_age_days: int = 30) -> list[MemoryBlock]:
        """
        加载指定实体的历史记忆

        Args:
            entity: 产品实体名
            max_age_days: 只加载最近 N 天的记忆

        Returns:
            MemoryBlock 列表（按时间倒序）
        """
        entity_dir_name = self._sanitize_filename(entity)
        entity_dir = self._entities_dir / entity_dir_name

        if not entity_dir.exists():
            return []

        memories = []
        cutoff_time = time.time() - (max_age_days * 86400)

        for json_file in entity_dir.glob("*.json"):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # 检查时间
                analyzed_at = data.get("analyzed_at", "")
                if analyzed_at:
                    # 简单解析 ISO 格式时间
                    dt = analyzed_at.replace("T", " ").replace("Z", "")
                    try:
                        file_time = time.mktime(time.strptime(dt[:19], "%Y-%m-%d %H:%M:%S"))
                        if file_time < cutoff_time:
                            continue  # 过期，跳过
                    except ValueError:
                        pass

                memories.append(MemoryBlock(**data))
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                # 跳过损坏的文件
                continue

        # 按时间倒序
        memories.sort(key=lambda x: x.analyzed_at, reverse=True)
        return memories

    def search_by_keyword(self, keyword: str, entity: str | None = None) -> list[MemoryBlock]:
        """按关键词搜索记忆"""
        results = []

        # 确定搜索范围
        if entity:
            search_dirs = [self._entities_dir / self._sanitize_filename(entity)]
        else:
            search_dirs = list(self._entities_dir.iterdir())

        for search_dir in search_dirs:
            if not search_dir.is_dir():
                continue

            for json_file in search_dir.glob("*.json"):
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    # 检查关键词
                    keywords = data.get("keywords", [])
                    summary = data.get("summary", "")
                    query = data.get("query", "")

                    if (keyword.lower() in query.lower() or
                        keyword.lower() in summary.lower() or
                        any(keyword.lower() in kw.lower() for kw in keywords)):
                        results.append(MemoryBlock(**data))
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue

        return results

    def _update_index(self, memory: MemoryBlock, filepath: str) -> None:
        """更新全局索引"""
        index = {"entities": {}, "recent_queries": []}

        if self._index_file.exists():
            try:
                with open(self._index_file, "r", encoding="utf-8") as f:
                    index = json.load(f)
            except json.JSONDecodeError:
                pass

        # 更新实体索引
        entity = memory.entity
        if entity not in index["entities"]:
            index["entities"][entity] = []

        index["entities"][entity].append({
            "path": filepath,
            "analyzed_at": memory.analyzed_at,
            "query": memory.query,
            "intent": memory.intent,
            "summary": memory.summary[:100]  # 摘要前 100 字
        })

        # 更新最近查询列表（保留最近 50 条）
        index["recent_queries"].append({
            "query": memory.query,
            "entity": entity,
            "timestamp": memory.analyzed_at
        })
        index["recent_queries"] = index["recent_queries"][-50:]

        # 写入索引
        with open(self._index_file, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    def _sanitize_filename(self, name: str) -> str:
        """清理文件名中的非法字符"""
        # 替换非法字符为下划线
        name = re.sub(r'[<>:"/\\|?*]', "_", name)
        # 限制长度
        return name[:50]

    def cleanup_old(self, max_age_days: int = 30) -> int:
        """清理过期记忆，返回删除的文件数"""
        cutoff_time = time.time() - (max_age_days * 86400)
        deleted_count = 0

        for entity_dir in self._entities_dir.iterdir():
            if not entity_dir.is_dir():
                continue

            for json_file in entity_dir.glob("*.json"):
                try:
                    mtime = json_file.stat().st_mtime
                    if mtime < cutoff_time:
                        json_file.unlink()
                        deleted_count += 1
                except OSError:
                    continue

        return deleted_count