"""PDF rendering backed by WeasyPrint."""
from __future__ import annotations

import os
from pathlib import Path

from app.models.report_ir import ReportIR
from app.reports.html_renderer import render_report_html


class PdfRendererUnavailable(RuntimeError):
    """Raised when WeasyPrint or native PDF dependencies are unavailable."""


def _ensure_windows_dll_path() -> None:
    if os.name != "nt":
        return
    configured = os.getenv("WEASYPRINT_DLL_DIRECTORIES", "").strip()
    candidates = [Path(path) for path in configured.split(os.pathsep) if path]
    candidates.append(Path(r"C:\msys64\ucrt64\bin"))
    candidates.append(Path(r"C:\msys64\mingw64\bin"))

    for path in candidates:
        if not path.exists():
            continue
        path_text = str(path)
        os.environ["PATH"] = path_text + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(path_text)
        break


def render_report_pdf(report: ReportIR) -> bytes:
    """Render ReportIR to a native, text-selectable PDF with real links."""
    _ensure_windows_dll_path()
    try:
        from weasyprint import HTML
    except Exception as exc:  # pragma: no cover - depends on optional install
        raise PdfRendererUnavailable(
            "PDF 导出依赖 WeasyPrint 及其原生渲染库。当前环境无法加载 WeasyPrint；"
            "Windows 上通常还需要安装 MSYS2/GTK/Pango，并通过 PATH 或 "
            "WEASYPRINT_DLL_DIRECTORIES 暴露包含 libgobject-2.0-0.dll 的目录。"
        ) from exc

    html = render_report_html(report)
    base_url = Path(__file__).resolve().parents[3]
    try:
        return HTML(string=html, base_url=str(base_url)).write_pdf()
    except Exception as exc:
        raise PdfRendererUnavailable(f"WeasyPrint 生成 PDF 失败: {exc}") from exc
