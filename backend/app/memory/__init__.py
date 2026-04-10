"""
记忆机制模块

从 LLM Wiki 架构借鉴：
- 知识编译：分析时预处理
- 交叉引用：实体-概念-证据网络
- 持续维护：增量更新
- 证据可追溯：防幻觉
"""
from app.memory.memory_types import (
    ConsensusCluster,
    Contradiction,
    QueryRecord,
    EntityMemory,
)
from app.memory.evidence_saver import EvidenceSaver, get_evidence_saver
from app.memory.evidence_selector import EvidenceSelector, get_evidence_selector
from app.memory.memory_manager import MemoryManager, get_memory_manager
from app.memory.contradiction_detector import ContradictionDetector, get_contradiction_detector
from app.memory.trend_calculator import TrendCalculator, get_trend_calculator
from app.memory.concept_memory import ConceptMemory, get_concept_memory
from app.memory.linter import MemoryLinter, get_memory_linter

__all__ = [
    "ConsensusCluster",
    "Contradiction",
    "QueryRecord",
    "EntityMemory",
    "EvidenceSaver",
    "get_evidence_saver",
    "EvidenceSelector",
    "get_evidence_selector",
    "MemoryManager",
    "get_memory_manager",
    "ContradictionDetector",
    "get_contradiction_detector",
    "TrendCalculator",
    "get_trend_calculator",
    "ConceptMemory",
    "get_concept_memory",
    "MemoryLinter",
    "get_memory_linter",
]
