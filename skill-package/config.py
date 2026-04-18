"""Skill 配置管理 - Cookie 加密存储、超时控制、后端 URL"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet


class SkillConfig:
    """管理 Skill 的所有配置，包括 Cookie 加密存储。"""

    def __init__(self) -> None:
        self.config_dir = Path.home() / ".claude" / "xhs-analysis"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.cookie_file = self.config_dir / "cookie.enc"
        self.key_file = self.config_dir / ".key"

        self._ensure_encryption_key()

    def _ensure_encryption_key(self) -> None:
        """确保加密密钥存在且权限正确。"""
        if not self.key_file.exists():
            key = Fernet.generate_key()
            self.key_file.write_bytes(key)
            try:
                self.key_file.chmod(0o600)
            except OSError:
                pass  # Windows 可能不支持 chmod

    def get_cookie(self) -> str | None:
        """获取 Cookie（优先级：环境变量 > 加密配置文件）。"""
        # 1. 环境变量
        env_cookie = os.getenv("XHS_COOKIES") or os.getenv("XHS_COOKIE")
        if env_cookie:
            return env_cookie.strip()

        # 2. 加密配置文件
        if self.cookie_file.exists() and self.key_file.exists():
            try:
                key = self.key_file.read_bytes()
                cipher = Fernet(key)
                encrypted = self.cookie_file.read_bytes()
                return cipher.decrypt(encrypted).decode()
            except Exception:
                return None

        return None

    def save_cookie(self, cookie: str) -> None:
        """加密保存 Cookie 到本地文件。"""
        if not self.key_file.exists():
            self._ensure_encryption_key()

        key = self.key_file.read_bytes()
        cipher = Fernet(key)
        encrypted = cipher.encrypt(cookie.strip().encode())
        self.cookie_file.write_bytes(encrypted)
        try:
            self.cookie_file.chmod(0o600)
        except OSError:
            pass

    def has_cookie(self) -> bool:
        """检查是否已配置 Cookie。"""
        return self.get_cookie() is not None

    def get_backend_url(self) -> str:
        """获取后端服务 URL。"""
        return os.getenv("BACKEND_URL", "http://127.0.0.1:8000")

    def get_backend_dir(self) -> Path:
        """获取后端代码目录（skill-package 的父目录下的 backend）。"""
        return Path(__file__).parent.parent / "backend"

    def get_timeout(self) -> int:
        """获取分析超时时间（秒），默认 5 分钟。"""
        return int(os.getenv("ANALYSIS_TIMEOUT", "300"))
