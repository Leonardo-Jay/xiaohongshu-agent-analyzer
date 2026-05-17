import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

try:
    from rank_bm25 import BM25Okapi
except ModuleNotFoundError:
    BM25Okapi = None


BM25_AVAILABLE = BM25Okapi is not None

_ALNUM_RE = re.compile(r"[a-z]+[0-9]*|[0-9]+(?:\.[0-9]+)?")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_SPACES_RE = re.compile(r"\s+")

_STOP_TOKENS = {
    "怎么样",
    "怎么",
    "如何",
    "可以吗",
    "好吗",
    "好不好",
    "会不会",
    "不会",
    "值得买吗",
    "值得买",
    "分析",
    "评价",
    "口碑",
    "看看",
    "帮我",
    "一下",
    "这个",
    "那个",
    "是否",
    "有没有",
    "问题",
    "建议",
    "情况",
    "表现",
    "体验",
    "吗",
    "呢",
    "啊",
    "的",
    "了",
    "和",
    "与",
    "及",
    "或",
}

_FIELD_WEIGHTS = {
    "primary_aspects": 5,
    "sub_aspects": 3,
    "synonym_aspects": 3,
    "topic": 2,
}


@dataclass
class BM25Hit:
    cluster_index: int
    raw_score: float
    normalized_score: float
    matched_terms: list[str] = field(default_factory=list)


def normalize_text(text: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    normalized = normalized.replace("_", " ").replace("-", " ")
    return _SPACES_RE.sub(" ", normalized).strip()


def strip_entity_terms(text: str, entity: str | None = None) -> str:
    normalized = normalize_text(text)
    if not entity:
        return normalized

    entity_norm = normalize_text(entity)
    candidates = {entity_norm, entity_norm.replace(" ", "")}
    stripped = normalized
    stripped_compact = stripped.replace(" ", "")

    for candidate in sorted(candidates, key=len, reverse=True):
        if not candidate:
            continue
        stripped = stripped.replace(candidate, " ")
        stripped_compact = stripped_compact.replace(candidate.replace(" ", ""), " ")

    return _SPACES_RE.sub(" ", stripped if stripped.strip() else stripped_compact).strip()


def tokenize_for_bm25(text: Any, extra_stop_terms: set[str] | None = None) -> list[str]:
    normalized = normalize_text(text)
    stop_tokens = set(_STOP_TOKENS)
    if extra_stop_terms:
        stop_tokens.update(normalize_text(term) for term in extra_stop_terms if term)

    tokens: list[str] = []
    tokens.extend(_ALNUM_RE.findall(normalized))

    for sequence in _CJK_RE.findall(normalized):
        if 1 < len(sequence) <= 8:
            tokens.append(sequence)

        for size in (2, 3):
            if len(sequence) < size:
                continue
            tokens.extend(sequence[i:i + size] for i in range(len(sequence) - size + 1))

    return [
        token
        for token in tokens
        if token
        and token not in stop_tokens
        and not (len(token) == 1 and token.isascii())
    ]


def _get_values(cluster: Any, field_name: str) -> list[str]:
    if isinstance(cluster, dict):
        value = cluster.get(field_name, [])
    else:
        value = getattr(cluster, field_name, [])

    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return [str(value)]


def build_cluster_document(cluster: Any) -> list[str]:
    document: list[str] = []
    for field_name, weight in _FIELD_WEIGHTS.items():
        for value in _get_values(cluster, field_name):
            field_tokens = tokenize_for_bm25(value)
            for _ in range(weight):
                document.extend(field_tokens)
    return document


class BM25ClusterIndex:
    def __init__(self, clusters: list[Any]):
        self.clusters = list(clusters)
        self.documents = [build_cluster_document(cluster) for cluster in self.clusters]
        self._document_sets = [set(document) for document in self.documents]
        self._bm25 = BM25Okapi(self.documents) if BM25_AVAILABLE and self.documents else None

    @property
    def available(self) -> bool:
        return self._bm25 is not None

    def search(self, query_text: str, top_k: int = 12, entity: str | None = None) -> list[BM25Hit]:
        if not self._bm25:
            return []

        clean_query = strip_entity_terms(query_text, entity)
        query_tokens = tokenize_for_bm25(clean_query)
        if not query_tokens:
            return []

        raw_scores = [float(score) for score in self._bm25.get_scores(query_tokens)]
        max_score = max(raw_scores) if raw_scores else 0.0
        if max_score <= 0:
            return []

        hits = []
        query_token_set = set(query_tokens)
        for index, raw_score in enumerate(raw_scores):
            if raw_score <= 0:
                continue
            matched_terms = sorted(query_token_set & self._document_sets[index])
            hits.append(BM25Hit(
                cluster_index=index,
                raw_score=raw_score,
                normalized_score=raw_score / max_score,
                matched_terms=matched_terms,
            ))

        hits.sort(key=lambda hit: hit.raw_score, reverse=True)
        return hits[:top_k]
