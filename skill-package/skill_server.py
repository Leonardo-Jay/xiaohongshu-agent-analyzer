import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from mcp.server import Server
from mcp.types import TextContent, Tool

from config import SkillConfig

app = Server("xhs-analysis")
config = SkillConfig()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
SPIDER_DIR = PROJECT_ROOT / "Spider_XHS-master"

for path in (BACKEND_DIR, SPIDER_DIR):
    path_str = str(path)
    if path.exists() and path_str not in sys.path:
        sys.path.insert(0, path_str)

try:
    from dotenv import load_dotenv

    load_dotenv(BACKEND_DIR / ".env")
except Exception:
    pass

try:
    from app.services.skill_analysis_runner import SkillAnalysisError, run_skill_analysis

    _LOCAL_IMPORT_ERROR: str | None = None
except Exception as exc:
    SkillAnalysisError = None  # type: ignore[assignment]
    run_skill_analysis = None  # type: ignore[assignment]
    _LOCAL_IMPORT_ERROR = repr(exc)


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="analyze_xhs_sentiment",
            description=(
                "分析小红书舆情、产品口碑或热点问题。默认直接运行本地多 Agent 工作流，"
                "不需要启动 Vue 前端或 FastAPI 后端服务。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要分析的问题，例如：iPhone 16 口碑怎么样",
                    },
                    "cookie": {
                        "type": "string",
                        "description": "可选：小红书 Cookie。省略时读取加密本地配置或环境变量。",
                    },
                    "enable_memory": {
                        "type": "boolean",
                        "description": "可选：是否启用 Wiki Memory 记忆复用。",
                    },
                    "return_report_ir": {
                        "type": "boolean",
                        "description": "可选：是否在 Markdown 后附加 Report IR JSON。",
                    },
                    "save_artifacts": {
                        "type": "boolean",
                        "description": "可选：是否把 Markdown 和结构化结果保存到本地文件。",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="configure_cookie",
            description="配置或更新小红书 Cookie，并加密保存到本地。",
            inputSchema={
                "type": "object",
                "properties": {
                    "cookie": {
                        "type": "string",
                        "description": "从浏览器开发者工具复制的小红书 Cookie 字符串",
                    }
                },
                "required": ["cookie"],
            },
        ),
        Tool(
            name="check_xhs_runtime",
            description="检查本地 Skill 运行环境，包括 Python 依赖、LLM 配置、Cookie、Node 和小红书 JS 依赖。",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "analyze_xhs_sentiment":
        return await _analyze_sentiment(arguments)
    if name == "configure_cookie":
        return await _configure_cookie(arguments)
    if name == "check_xhs_runtime":
        return await _check_runtime()
    raise ValueError(f"Unknown tool: {name}")


async def _configure_cookie(args: dict) -> list[TextContent]:
    cookie = (args.get("cookie") or "").strip()
    if not cookie:
        return [TextContent(type="text", text="Error: Cookie 不能为空")]

    config.save_cookie(cookie)
    return [
        TextContent(
            type="text",
            text="Cookie 配置成功。后续分析会优先读取环境变量，其次读取本地加密 Cookie。",
        )
    ]


async def _analyze_sentiment(args: dict) -> list[TextContent]:
    query = (args.get("query") or "").strip()
    cookie = (args.get("cookie") or config.get_cookie() or "").strip()
    enable_memory = _as_bool(args.get("enable_memory"), False)
    return_report_ir = _as_bool(args.get("return_report_ir"), False)
    save_artifacts = _as_bool(args.get("save_artifacts"), False)

    if not query:
        return [TextContent(type="text", text="Error: query 不能为空")]

    if not cookie:
        return [TextContent(type="text", text=_get_cookie_setup_guide())]

    if config.get_skill_mode() == "remote":
        return await _analyze_sentiment_remote(
            query=query,
            cookie=cookie,
            enable_memory=enable_memory,
        )

    if _LOCAL_IMPORT_ERROR or run_skill_analysis is None:
        return [
            TextContent(
                type="text",
                text=(
                    "Error: 本地多 Agent 工作流无法导入。\n\n"
                    f"{_LOCAL_IMPORT_ERROR}\n\n"
                    "请先运行 check_xhs_runtime，并确认 backend/.venv 已安装 backend/requirements.txt。"
                ),
            )
        ]

    try:
        result = await run_skill_analysis(
            query=query,
            cookie=cookie,
            enable_memory=enable_memory,
            timeout=config.get_timeout(),
        )
        text = _format_analysis_result(
            result,
            return_report_ir=return_report_ir,
            save_artifacts=save_artifacts,
        )
        return [TextContent(type="text", text=text)]
    except Exception as exc:
        if SkillAnalysisError is not None and isinstance(exc, SkillAnalysisError):
            if exc.code == "COOKIE_EXPIRED":
                return [TextContent(type="text", text=_get_cookie_expired_guide())]
            return [TextContent(type="text", text=_format_skill_error(exc.code, exc.message))]
        return [
            TextContent(
                type="text",
                text=(
                    "Error: 本地 Skill 分析失败\n\n"
                    f"{exc}\n\n"
                    "建议先调用 check_xhs_runtime 查看依赖、Cookie、Node 和 LLM 配置。"
                ),
            )
        ]


async def _analyze_sentiment_remote(
    *,
    query: str,
    cookie: str,
    enable_memory: bool,
) -> list[TextContent]:
    backend_url = config.get_backend_url()
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        try:
            response = await client.post(
                f"{backend_url}/api/v1/analysis/product",
                json={"query": query, "cookie": cookie, "enable_memory": enable_memory},
            )
            response.raise_for_status()
            run_id = response.json()["run_id"]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                return [TextContent(type="text", text=_get_cookie_expired_guide())]
            return [
                TextContent(
                    type="text",
                    text=f"Error: 远程后端启动分析失败\n\n{exc.response.text}",
                )
            ]
        except Exception as exc:
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Error: 无法连接远程后端 {backend_url}\n\n{exc}\n\n"
                        "默认本地模式不需要后端服务；只有设置 XHS_SKILL_MODE=remote 时才会走这个路径。"
                    ),
                )
            ]

    try:
        report = await _consume_sse_stream(backend_url, run_id)
        return [TextContent(type="text", text=report)]
    except TimeoutError:
        timeout = config.get_timeout()
        return [
            TextContent(
                type="text",
                text=(
                    f"Analysis timeout ({timeout}s)\n\n"
                    "Try narrowing the query, enabling memory reuse, or retrying later."
                ),
            )
        ]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error: SSE stream failed\n\n{exc}")]


