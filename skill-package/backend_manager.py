import subprocess
import time
import asyncio
import httpx
import psutil
import os
import sys
from pathlib import Path
from loguru import logger


class BackendManager:
    """管理后端服务的生命周期。"""

    def __init__(self, backend_url: str = "http://127.0.0.1:8000"):
        self.backend_url = backend_url
        # 获取 backend 目录
        self.backend_dir = Path(__file__).parent.parent / "backend"
        self.pid_file = Path(__file__).parent / "backend.pid"
        self.process = None

    async def ensure_running(self) -> bool:
        """确保后端服务正在运行。如果未运行，则自动启动。"""
        # 1. 检查服务是否已经健康运行
        if await self._is_healthy():
            logger.info("Backend service is already running and healthy.")
            return True

        # 2. 检查是否有 PID 文件，如果有则检查进程是否存在
        if self.pid_file.exists():
            try:
                old_pid = int(self.pid_file.read_text().strip())
                if psutil.pid_exists(old_pid):
                    proc = psutil.Process(old_pid)
                    # 验证是否是 python 进程且在 backend 目录运行（简单检查）
                    if "python" in proc.name().lower():
                        logger.info(f"Backend already running with PID {old_pid}")
                        return True
                    else:
                        logger.warning(f"PID {old_pid} exists but doesn't look like our backend. Cleaning up.")
                        self.pid_file.unlink()
            except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
                self.pid_file.unlink(missing_ok=True)

        # 3. 启动后端服务
        logger.info(f"Starting backend service in {self.backend_dir}...")

        # 构造启动命令
        # 优先使用 sys.executable 确保环境一致
        python_exe = sys.executable

        try:
            # 在 Windows 下使用 CREATE_NEW_PROCESS_GROUP 避免 Ctrl+C 传播
            # 在 Linux 下使用 start_new_session=True
            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

            # 启动进程
            self.process = subprocess.Popen(
                [python_exe, "run.py"],
                cwd=self.backend_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creation_flags,
                start_new_session=(sys.platform != "win32")
            )

            # 记录 PID
            self.pid_file.write_text(str(self.process.pid))
            logger.info(f"Backend process started with PID {self.process.pid}")

            # 4. 轮询健康检查接口
            max_wait = 30
            for i in range(max_wait):
                await asyncio.sleep(1)
                if await self._is_healthy():
                    logger.info("Backend service is now healthy.")
                    return True

                # 检查进程是否意外退出
                if self.process.poll() is not None:
                    _, stderr = self.process.communicate()
                    logger.error(f"Backend process exited prematurely with code {self.process.returncode}")
                    if stderr:
                        logger.error(f"Error output: {stderr.decode()}")
                    return False

            logger.error(f"Backend service failed to become healthy within {max_wait} seconds.")
            return False

        except Exception as e:
            logger.error(f"Failed to start backend service: {e}")
            return False

    async def _is_healthy(self) -> bool:
        """检查后端服务是否响应健康检查。"""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{self.backend_url}/health")
                return response.status_code == 200
        except Exception:
            return False

    def stop(self):
        """停止后端服务。"""
        # 1. 停止当前管理的进程
        if self.process:
            logger.info(f"Terminating backend process {self.process.pid}...")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Process didn't terminate, killing...")
                self.process.kill()

        # 2. 检查 PID 文件并清理
        if self.pid_file.exists():
            try:
                pid = int(self.pid_file.read_text().strip())
                if psutil.pid_exists(pid):
                    psutil.Process(pid).terminate()
            except Exception:
                pass
            self.pid_file.unlink(missing_ok=True)
