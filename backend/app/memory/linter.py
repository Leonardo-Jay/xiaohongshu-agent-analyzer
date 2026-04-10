"""
记忆库健康检查模块

定期检查记忆库健康，发现：
1. 证据完整性：观点簇的证据引用是否都存在
2. 过时数据：>30天未更新的实体
3. 孤立证据：无引用的评论/帖子文件
4. 疑似幻觉：无证据支撑的观点簇
"""
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class LintIssue:
    """健康检查问题"""
    type: str               # 问题类型
    entity: str             # 相关实体
    details: str            # 详细描述
    severity: str           # 严重程度：high/medium/low
    ref: str = ""           # 相关引用路径


class MemoryLinter:
    """记忆库健康检查器"""

    def __init__(self, base_dir: str | None = None):
        self._base_dir = Path(base_dir) if base_dir else Path(__file__).parent.parent.parent / "data" / "memory"
        self._entities_dir = self._base_dir / "entities"

    def lint(self) -> list[dict[str, Any]]:
        """
        执行完整健康检查

        Returns:
            问题列表
        """
        issues = []

        # 1. 检查证据完整性
        issues.extend(self._check_evidence_integrity())

        # 2. 检查过时数据
        issues.extend(self._check_stale_data())

        # 3. 检查孤立证据
        issues.extend(self._check_orphan_evidence())

        # 4. 检查疑似幻觉
        issues.extend(self._check_hallucination())

        logger.info(f"[MemoryLinter] 健康检查完成: 发现 {len(issues)} 个问题")

        return [
            {
                "type": issue.type,
                "entity": issue.entity,
                "details": issue.details,
                "severity": issue.severity,
                "ref": issue.ref
            }
            for issue in issues
        ]

    def _check_evidence_integrity(self) -> list[LintIssue]:
        """检查证据完整性：观点簇的证据引用是否都存在"""
        issues = []

        if not self._entities_dir.exists():
            return issues

        for entity_dir in self._entities_dir.iterdir():
            if not entity_dir.is_dir():
                continue

            memory_file = entity_dir / "memory.json"
            if not memory_file.exists():
                continue

            try:
                with open(memory_file, "r", encoding="utf-8") as f:
                    memory_data = json.load(f)

                entity = memory_data.get("entity", entity_dir.name)
                clusters = memory_data.get("consensus_clusters", [])

                for cluster in clusters:
                    topic = cluster.get("topic", "")
                    evidence_ids = cluster.get("evidence_ids", [])

                    for evidence_id in evidence_ids:
                        # 检查证据文件是否存在
                        evidence_path = entity_dir / "evidence" / f"{evidence_id}.json"
                        if not evidence_path.exists():
                            issues.append(LintIssue(
                                type="missing_evidence",
                                entity=entity,
                                details=f"观点簇 '{topic}' 的证据文件不存在",
                                severity="high",
                                ref=evidence_id
                            ))

            except Exception as e:
                logger.warning(f"[MemoryLinter] 检查实体 {entity_dir.name} 失败: {e}")

        return issues

    def _check_stale_data(self) -> list[LintIssue]:
        """检查过时数据：>30天未更新的实体"""
        issues = []

        if not self._entities_dir.exists():
            return issues

        cutoff_time = time.time() - (30 * 86400)  # 30天前

        for entity_dir in self._entities_dir.iterdir():
            if not entity_dir.is_dir():
                continue

            memory_file = entity_dir / "memory.json"
            if not memory_file.exists():
                continue

            try:
                # 检查文件修改时间
                mtime = memory_file.stat().st_mtime
                if mtime < cutoff_time:
                    with open(memory_file, "r", encoding="utf-8") as f:
                        memory_data = json.load(f)

                    entity = memory_data.get("entity", entity_dir.name)
                    last_analyzed = memory_data.get("last_analyzed", "")

                    issues.append(LintIssue(
                        type="stale_data",
                        entity=entity,
                        details=f"实体已超过 30 天未更新（最后更新: {last_analyzed}）",
                        severity="medium"
                    ))

            except Exception as e:
                logger.warning(f"[MemoryLinter] 检查实体 {entity_dir.name} 失败: {e}")

        return issues

    def _check_orphan_evidence(self) -> list[LintIssue]:
        """检查孤立证据：无引用的评论/帖子文件"""
        issues = []

        if not self._entities_dir.exists():
            return issues

        for entity_dir in self._entities_dir.iterdir():
            if not entity_dir.is_dir():
                continue

            memory_file = entity_dir / "memory.json"
            evidence_dir = entity_dir / "evidence"

            if not memory_file.exists() or not evidence_dir.exists():
                continue

            try:
                # 收集所有引用的证据
                with open(memory_file, "r", encoding="utf-8") as f:
                    memory_data = json.load(f)

                all_refs = set()
                clusters = memory_data.get("consensus_clusters", [])
                for cluster in clusters:
                    all_refs.update(cluster.get("evidence_ids", []))

                # 检查证据目录中的文件
                for evidence_file in evidence_dir.glob("*.json"):
                    evidence_id = evidence_file.stem

                    if evidence_id not in all_refs:
                        entity = memory_data.get("entity", entity_dir.name)
                        issues.append(LintIssue(
                            type="orphan_evidence",
                            entity=entity,
                            details=f"证据文件无引用",
                            severity="low",
                            ref=evidence_id
                        ))

            except Exception as e:
                logger.warning(f"[MemoryLinter] 检查实体 {entity_dir.name} 失败: {e}")

        return issues

    def _check_hallucination(self) -> list[LintIssue]:
        """检查疑似幻觉：无证据支撑的观点簇"""
        issues = []

        if not self._entities_dir.exists():
            return issues

        for entity_dir in self._entities_dir.iterdir():
            if not entity_dir.is_dir():
                continue

            memory_file = entity_dir / "memory.json"
            if not memory_file.exists():
                continue

            try:
                with open(memory_file, "r", encoding="utf-8") as f:
                    memory_data = json.load(f)

                entity = memory_data.get("entity", entity_dir.name)
                clusters = memory_data.get("consensus_clusters", [])

                for cluster in clusters:
                    topic = cluster.get("topic", "")
                    evidence_ids = cluster.get("evidence_ids", [])
                    frequency = cluster.get("frequency", 0)

                    # 观点簇出现 >= 2 次但仍无证据
                    if frequency >= 2 and not evidence_ids:
                        issues.append(LintIssue(
                            type="potential_hallucination",
                            entity=entity,
                            details=f"观点簇 '{topic}' 出现 {frequency} 次但仍无证据支撑，疑似幻觉",
                            severity="high"
                        ))

            except Exception as e:
                logger.warning(f"[MemoryLinter] 检查实体 {entity_dir.name} 失败: {e}")

        return issues

    def cleanup_orphan_evidence(self) -> int:
        """
        清理孤立证据文件

        Returns:
            删除的文件数量
        """
        deleted_count = 0

        issues = self._check_orphan_evidence()
        for issue in issues:
            if issue.type == "orphan_evidence" and issue.ref:
                # 删除孤立证据
                entity_dir = self._entities_dir / issue.entity
                evidence_path = entity_dir / "evidence" / f"{issue.ref}.json"

                try:
                    if evidence_path.exists():
                        evidence_path.unlink()
                        deleted_count += 1
                        logger.info(f"[MemoryLinter] 删除孤立证据: {evidence_path}")
                except Exception as e:
                    logger.warning(f"[MemoryLinter] 删除失败: {e}")

        return deleted_count


# 全局实例
_memory_linter: MemoryLinter | None = None


def get_memory_linter() -> MemoryLinter:
    """获取记忆库健康检查器单例"""
    global _memory_linter
    if _memory_linter is None:
        _memory_linter = MemoryLinter()
    return _memory_linter