async def _consume_sse_stream(backend_url: str, run_id: str) -> str:
    timeout = config.get_timeout()
    report_parts: list[str] = []
    event_type = ""

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=timeout)) as client:
        async with client.stream(
            "GET",
            f"{backend_url}/api/v1/analysis/stream/{run_id}",
            headers={"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    event_type = line[len("event:") :].strip()
                    continue
                if not line.startswith("data:"):
                    continue

                try:
                    data = json.loads(line[len("data:") :].strip())
                except json.JSONDecodeError:
                    continue

                if event_type == "result":
                    report = data.get("final_answer", "")
                    if report:
                        report_parts.append(report)
                elif event_type == "error":
                    raise RuntimeError(data.get("message", "Unknown error"))
                elif event_type == "done":
                    break

    if not report_parts:
        raise RuntimeError("No report content received from SSE stream")
    return "\n".join(report_parts)


async def _check_runtime() -> list[TextContent]:
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append((name, ok, detail))

    add("Project root", PROJECT_ROOT.exists(), str(PROJECT_ROOT))
    add("Backend directory", BACKEND_DIR.exists(), str(BACKEND_DIR))
    add("Spider directory", SPIDER_DIR.exists(), str(SPIDER_DIR))
    add("Local runner import", _LOCAL_IMPORT_ERROR is None, _LOCAL_IMPORT_ERROR or "ok")

    provider = os.getenv("LLM_PROVIDER", "qianfan").strip().lower()
    required_env = {
        "longcat": "LONGCAT_API_KEY",
        "modelscope": "MODELSCOPE_API_KEY",
        "qianfan": "QIANFAN_BEARER_TOKEN",
    }.get(provider, "QIANFAN_BEARER_TOKEN")
    add(
        "LLM configuration",
        bool(os.getenv(required_env)),
        f"provider={provider}, required={required_env}",
    )
    add("XHS Cookie", config.has_cookie(), "configured" if config.has_cookie() else "not configured")

    node_path = _resolve_node_runtime()
    node_ok = bool(node_path) and not str(node_path).startswith("error:")
    add("Node runtime", node_ok, node_path or "not found")

    node_deps_ok, node_deps_detail = _check_node_dependencies(node_path)
    add("Spider Node dependencies", node_deps_ok, node_deps_detail)

    preflight_ok, preflight_detail = _run_mcp_preflight()
    add("MCP preflight", preflight_ok, preflight_detail)

    lines = ["# XHS Skill Runtime Check", ""]
    for name, ok, detail in checks:
        mark = "OK" if ok else "FAIL"
        lines.append(f"- **{name}**: {mark} - {detail}")

    if not all(ok for _, ok, _ in checks):
        lines.extend(
            [
                "",
                "## Suggested fixes",
                "",
                "- Install backend dependencies: `cd backend && .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt`",
                "- Install Spider Node dependencies: `cd Spider_XHS-master && npm install`",
                "- Configure LLM environment variables in `backend/.env`.",
                "- Configure Cookie with `configure_cookie`, or set `XHS_COOKIES=-1` for mock mode.",
            ]
        )

    return [TextContent(type="text", text="\n".join(lines))]


def _resolve_node_runtime() -> str | None:
    if not SPIDER_DIR.exists():
        return None

    try:
        from xhs_utils.js_runtime_util import ensure_node_runtime

        return ensure_node_runtime()
    except Exception as exc:
        return f"error: {exc}"


def _check_node_dependencies(node_path: str | None) -> tuple[bool, str]:
    if not node_path or node_path.startswith("error:"):
        return False, "Node runtime is unavailable"

    try:
        result = subprocess.run(
            [
                node_path,
                "-e",
                "require.resolve('crypto-js'); require.resolve('jsdom');",
            ],
            cwd=SPIDER_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return False, str(exc)

    if result.returncode == 0:
        return True, "crypto-js and jsdom are available"
    return False, (result.stderr or result.stdout or "missing crypto-js/jsdom").strip()


def _run_mcp_preflight() -> tuple[bool, str]:
    try:
        from app.tools.mcp_client import _preflight_check

        _preflight_check()
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def _format_analysis_result(
    result: dict[str, Any],
    *,
    return_report_ir: bool,
    save_artifacts: bool,
) -> str:
    report = (result.get("final_answer") or "").strip()
    if not report:
        report = "Analysis finished, but no Markdown report was returned."

    extra: list[str] = []
    if save_artifacts:
        paths = _save_artifacts(result)
        extra.append("## Saved artifacts\n\n" + "\n".join(f"- `{path}`" for path in paths))

    if return_report_ir:
        report_ir = result.get("report_ir") or {}
        extra.append(
            "## Report IR\n\n"
            "```json\n"
            + json.dumps(report_ir, ensure_ascii=False, indent=2, default=str)
            + "\n```"
        )

    if extra:
        report += "\n\n---\n\n" + "\n\n".join(extra)
    return report


def _save_artifacts(result: dict[str, Any]) -> list[Path]:
    artifact_dir = config.get_artifact_dir()
    run_id = result.get("run_id") or "analysis"
    md_path = artifact_dir / f"{run_id}.md"
    json_path = artifact_dir / f"{run_id}.json"

    md_path.write_text(result.get("final_answer", ""), encoding="utf-8")
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return [md_path, json_path]


def _format_skill_error(code: str, message: str) -> str:
    return (
        f"Error: {code}\n\n"
        f"{message}\n\n"
        "建议先调用 `check_xhs_runtime`，重点检查 Cookie、Node 依赖、LLM 环境变量和 MCP 预检。"
    )


def _get_cookie_setup_guide() -> str:
    return """First-time setup: XHS Cookie required.

## Get your XHS Cookie

1. Open https://www.xiaohongshu.com in a browser and log in.
2. Press F12 and open the Network tab.
3. Refresh the page and select any request.
4. Copy the full `Cookie` request header.

## Configure Cookie

Use the `configure_cookie` tool with your Cookie string.

For local smoke tests, you can set `XHS_COOKIES=-1` to use mock data.
"""


def _get_cookie_expired_guide() -> str:
    return """XHS Cookie has expired.

Please use `configure_cookie` with a fresh Cookie.

Get Cookie: log in at https://www.xiaohongshu.com, then copy the request Cookie from F12 > Network.
"""


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


async def main() -> None:
    from mcp.server.stdio import stdio_server

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(log_dir / "skill_{time:YYYY-MM-DD}.log", rotation="1 day", retention="7 days")

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
