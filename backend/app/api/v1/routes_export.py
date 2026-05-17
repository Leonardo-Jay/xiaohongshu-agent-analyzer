"""Export routes for report artifacts."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError

from app.models.report_ir import ReportIR
from app.reports.pdf_renderer import PdfRendererUnavailable, render_report_pdf

router = APIRouter(prefix="/api/v1/export", tags=["export"])


class PdfExportRequest(BaseModel):
    report_ir: dict[str, Any] = Field(..., description="Report IR v1 structured report")


def _safe_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", title).strip(" ._")
    if not name:
        name = "report"
    return name[:80]


@router.post("/pdf", summary="导出 Report IR PDF")
async def export_pdf(req: PdfExportRequest) -> Response:
    if not req.report_ir:
        raise HTTPException(status_code=400, detail="缺少 report_ir，无法生成结构化 PDF")

    try:
        report = ReportIR.model_validate(req.report_ir)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    try:
        pdf_bytes = render_report_pdf(report)
    except PdfRendererUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    filename = f"{_safe_filename(report.title)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    headers = {
        "Content-Disposition": (
            f"attachment; filename=report.pdf; filename*=UTF-8''{quote(filename)}"
        )
    }
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
