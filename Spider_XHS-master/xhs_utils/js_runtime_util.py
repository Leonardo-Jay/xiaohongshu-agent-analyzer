import os
import shutil
import subprocess
from pathlib import Path


_CONFIGURED = False


def _iter_node_candidates():
    for env_name in ("XHS_NODE_PATH", "NODE_BINARY", "NODE_EXE"):
        value = os.getenv(env_name)
        if value:
            yield Path(value)

    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        yield Path(found)

    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            yield Path(local_app_data) / "OpenAI" / "Codex" / "bin" / "node.exe"
        yield Path(r"C:\Program Files\nodejs\node.exe")
        yield Path(r"C:\Program Files (x86)\nodejs\node.exe")
    else:
        yield Path("/usr/local/bin/node")
        yield Path("/usr/bin/node")


def _is_executable_node(path: Path) -> bool:
    try:
        result = subprocess.run(
            [str(path), "-v"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_node_runtime() -> str | None:
    """Prefer a real Node.js runtime for PyExecJS.

    On Windows, `node` can resolve to a WindowsApps execution alias that Python
    cannot spawn, causing PyExecJS to fail or fall back to legacy JScript.
    Putting a verified Node directory first in PATH keeps bundled modern JS
    files running under V8.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return os.getenv("XHS_NODE_RESOLVED")

    os.environ.setdefault("EXECJS_RUNTIME", "Node")
    seen: set[str] = set()
    for candidate in _iter_node_candidates():
        normalized = str(candidate)
        key = normalized.lower() if os.name == "nt" else normalized
        if key in seen:
            continue
        seen.add(key)
        if not candidate.exists() or not _is_executable_node(candidate):
            continue

        parent = str(candidate.parent)
        current_path = os.environ.get("PATH", "")
        os.environ["PATH"] = parent + os.pathsep + current_path
        os.environ["XHS_NODE_RESOLVED"] = normalized
        spider_root = Path(__file__).resolve().parent.parent
        node_modules = spider_root / "node_modules"
        if node_modules.exists():
            os.environ["NODE_PATH"] = str(node_modules)
        _CONFIGURED = True
        return normalized

    _CONFIGURED = True
    return None
