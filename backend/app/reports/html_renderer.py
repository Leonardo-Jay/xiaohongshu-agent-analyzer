"""Render ReportIR into print-oriented HTML."""
from __future__ import annotations

from html import escape

from app.models.report_ir import ReportIR

_CHART_COLUMN_LABELS = {
    "label": "指标",
    "value": "当前表现",
    "insight": "解读",
    "basis": "依据",
    "note": "说明",
}


def _e(value: object) -> str:
    return escape(str(value or ""), quote=True)


def _citation_number_map(report: ReportIR) -> dict[str, int]:
    return {citation.id: idx + 1 for idx, citation in enumerate(report.citations)}


def _citation_links(citation_ids: list[str], number_map: dict[str, int]) -> str:
    links = []
    seen = set()
    for citation_id in citation_ids:
        number = number_map.get(citation_id)
        if not number or number in seen:
            continue
        seen.add(number)
        links.append(f'<a class="citation" href="#ref-{number}">[{number}]</a>')
    if not links:
        return ""
    return '<span class="citations">' + "".join(links) + "</span>"


def _render_blocks(report: ReportIR) -> str:
    number_map = _citation_number_map(report)
    parts: list[str] = []
    for section in report.sections:
        parts.append(f'<section class="report-section" id="{_e(section.id)}">')
        parts.append(f"<h2>{_e(section.title)}</h2>")
        for block in section.blocks:
            citations = _citation_links(block.citation_ids, number_map)
            if block.type == "subheading":
                if block.text:
                    parts.append(f"<h3>{_e(block.text)}</h3>")
                continue
            if block.type == "list":
                items = block.items or ([block.text] if block.text else [])
                if items:
                    parts.append("<ul>")
                    for item in items:
                        if item:
                            parts.append(f"<li>{_e(item)}{citations}</li>")
                    parts.append("</ul>")
                continue
            if block.text:
                parts.append(f"<p>{_e(block.text)}{citations}</p>")
        parts.append("</section>")
    return "\n".join(parts)


def _render_summary(report: ReportIR) -> str:
    if not report.summary_cards:
        return ""
    rows = []
    for card in report.summary_cards:
        if card.label and card.value:
            rows.append(
                f'<li><strong>{_e(card.label)}</strong><span>{_e(card.value)}</span></li>'
            )
    if not rows:
        return ""
    return '<section class="summary-cards"><h2>关键摘要</h2><ul>' + "".join(rows) + "</ul></section>"


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


def _render_charts(report: ReportIR) -> str:
    if not report.charts:
        return ""
    parts = ['<section class="charts"><h2>数据概览</h2>']
    rendered = 0
    for chart in report.charts:
        if not chart.data:
            continue
        rendered += 1
        parts.append(f"<h3>{_e(chart.title)}</h3>")
        columns = _chart_columns(chart.data)
        header = "".join(f"<th>{_e(title)}</th>" for _, title in columns)
        parts.append(f"<table><thead><tr>{header}</tr></thead><tbody>")
        for row in chart.data:
            cells = "".join(f"<td>{_e(_chart_row_value(row, key))}</td>" for key, _ in columns)
            parts.append(f"<tr>{cells}</tr>")
        parts.append("</tbody></table>")
    parts.append("</section>")
    return "\n".join(parts) if rendered else ""


def _render_limitations(report: ReportIR) -> str:
    limitations = [item for item in report.metadata.limitations if item]
    if not limitations:
        return ""
    items = "".join(f"<li>{_e(item)}</li>" for item in limitations)
    return f'<section class="limitations"><h2>局限性</h2><ul>{items}</ul></section>'


def _render_references(report: ReportIR) -> str:
    if not report.citations:
        return ""
    parts = ['<section class="references" id="references"><h2>参考证据</h2>']
    for idx, citation in enumerate(report.citations, start=1):
        source = citation.source_title or "用户原话"
        if citation.source_url:
            source_html = f'<a class="source-link" href="{_e(citation.source_url)}">{_e(source)}</a>'
        else:
            source_html = f'<span class="source-link muted">{_e(source)}</span>'
        parts.append(
            f"""
            <article class="reference-card" id="ref-{idx}">
              <div class="reference-head">
                <a class="backlink" href="#references">[{idx}]</a>
                <span class="reference-topic">{_e(citation.topic)}</span>
                <span class="sentiment">{_e(citation.sentiment)}</span>
              </div>
              <div class="reference-source">{source_html}</div>
              <blockquote>{_e(citation.quote)}</blockquote>
            </article>
            """
        )
    parts.append("</section>")
    return "\n".join(parts)


