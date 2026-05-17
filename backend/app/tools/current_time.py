"""Current time tool client.

Uses the ApiHz time endpoint when configured and falls back to local
Asia/Shanghai time so analysis never blocks on an external clock.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import os
from typing import Any

import httpx
from loguru import logger


def _shanghai_tz():
    try:
        return ZoneInfo("Asia/Shanghai")
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


class CurrentTimeClient:
    API_URL = "https://cn.apihz.cn/api/time/getapi.php"
    TIMEZONE = "Asia/Shanghai"

    def __init__(self, api_id: str | None = None, api_key: str | None = None) -> None:
        self._api_id = api_id or os.getenv("APIHZ_ID", "")
        self._api_key = api_key or os.getenv("APIHZ_KEY", "")

    async def get_current_time(self) -> dict[str, Any]:
        if self._api_id and self._api_key:
            data = await self._fetch_api_time()
            if data:
                return data
        return self._local_fallback("current time api unavailable")

    async def _fetch_api_time(self) -> dict[str, Any] | None:
        params = {"id": self._api_id, "key": self._api_key, "type": 20}
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                resp = await client.get(self.API_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning(f"[CurrentTime] ApiHz request failed: {exc}")
            return None

        if data.get("code") != 200:
            logger.warning(f"[CurrentTime] ApiHz returned error: {data.get('msg', 'unknown')}")
            return None

        try:
            dt = datetime(
                int(data["y"]),
                int(data["m"]),
                int(data["d"]),
                int(data["h"]),
                int(data["i"]),
                int(data["s"]),
                tzinfo=_shanghai_tz(),
            )
        except Exception as exc:
            logger.warning(f"[CurrentTime] ApiHz payload parse failed: {exc}")
            return None

        return {
            "source": "apihz",
            "now_iso": dt.isoformat(timespec="seconds"),
            "date": dt.date().isoformat(),
            "timestamp": int(data.get("sjc") or dt.timestamp()),
            "timezone": self.TIMEZONE,
        }

    def _local_fallback(self, warning: str) -> dict[str, Any]:
        dt = datetime.now(_shanghai_tz())
        return {
            "source": "local_fallback",
            "now_iso": dt.isoformat(timespec="seconds"),
            "date": dt.date().isoformat(),
            "timestamp": 0,
            "timezone": self.TIMEZONE,
            "warning": warning,
        }


current_time_client = CurrentTimeClient()
