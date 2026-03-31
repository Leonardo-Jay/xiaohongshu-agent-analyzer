"""MCP Client — 通过 stdio transport 连接到 xhs_mcp_server.py。

使用方式（单个客户端）:
    async with XhsMcpClient() as client:
        posts = await client.search_posts(query="产品名称")

使用方式（连接池，推荐用于并发场景）:
    async with XhsMcpClientPool(size=3) as pool:
        async with pool.borrow() as client:
            comments = await client.search_comments(note_url)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import traceback
from contextlib import asynccontextmanager
from typing import Any

from loguru import logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MCP_SERVER = os.path.join(_BACKEND_ROOT, "mcp_server", "xhs_mcp_server.py")
_SPIDER = os.path.join(os.path.dirname(_BACKEND_ROOT), "Spider_XHS-master")
_PYTHON = sys.executable

# 预检缓存：只执行一次，避免多个 client 并发时重复启动 subprocess
_preflight_done: bool = False
_preflight_lock: asyncio.Lock | None = None


def _get_preflight_lock() -> asyncio.Lock:
    global _preflight_lock
    if _preflight_lock is None:
        _preflight_lock = asyncio.Lock()
    return _preflight_lock


def _preflight_check() -> None:
    """在启动 stdio 连接前，用同步子进程验证 MCP server 能否正常导入。
    若失败，抛出包含 stderr 的 RuntimeError。
    """
    check_code = (
        f"import sys; "
        f"sys.path.insert(0, {_SPIDER!r}); "
        f"from apis.xhs_pc_apis import XHS_Apis; "
        f"from xhs_utils.xhs_util import generate_xs_xs_common; "
        f"print('preflight_ok')"
    )
    result = subprocess.run(
        [_PYTHON, "-c", check_code],
        capture_output=True,
        text=True,
        timeout=20,
        env=dict(os.environ),
    )
    if result.returncode != 0 or "preflight_ok" not in result.stdout:
        stderr = result.stderr.strip() or result.stdout.strip() or "(无输出)"
        raise RuntimeError(
            f"MCP server 依赖检查失败（exit={result.returncode}）:\n{stderr}"
        )
    logger.debug("MCP server 依赖预检通过")


async def _ensure_preflight() -> None:
    """确保预检只执行一次，并发安全。"""
    global _preflight_done
    if _preflight_done:
        return
    async with _get_preflight_lock():
        if _preflight_done:
            return
        try:
            await asyncio.to_thread(_preflight_check)
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"MCP server 预检异常: {e}\n{traceback.format_exc()}")
        _preflight_done = True


class XhsMcpClient:
    def __init__(self, cookie: str | None = None):
        self._session: ClientSession | None = None
        self._cm = None
        self._cookie = cookie

    async def __aenter__(self) -> "XhsMcpClient":
        # 预检：只执行一次（并发安全）
        await _ensure_preflight()

        env = dict(os.environ)
        if self._cookie:
            env["XHS_COOKIES"] = self._cookie
        params = StdioServerParameters(
            command=_PYTHON,
            args=[_MCP_SERVER],
            env=env,
        )
        try:
            self._cm = stdio_client(params)
            read, write = await self._cm.__aenter__()
            self._session = ClientSession(read, write)
            await self._session.__aenter__()
            await self._session.initialize()
        except Exception as e:
            tb = traceback.format_exc()
            raise RuntimeError(f"MCP stdio 连接失败: {e}\n{tb}")

        logger.debug("XhsMcpClient connected")
        return self

    async def __aexit__(self, *exc):
        if self._session:
            try:
                await self._session.__aexit__(*exc)
            except Exception:
                pass
        if self._cm:
            try:
                await self._cm.__aexit__(*exc)
            except Exception:
                pass

    async def _call(self, tool: str, args: dict[str, Any]) -> Any:
        assert self._session, "Client not connected"
        try:
            result = await self._session.call_tool(tool, args)
        except Exception as e:
            raise RuntimeError(f"MCP tool '{tool}' 调用失败: {e}\n{traceback.format_exc()}")
        raw = result.content[0].text
        data = json.loads(raw)
        if "error" in data:
            raise RuntimeError(f"MCP tool '{tool}' 返回错误: {data['error']}")
        return data

    async def search_posts(
        self,
        query: str,
        require_num: int = 10,
        sort_type: int = 0,
        note_type: int = 0,
    ) -> list[dict[str, Any]]:
        data = await self._call(
            "search_posts",
            {"query": query, "require_num": require_num, "sort_type": sort_type, "note_type": note_type},
        )
        return data.get("posts", [])

    async def fetch_post_detail(self, note_url: str) -> dict[str, Any]:
        data = await self._call("fetch_post_detail", {"note_url": note_url})
        return data.get("note", {})

    async def search_comments(self, note_url: str) -> list[dict[str, Any]]:
        data = await self._call("search_comments", {"note_url": note_url})
        return data.get("comments", [])

    async def fetch_comment_thread(
        self, note_id: str, xsec_token: str
    ) -> list[dict[str, Any]]:
        data = await self._call(
            "fetch_comment_thread", {"note_id": note_id, "xsec_token": xsec_token}
        )
        return data.get("threads", [])


class XhsMcpClientPool:
    """固定大小的 MCP 客户端连接池，用于并发拉取评论时复用连接。

    用法:
        async with XhsMcpClientPool(size=3) as pool:
            async with pool.borrow() as client:
                comments = await client.search_comments(note_url)
    """

    def __init__(self, size: int = 3, cookie: str | None = None):
        self._size = size
        self._cookie = cookie
        self._clients: list[XhsMcpClient] = []
        self._queue: asyncio.Queue[XhsMcpClient] = asyncio.Queue()

    async def __aenter__(self) -> "XhsMcpClientPool":
        # 并发启动所有 client（预检只执行一次）
        instances = [XhsMcpClient(cookie=self._cookie) for _ in range(self._size)]
        connected = await asyncio.gather(*[c.__aenter__() for c in instances])
        self._clients = list(connected)
        for c in self._clients:
            self._queue.put_nowait(c)
        logger.info(f"[Pool] {self._size} 个 MCP 客户端连接池已就绪")
        return self

    async def __aexit__(self, *exc) -> None:
        for c in self._clients:
            try:
                await c.__aexit__(None, None, None)
            except Exception:
                pass
        logger.debug("[Pool] MCP 客户端连接池已关闭")

    @asynccontextmanager
    async def borrow(self):
        """从池中借用一个客户端，用完自动归还。连接断开时自动重建。"""
        client = await self._queue.get()
        try:
            yield client
            self._queue.put_nowait(client)  # 正常归还
        except Exception:
            # 连接可能已断，尝试重建后再归还
            logger.warning("[Pool] client 异常，尝试重建连接")
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass
            try:
                new_client = await XhsMcpClient(cookie=self._cookie).__aenter__()
                self._clients = [c for c in self._clients if c is not client] + [new_client]
                self._queue.put_nowait(new_client)
                logger.info("[Pool] client 重建成功")
            except Exception as rebuild_err:
                logger.error(f"[Pool] 重建 client 失败: {rebuild_err}")
            raise  # 把原始异常抛出，让调用方的重试逻辑处理
