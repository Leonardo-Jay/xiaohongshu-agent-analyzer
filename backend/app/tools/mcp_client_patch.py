"""MCP stdio_client Monkey Patch：捕获进程对象"""
import os
import asyncio
from typing import AsyncIterator
from contextlib import asynccontextmanager
from loguru import logger

# 在导入 MCP 之前就应用补丁
import psutil


# 全局进程注册表：记录每个进程创建的 MCP 子进程
_process_registry: dict[int, dict] = {}  # {parent_pid: {"mcp_pids": set[int], "processes": list}}

# 保存原始 stdio_client 的引用（在应用补丁时设置）
_original_stdio_client = None


def get_process_registry() -> dict:
    """获取当前进程的注册表"""
    pid = os.getpid()
    if pid not in _process_registry:
        _process_registry[pid] = {
            "mcp_pids": set(),  # MCP 子进程 PID
            "processes": [],    # psutil.Process 对象
        }
    return _process_registry[pid]


@asynccontextmanager
async def stdio_client_with_pid_tracking(
    server,  # StdioServerParameters
) -> AsyncIterator:
    """
    包装 stdio_client，记录创建的子进程 PID

    这个函数会在导入 MCP 后被 Monkey Patch 应用
    """
    global _original_stdio_client

    if _original_stdio_client is None:
        raise RuntimeError("[MCPPatch] 原始 stdio_client 未保存，请先调用 apply_patch()")

    # 获取当前进程的注册表
    registry = get_process_registry()

    # 记录创建前的子进程
    try:
        parent = psutil.Process(os.getpid())
        children_before = {p.pid for p in parent.children(recursive=True)}
        logger.debug(f"[MCPPatch] 创建 MCP 连接前，已有子进程: {children_before}")
    except Exception as e:
        logger.warning(f"[MCPPatch] 无法获取子进程列表: {e}")
        children_before = set()

    # 调用原始 stdio_client（保存的引用，不会递归）
    async with _original_stdio_client(server) as (read_stream, write_stream):
        # 找到新创建的子进程
        try:
            children_after = {p.pid for p in parent.children(recursive=True)}
            new_pids = children_after - children_before

            if new_pids:
                logger.info(f"[MCPPatch] 检测到新创建的 MCP 子进程: {new_pids}")
                registry["mcp_pids"].update(new_pids)

                # 记录 psutil.Process 对象
                for pid in new_pids:
                    try:
                        proc = psutil.Process(pid)
                        cmdline = " ".join(proc.cmdline())
                        logger.info(f"[MCPPatch] 子进程 PID={pid}, cmdline={cmdline[:100]}")
                        registry["processes"].append(proc)
                    except Exception as e:
                        logger.warning(f"[MCPPatch] 无法获取进程信息 PID={pid}: {e}")
        except Exception as e:
            logger.warning(f"[MCPPatch] 获取子进程信息失败: {e}")

        yield read_stream, write_stream

    # 退出上下文管理器后，stdio_client 会自动清理进程


def apply_patch():
    """应用 Monkey Patch"""
    global _original_stdio_client

    try:
        import mcp.client.stdio

        # ✅ 关键：在替换之前保存原始函数
        if not hasattr(mcp.client.stdio, '_patch_applied'):
            _original_stdio_client = mcp.client.stdio.stdio_client
            # 替换为我们的包装函数
            mcp.client.stdio.stdio_client = stdio_client_with_pid_tracking
            # 标记已应用，防止重复
            mcp.client.stdio._patch_applied = True
            logger.info("[MCPPatch] 已应用 MCP stdio_client 补丁")
        else:
            logger.info("[MCPPatch] 补丁已应用，跳过")
    except Exception as e:
        logger.error(f"[MCPPatch] 应用补丁失败: {e}")


def get_mcp_children() -> list:
    """获取当前进程创建的所有 MCP 子进程"""
    registry = get_process_registry()
    # 过滤掉已经不存在的进程
    alive_processes = []
    for proc in registry["processes"]:
        try:
            if proc.is_running():
                alive_processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return alive_processes


def clear_mcp_children():
    """清理所有 MCP 子进程"""
    registry = get_process_registry()

    killed = 0
    for proc in registry["processes"]:
        try:
            if proc.is_running():
                cmdline = " ".join(proc.cmdline())
                logger.warning(f"[MCPPatch] 清理 MCP 子进程 PID={proc.pid}, cmdline={cmdline[:80]}")
                try:
                    # 先尝试优雅终止
                    proc.terminate()
                    proc.wait(timeout=2)
                    killed += 1
                    logger.info(f"[MCPPatch] 成功终止进程 PID={proc.pid}")
                except psutil.TimeoutExpired:
                    # 超时后强制杀死
                    proc.kill()
                    killed += 1
                    logger.warning(f"[MCPPatch] 强制杀死进程 PID={proc.pid}")
                except psutil.NoSuchProcess:
                    # 进程已经不存在
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            logger.warning(f"[MCPPatch] 无法访问进程: {e}")

    # 清空注册表
    registry["mcp_pids"].clear()
    registry["processes"].clear()

    if killed > 0:
        logger.info(f"[MCPPatch] 成功清理 {killed} 个 MCP 子进程")
    else:
        logger.info("[MCPPatch] 没有需要清理的 MCP 子进程")

    return killed


def cleanup_all_python_children():
    """暴力清理所有 Python 子进程（最后的保险）"""
    try:
        current_pid = os.getpid()
        parent = psutil.Process(current_pid)

        all_children = parent.children(recursive=True)

        logger.info(f"[MCPPatch] 当前 worker PID={current_pid}, 发现 {len(all_children)} 个后代进程")

        killed = 0
        for child in all_children:
            try:
                cmdline = " ".join(child.cmdline())
                # 清理所有 Python 进程
                if "python" in cmdline.lower():
                    logger.warning(f"[MCPPatch] 强制终止 Python 子进程 PID={child.pid}, cmdline={cmdline[:80]}")
                    try:
                        child.terminate()
                        child.wait(timeout=1)
                        killed += 1
                    except psutil.TimeoutExpired:
                        child.kill()
                        killed += 1
                    except psutil.NoSuchProcess:
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                logger.warning(f"[MCPPatch] 无法访问进程 PID={child.pid}: {e}")

        if killed > 0:
            logger.info(f"[MCPPatch] 成功清理 {killed} 个 Python 子进程")

        return killed

    except Exception as e:
        logger.error(f"[MCPPatch] 清理所有子进程失败: {e}")
        return 0
