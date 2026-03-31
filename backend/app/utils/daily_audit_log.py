from __future__ import annotations

import json
import os
from datetime import datetime
from threading import Lock
from typing import Any

_WRITE_LOCK = Lock()


def _backend_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_daily_log_path() -> str:
    logs_dir = os.path.join(_backend_root(), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(logs_dir, f"access-{date_str}.log")


def append_audit_log(event: str, **fields: Any) -> None:
    payload = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        **fields,
    }
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with _WRITE_LOCK:
        with open(get_daily_log_path(), "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
