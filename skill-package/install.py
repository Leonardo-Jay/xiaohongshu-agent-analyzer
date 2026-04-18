#!/usr/bin/env python3
"""
自动安装脚本：将 XHS Analysis Skill 注册到 Claude Desktop / Cursor

使用方式：
    python install.py

功能：
    - 自动检测操作系统（macOS/Windows）
    - 自动定位 Claude Desktop 配置文件
    - 自动填写正确的路径
    - 支持增量配置（不覆盖现有配置）
"""
import sys
import json
from pathlib import Path


def get_claude_desktop_config_path() -> Path:
    """获取 Claude Desktop 配置文件路径"""
    if sys.platform == "darwin":  # macOS
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    elif sys.platform == "win32":  # Windows
        return Path.home() / "AppData/Roaming/Claude/claude_desktop_config.json"
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def get_cursor_config_path() -> Path:
    """获取 Cursor 配置文件路径"""
    if sys.platform == "darwin":  # macOS
        return Path.home() / ".cursor/mcp.json"
    elif sys.platform == "win32":  # Windows
        return Path.home() / "AppData/Roaming/Cursor/User/globalStorage/mcp.json"
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def install_to_claude_desktop(skill_path: Path):
    """安装到 Claude Desktop"""
    config_path = get_claude_desktop_config_path()

    # 读取现有配置
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding='utf-8'))
    else:
        config = {}

    # 添加 skill 配置
    config.setdefault("mcpServers", {})
    config["mcpServers"]["xhs-analysis"] = {
        "command": sys.executable,
        "args": [str(skill_path / "skill_server.py")],
        "cwd": str(skill_path)
    }

    # 保存配置
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding='utf-8')

    return config_path


def install_to_cursor(skill_path: Path):
    """安装到 Cursor IDE"""
    config_path = get_cursor_config_path()

    # 读取现有配置
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding='utf-8'))
    else:
        config = {}

    # 添加 skill 配置
    config.setdefault("mcpServers", {})
    config["mcpServers"]["xhs-analysis"] = {
        "command": sys.executable,
        "args": [str(skill_path / "skill_server.py")],
        "cwd": str(skill_path)
    }

    # 保存配置
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding='utf-8')

    return config_path


def main():
    print("=" * 60)
    print("XHS Analysis Skill - 自动安装")
    print("=" * 60)
    print()

    # 获取 skill-package 路径
    skill_path = Path(__file__).parent.resolve()

    if not (skill_path / "skill_server.py").exists():
        print("❌ 错误：未找到 skill_server.py")
        print(f"   请确保在 skill-package 目录下运行此脚本")
        sys.exit(1)

    print(f"✅ Skill 路径: {skill_path}")
    print()

    # 选择安装目标
    print("请选择安装目标：")
    print("  [1] Claude Desktop（推荐）")
    print("  [2] Cursor IDE")
    print("  [3] 两者都安装")
    print()

    try:
        choice = input("请输入选择 [1/2/3，默认 1]: ").strip() or "1"
    except EOFError:
        choice = "1"

    installed = []

    try:
        if choice in ["1", "3"]:
            config_path = install_to_claude_desktop(skill_path)
            installed.append(("Claude Desktop", config_path))

        if choice in ["2", "3"]:
            config_path = install_to_cursor(skill_path)
            installed.append(("Cursor IDE", config_path))

    except Exception as e:
        print(f"\n❌ 安装失败: {e}")
        print("\n请尝试手动配置，参考 README.md 中的说明")
        sys.exit(1)

    # 输出成功信息
    print()
    print("=" * 60)
    print("✅ 安装成功！")
    print("=" * 60)
    print()

    for name, path in installed:
        print(f"已安装到 {name}:")
        print(f"  配置文件: {path}")
        print()

    print("下一步：")
    print("  1. 重启 Claude Desktop / Cursor")
    print("  2. 输入查询：'分析 iPhone 16 的用户口碑'")
    print("  3. 首次使用时，Claude 会提示你配置 Cookie")
    print()
    print("获取 Cookie:")
    print("  1. 打开 https://www.xiaohongshu.com 并登录")
    print("  2. 按 F12 → Network → 刷新页面")
    print("  3. 复制任意请求的 Cookie 值")
    print()
    print("=" * 60)


if __name__ == "__main__":
    main()
