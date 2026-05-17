"""Render ReportIR into user-facing formats."""
from __future__ import annotations

from app.models.report_ir import Citation, ReportIR

_CHART_COLUMN_LABELS = {
    "label": "指标",
    "value": "当前表现",
    "insight": "解读",
    "basis": "依据",
    "note": "说明",
}


def _citation_number_map(citations: list[Citation]) -> dict[str, int]:
    return {citation.id: idx + 1 for idx, citation in enumerate(citations)}


def _format_citation_marks(citation_ids: list[str], number_map: dict[str, int]) -> str:
    numbers = []
    for citation_id in citation_ids:
        number = number_map.get(citation_id)
        if number and number not in numbers:
            numbers.append(number)
    if not numbers:
        return ""
    return "".join(f"[{number}]" for number in numbers)


def _chart_row_value(row: dict, key: str) -> object:
    if key == "label":
        return row.get("label", row.get("name", ""))
    if key == "value":
        return row.get("value", row.get("count", ""))
    return row.get(key, "")


def _chart_columns(rows: list[dict]) -> list[tuple[str, str]]:
    columns = [
        (key, title)
        for key, title in _CHART_COLUMN_LABELS.items()
        if any(_chart_row_value(row, key) not in (None, "") for row in rows)
    ]
    seen = {key for key, _ in columns}
    for row in rows:
        for key in row.keys():
            if key in {"name", "count"} or key in seen:
                continue
            columns.append((key, str(key)))
            seen.add(key)
    return columns or [("label", "指标"), ("value", "当前表现")]


def _md_cell(value: object) -> str:
    return str(value or "").replace("\n", " ").replace("|", "\\|").strip()


def render_markdown(report: ReportIR) -> str:
    """Render ReportIR v1 to Markdown.

    The renderer is intentionally deterministic: LLM output defines content,
    while headings, citations, metadata, charts, and evidence formatting are
    controlled by code.
    """
    number_map = _citation_number_map(report.citations)
    lines: list[str] = [f"# {report.title}", ""]

    meta = report.metadata
    if meta.post_count or meta.comment_count or meta.confidence_score:
        lines.append(
            f"> 样本：{meta.post_count} 篇帖子，{meta.comment_count} 条评论；"
            f"置信度：{meta.confidence_score:.0%}"
        )
        lines.append("")

    if report.summary_cards:
        lines.append("## 关键摘要")
        for card in report.summary_cards:
            value = card.value.strip()
            if value:
                lines.append(f"- **{card.label}**：{value}")
        lines.append("")

    if report.charts:
        lines.append("## 数据概览")
        for chart in report.charts:
            if not chart.data:
                continue
            lines.append(f"### {chart.title}")
            columns = _chart_columns(chart.data)
            lines.append("| " + " | ".join(title for _, title in columns) + " |")
            lines.append("| " + " | ".join("---" for _ in columns) + " |")
            for row in chart.data:
                cells = [_md_cell(_chart_row_value(row, key)) for key, _ in columns]
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")

    for section in report.sections:
        lines.append(f"## {section.title}")
        lines.append("")
        for block in section.blocks:
            text = block.text.strip()
            marks = _format_citation_marks(block.citation_ids, number_map)

            if block.type == "subheading":
                if text:
                    lines.append(f"### {text}")
                    lines.append("")
                continue

            if block.type == "list":
                items = block.items or ([text] if text else [])
                for item in items:
                    item_text = item.strip()
                    if item_text:
                        lines.append(f"- {item_text}{marks}")
                lines.append("")
                continue

            if text:
                lines.append(f"{text}{marks}")
                lines.append("")

    if meta.limitations:
        lines.append("## 局限性")
        for limitation in meta.limitations:
            if limitation:
                lines.append(f"- {limitation}")
        lines.append("")

    if report.citations:
        lines.append("## 参考证据")
        for citation in report.citations:
            number = number_map[citation.id]
            source = citation.source_title or "用户原话"
            quote = citation.quote.strip()
            url = citation.source_url.strip()
            if url:
                lines.append(f"[{number}] [{source}]({url})：{quote}")
            else:
                lines.append(f"[{number}] {source}：{quote}")
        lines.append("")

    return "\n".join(lines).strip()
