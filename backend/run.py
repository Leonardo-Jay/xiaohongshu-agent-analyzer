
import asyncio
import os
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parent


def _project_venv_python():
    if sys.platform == "win32":
        return BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
    return BACKEND_DIR / ".venv" / "bin" / "python"


def _maybe_reexec_with_project_venv():
    """Allow `python run.py` to work even when the shell points at system Python."""
    venv_python = _project_venv_python()
    if not venv_python.exists():
        return

    try:
        current_python = Path(sys.executable).resolve()
        target_python = venv_python.resolve()
    except OSError:
        current_python = Path(sys.executable)
        target_python = venv_python

    if current_python != target_python:
        os.execv(str(target_python), [str(target_python), str(Path(__file__).resolve())] + sys.argv[1:])


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        if exc.name != "uvicorn":
            raise
        raise SystemExit(
            "未找到 uvicorn。请先在 backend 目录执行：\n"
            r"  .\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
        ) from exc

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        workers=1,  # Windows 下改成 1，Linux 可以增加核数自动 flow.
        reload=False,
    )


if __name__ == "__main__":
    _maybe_reexec_with_project_venv()
    main()
