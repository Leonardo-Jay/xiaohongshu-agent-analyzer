import asyncio
import httpx
import json
from pathlib import Path
from mcp.server import Server
from mcp.types import Tool, TextContent
from loguru import logger

from config import SkillConfig
from backend_manager import BackendManager

app = Server("xhs-analysis")
config = SkillConfig()
backend_manager = BackendManager(config.get_backend_url())


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="analyze_xhs_sentiment",
            description="分析小红书舆情，返回完整 Markdown 报告。首次使用时需配置 Cookie。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要分析的关键词（如 'iPhone16'、'小米汽车'）"
                    },
                    "cookie": {
                        "type": "string",
                        "description": "可选：小红书 Cookie。首次使用时必填，后续可省略（自动从加密文件读取）"
                    },
                    "enable_memory": {
                        "type": "boolean",
                        "description": "可选：启用记忆复用以加速分析（默认 false）"
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="configure_cookie",
            description="配置或更新小红书 Cookie，加密保存到本地。",
            inputSchema={
                "type": "object",
                "properties": {
                    "cookie": {
                        "type": "string",
                        "description": "小红书 Cookie 字符串（从浏览器开发者工具复制）"
                    }
                },
                "required": ["cookie"]
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "analyze_xhs_sentiment":
        return await _analyze_sentiment(arguments)
    elif name == "configure_cookie":
        return await _configure_cookie(arguments)
    else:
        raise ValueError(f"Unknown tool: {name}")


async def _configure_cookie(args: dict) -> list[TextContent]:
    """配置 Cookie"""
    cookie = args["cookie"].strip()

    if not cookie:
        return [TextContent(
            type="text",
            text="Error: Cookie 不能为空"
        )]

    config.save_cookie(cookie)

    return [TextContent(
        type="text",
        text="Cookie 配置成功。已加密保存到本地，后续分析将自动使用此 Cookie。"
    )]


async def _analyze_sentiment(args: dict) -> list[TextContent]:
    """核心分析逻辑：启动后端、调用 API、SSE 流式获取报告"""
    query = args.get("query", "").strip()
    cookie = args.get("cookie") or config.get_cookie()
    enable_memory = args.get("enable_memory", False)

    if not query:
        return [TextContent(
            type="text",
            text="Error: query 不能为空"
        )]

    # 1. Cookie 检查
    if not cookie:
        return [TextContent(type="text", text=_get_cookie_setup_guide())]

    # 2. 确保后端运行
    if not await backend_manager.ensure_running():
        return [TextContent(
            type="text",
            text="Error: 后端服务启动失败。请检查 skill-package/logs/ 目录下的日志文件。"
        )]

    # 3. 启动分析任务
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        try:
            response = await client.post(
                f"{config.get_backend_url()}/api/v1/analysis/product",
                json={
                    "query": query,
                    "cookie": cookie,
                    "enable_memory": enable_memory
                }
            )
            response.raise_for_status()
            run_id = response.json()["run_id"]

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                return [TextContent(type="text", text=_get_cookie_expired_guide())]
            else:
                return [TextContent(
                    type="text",
                    text=f"Error: 启动分析失败\n\n{e.response.text}"
                )]
        except Exception as e:
            return [TextContent(
                type="text",
                text=f"Error: 连接后端失败\n\n{str(e)}"
            )]

    # 4. SSE 流式获取结果
    try:
        report = await _consume_sse_stream(config.get_backend_url(), run_id)
        return [TextContent(
            type="text",
            text=report
        )]
    except TimeoutError:
        timeout = config.get_timeout()
        return [TextContent(
            type="text",
            text=f"Analysis timeout ({timeout}s)\n\nQuery '{query}' is too complex. Try:\n1. Narrow the scope (e.g., 'iPhone16 battery' instead of 'phone')\n2. Enable memory reuse\n3. Retry later"
        )]
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"Error: SSE stream failed\n\n{str(e)}"
        )]


async def _consume_sse_stream(backend_url: str, run_id: str) -> str:
    """连接到 SSE 流，收集报告内容。"""
    timeout = config.get_timeout()
    report_parts = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=timeout)) as client:
        async with client.stream(
            "GET",
            f"{backend_url}/api/v1/analysis/stream/{run_id}",
            headers={"Accept": "text/event-stream"}
        ) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                if not line:
                    continue

                # SSE 格式: event: xxx
                if line.startswith("event:"):
                    event_type = line[len("event:"):].strip()
                    continue

                # SSE 格式: data: xxx
                if line.startswith("data:"):
                    data_str = line[len("data:"):].strip()
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if event_type == "result":
                        # 最终报告
                        report = data.get("final_answer", "")
                        if report:
                            report_parts.append(report)

                    elif event_type == "error":
                        raise RuntimeError(data.get("message", "Unknown error"))

                    elif event_type == "done":
                        break

                    elif event_type == "progress":
                        # 可以记录进度但当前不需要
                        pass

            if not report_parts:
                raise RuntimeError("No report content received from SSE stream")

    return "\n".join(report_parts)


def _get_cookie_setup_guide() -> str:
    """首次配置 Cookie 的引导"""
    return """First-time setup: XHS Cookie required.

## Get your XHS Cookie:

1. Open https://www.xiaohongshu.com in browser and login
2. Press F12 to open Developer Tools
3. Go to the Network tab
4. Refresh the page, find any request
5. Copy the full `Cookie` value from request headers

## Configure Cookie:

Use the configure_cookie tool, passing your Cookie as the cookie argument.

Alternatively, set the XHS_COOKIE environment variable for permanent use.

Once configured, the Cookie is encrypted and saved locally; no need to repeat.
"""


def _get_cookie_expired_guide() -> str:
    """Cookie 过期的引导"""
    return """XHS Cookie has expired.

Please reconfigure using the configure_cookie tool with a fresh Cookie.

Get Cookie: Login at https://www.xiaohongshu.com, F12 > Network > copy Cookie.
"""


# 启动 MCP Server
async def main():
    from mcp.server.stdio import stdio_server

    # 初始化日志
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "skill_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="7 days"
    )

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
