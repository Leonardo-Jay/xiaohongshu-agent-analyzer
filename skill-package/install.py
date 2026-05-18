#!/usr/bin/env python3
"""Register the XHS Analysis MCP Skill in Claude Desktop or Cursor."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def get_claude_desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform == "win32":
        return Path.home() / "AppData/Roaming/Claude/claude_desktop_config.json"
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def get_cursor_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / ".cursor/mcp.json"
    if sys.platform == "win32":
        return Path.home() / "AppData/Roaming/Cursor/User/globalStorage/mcp.json"
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def get_runtime_python(skill_path: Path) -> str:
    project_root = skill_path.parent
    candidates = []
    if sys.platform == "win32":
        candidates.append(project_root / "backend" / ".venv" / "Scripts" / "python.exe")
    else:
        candidates.append(project_root / "backend" / ".venv" / "bin" / "python")
    candidates.append(Path(sys.executable))

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def install_server(config_path: Path, skill_path: Path, runtime_python: str) -> Path:
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        config = {}

    config.setdefault("mcpServers", {})
    config["mcpServers"]["xhs-analysis"] = {
        "command": runtime_python,
        "args": [str(skill_path / "skill_server.py")],
        "cwd": str(skill_path),
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return config_path


def main() -> None:
    print("=" * 68)
    print("XHS Analysis Skill - 安装/更新 MCP 配置")
    print("=" * 68)
    print()

    skill_path = Path(__file__).parent.resolve()
    project_root = skill_path.parent
    runtime_python = get_runtime_python(skill_path)

    if not (skill_path / "skill_server.py").exists():
        print("错误：未找到 skill_server.py，请在 skill-package 目录运行安装脚本。")
        sys.exit(1)

    print(f"Skill 路径: {skill_path}")
    print(f"项目路径: {project_root}")
    print(f"MCP Python: {runtime_python}")
    print()

    print("请选择安装目标：")
    print("  [1] Claude Desktop（推荐）")
    print("  [2] Cursor")
    print("  [3] 两者都安装")
    print()

    try:
        choice = input("请输入选择 [1/2/3，默认 1]: ").strip() or "1"
    except EOFError:
        choice = "1"

    installed: list[tuple[str, Path]] = []
    try:
        if choice in {"1", "3"}:
            installed.append(
                (
                    "Claude Desktop",
                    install_server(get_claude_desktop_config_path(), skill_path, runtime_python),
                )
            )
        if choice in {"2", "3"}:
            installed.append(
                (
                    "Cursor",
                    install_server(get_cursor_config_path(), skill_path, runtime_python),
                )
            )
    except Exception as exc:
        print(f"\n安装失败: {exc}")
        sys.exit(1)

    print()
    print("=" * 68)
    print("安装成功")
    print("=" * 68)
    for name, path in installed:
        print(f"- {name}: {path}")

    print()
    print("运行前请确认：")
    print(r"1. 后端依赖已安装：cd backend && .\.venv\Scripts\python.exe -m pip install -r requirements.txt")
    print(r"2. 小红书 JS 依赖已安装：cd Spider_XHS-master && npm install")
    print("3. backend/.env 已配置 LLM API，或环境变量中已配置。")
    print("4. 重启 Claude Desktop / Cursor 后，先调用 check_xhs_runtime。")
    print()
    print("说明：默认 Skill 会直接运行本地多 Agent 工作流，不会启动 Vue 前端或 FastAPI 后端。")


if __name__ == "__main__":
    main()
