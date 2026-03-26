#!/usr/bin/env python3
# encoding: utf-8
"""
XHS MCP Server — 4 tools:
  search_posts, fetch_post_detail, search_comments, fetch_comment_thread
"""

import asyncio
import os
import sys
import json

# 让 Python 能找到 Spider_XHS-master 的模块
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SPIDER = os.path.join(_ROOT, "Spider_XHS-master")
if _SPIDER not in sys.path:
    sys.path.insert(0, _SPIDER)

# 让 Node.js 的 require 能找到 Spider_XHS-master/node_modules
_NODE_MODULES = os.path.join(_SPIDER, "node_modules")
os.environ["NODE_PATH"] = _NODE_MODULES

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from dotenv import load_dotenv
from loguru import logger

from apis.xhs_pc_apis import XHS_Apis
from xhs_utils.data_util import handle_note_info, handle_comment_info

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

XHS_COOKIES = os.getenv("XHS_COOKIES", "")

xhs = XHS_Apis()
server = Server("xhs-spider")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _flat_comment(c: dict, is_sub: bool = False) -> dict:
    """把原始评论对象压平成简洁结构。"""
    return {
        "comment_id": c.get("id", ""),
        "content": c.get("content", ""),
        "like_count": c.get("like_count", 0),
        "create_time": c.get("create_time", 0),
        "ip_location": c.get("ip_location", "未知"),
        "nickname": c.get("user_info", {}).get("nickname", ""),
        "user_id": c.get("user_info", {}).get("user_id", ""),
        "is_sub_comment": is_sub,
    }


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_posts",
            description="搜索小红书笔记，最多返回 10 条。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "require_num": {"type": "integer", "description": "期望数量 1-10", "default": 10},
                    "sort_type": {
                        "type": "integer",
                        "description": "排序方式 0综合 1最新 2最多点赞 3最多评论 4最多收藏",
                        "default": 0,
                    },
                    "note_type": {
                        "type": "integer",
                        "description": "笔记类型 0不限 1视频 2图文",
                        "default": 0,
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="fetch_post_detail",
            description="获取单篇小红书笔记的详细信息。",
            inputSchema={
                "type": "object",
                "properties": {
                    "note_url": {"type": "string", "description": "笔记完整 URL（含 xsec_token）"},
                },
                "required": ["note_url"],
            },
        ),
        types.Tool(
            name="search_comments",
            description="获取一篇笔记的所有评论，最多返回 50 条（含二级评论展开后计算）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "note_url": {"type": "string", "description": "笔记完整 URL（含 xsec_token）"},
                },
                "required": ["note_url"],
            },
        ),
        types.Tool(
            name="fetch_comment_thread",
            description="获取笔记的评论树：一级评论最多 20 条，每条子评论最多 5 条。",
            inputSchema={
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "笔记 ID"},
                    "xsec_token": {"type": "string", "description": "xsec_token（从笔记 URL query 中获取）"},
                },
                "required": ["note_id", "xsec_token"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "search_posts":
            result = await _search_posts(arguments)
        elif name == "fetch_post_detail":
            result = await _fetch_post_detail(arguments)
        elif name == "search_comments":
            result = await _search_comments(arguments)
        elif name == "fetch_comment_thread":
            result = await _fetch_comment_thread(arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    except Exception as e:
        logger.error(f"Tool {name} error: {e}")
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]


async def _search_posts(args: dict) -> dict:
    query = args["query"]
    require_num = _clamp(int(args.get("require_num", 10)), 1, 10)
    sort_type = int(args.get("sort_type", 0))
    note_type = int(args.get("note_type", 0))

    success, msg, raw_list = await asyncio.to_thread(
        xhs.search_some_note, query, require_num, XHS_COOKIES, sort_type, note_type
    )
    if not success:
        raise RuntimeError(f"search_some_note failed: {msg}")

    posts = []
    for item in raw_list:
        if item.get("model_type") != "note":
            continue
        card = item.get("note_card", {})
        interact = card.get("interact_info", {})
        user = card.get("user", {})
        note_id = item.get("id", "")
        xsec_token = item.get("xsec_token", "")
        note_url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={xsec_token}&xsec_source=pc_search"
        desc = card.get("desc", "")
        posts.append({
            "note_id": note_id,
            "title": card.get("title", "") or "无标题",
            "desc": desc[:200] if desc else "",
            "like_count": interact.get("liked_count", 0),
            "comment_count": interact.get("comment_count", 0),
            "share_count": interact.get("share_count", 0),
            "author_nickname": user.get("nickname", ""),
            "note_url": note_url,
            "upload_time": card.get("time", ""),
        })
    return {"success": True, "total": len(posts), "posts": posts}


async def _fetch_post_detail(args: dict) -> dict:
    note_url = args["note_url"]
    success, msg, res_json = await asyncio.to_thread(xhs.get_note_info, note_url, XHS_COOKIES)
    if not success:
        raise RuntimeError(f"get_note_info failed: {msg}")
    data = res_json.get("data", {}).get("items", [{}])[0]
    info = handle_note_info(data)
    # 只保留分析所需字段
    return {
        "success": True,
        "note": {
            "note_id": info["note_id"],
            "title": info["title"],
            "desc": info["desc"],
            "note_type": info["note_type"],
            "tags": info["tags"],
            "like_count": info["liked_count"],
            "comment_count": info["comment_count"],
            "collect_count": info["collected_count"],
            "upload_time": info["upload_time"],
            "ip_location": info["ip_location"],
            "author_nickname": info["nickname"],
        },
    }


async def _search_comments(args: dict) -> dict:
    note_url = args["note_url"]
    success, msg, raw_comments = await asyncio.to_thread(xhs.get_note_all_comment, note_url, XHS_COOKIES)
    if not success:
        raise RuntimeError(f"get_note_all_comment failed: {msg}")

    flat: list[dict] = []
    for c in raw_comments:
        if len(flat) >= 50:
            break
        flat.append(_flat_comment(c, is_sub=False))
        for sub in c.get("sub_comments", []):
            if len(flat) >= 50:
                break
            flat.append(_flat_comment(sub, is_sub=True))

    return {"success": True, "total": len(flat), "comments": flat}


async def _fetch_comment_thread(args: dict) -> dict:
    note_id = args["note_id"]
    xsec_token = args["xsec_token"]

    success, msg, out_comments = await asyncio.to_thread(
        xhs.get_note_all_out_comment, note_id, xsec_token, XHS_COOKIES
    )
    if not success:
        raise RuntimeError(f"get_note_all_out_comment failed: {msg}")

    top_comments = out_comments[:20]
    threads = []
    for c in top_comments:
        entry = _flat_comment(c, is_sub=False)
        sub_list = c.get("sub_comments", [])[:5]
        entry["sub_comments"] = [_flat_comment(s, is_sub=True) for s in sub_list]
        threads.append(entry)

    return {"success": True, "total": len(threads), "threads": threads}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