def render_report_html(report: ReportIR) -> str:
    """Render a complete print-ready HTML document for PDF generation."""
    meta = report.metadata
    confidence = f"{meta.confidence_score:.0%}" if meta.confidence_score else "待评估"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>{_e(report.title)}</title>
  <style>
    @page {{
      size: A4;
      margin: 18mm 16mm 20mm;
      @bottom-center {{
        content: "第 " counter(page) " 页 / 共 " counter(pages) " 页";
        color: #64748b;
        font-size: 9pt;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: "Microsoft YaHei", "Noto Sans CJK SC", "Source Han Sans SC", sans-serif;
      color: #111827;
      font-size: 11pt;
      line-height: 1.75;
    }}
    h1 {{
      bookmark-level: 1;
      font-size: 24pt;
      line-height: 1.25;
      text-align: center;
      margin: 0 0 10mm;
      color: #0f172a;
    }}
    h2 {{
      bookmark-level: 2;
      break-after: avoid;
      margin: 8mm 0 3mm;
      padding-left: 3mm;
      border-left: 3pt solid #3b82f6;
      color: #1d4ed8;
      font-size: 15pt;
    }}
    h3 {{
      bookmark-level: 3;
      break-after: avoid;
      margin: 5mm 0 2mm;
      color: #475569;
      font-size: 12pt;
    }}
    p {{ margin: 0 0 3mm; text-align: justify; }}
    ul {{ margin: 0 0 3mm 5mm; padding-left: 5mm; }}
    li {{ margin: 0 0 1.5mm; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin: 2mm 0 5mm;
      break-inside: avoid;
    }}
    th, td {{
      border: 0.5pt solid #cbd5e1;
      padding: 2mm 2.5mm;
    }}
    th {{ background: #eff6ff; color: #1e40af; text-align: left; }}
    .meta {{
      margin: 0 auto 8mm;
      padding: 3mm 4mm;
      border: 0.5pt solid #dbeafe;
      background: #f8fafc;
      color: #475569;
      text-align: center;
      border-radius: 4pt;
    }}
    .summary-cards ul {{
      list-style: none;
      margin-left: 0;
      padding-left: 0;
    }}
    .summary-cards li {{
      break-inside: avoid;
      border: 0.5pt solid #dbeafe;
      background: #f8fafc;
      border-radius: 4pt;
      padding: 2.5mm 3mm;
      margin-bottom: 2mm;
    }}
    .summary-cards strong {{ display: inline-block; min-width: 24mm; color: #1e40af; }}
    a {{ color: #2563eb; text-decoration: none; }}
    .citations {{
      white-space: nowrap;
      margin-left: 1mm;
    }}
    .citation {{
      font-size: 9pt;
      font-weight: 700;
      padding: 0 0.8mm;
    }}
    .reference-card {{
      break-inside: avoid;
      margin: 0 0 4mm;
      padding: 3mm;
      border-left: 3pt solid #cbd5e1;
      background: #f8fafc;
    }}
    .reference-head {{
      display: flex;
      gap: 2mm;
      align-items: baseline;
      margin-bottom: 1mm;
      font-weight: 700;
    }}
    .reference-topic {{ color: #0f172a; }}
    .sentiment {{
      margin-left: auto;
      font-size: 9pt;
      color: #64748b;
      font-weight: 400;
    }}
    .reference-source {{
      margin-bottom: 1.5mm;
      color: #475569;
    }}
    blockquote {{
      margin: 0;
      padding: 2mm 2.5mm;
      color: #334155;
      background: #ffffff;
      border-left: 2pt solid #bfdbfe;
    }}
    .muted {{ color: #64748b; }}
    .report-section, .limitations, .references, .charts {{ break-inside: auto; }}
  </style>
</head>
<body>
  <h1>{_e(report.title)}</h1>
  <div class="meta">
    查询：{_e(meta.query)}　
    样本：{meta.post_count} 篇帖子 / {meta.comment_count} 条评论　
    置信度：{_e(confidence)}
  </div>
  {_render_summary(report)}
  {_render_charts(report)}
  {_render_blocks(report)}
  {_render_limitations(report)}
  {_render_references(report)}
</body>
</html>"""
