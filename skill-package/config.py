"""Configuration for the XHS Analysis MCP Skill."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet


class SkillConfig:
    """Manage local Skill configuration, including encrypted Cookie storage."""

    def __init__(self) -> None:
        configured_dir = os.getenv("XHS_SKILL_CONFIG_DIR", "").strip()
        self.config_dir = Path(configured_dir) if configured_dir else Path.home() / ".claude" / "xhs-analysis"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.cookie_file = self.config_dir / "cookie.enc"
        self.key_file = self.config_dir / ".key"
        self.artifact_dir = self.config_dir / "artifacts"

        self._ensure_encryption_key()

    def _ensure_encryption_key(self) -> None:
        if not self.key_file.exists():
            self.key_file.write_bytes(Fernet.generate_key())
            try:
                self.key_file.chmod(0o600)
            except OSError:
                pass

    def get_cookie(self) -> str | None:
        env_cookie = os.getenv("XHS_COOKIES") or os.getenv("XHS_COOKIE")
        if env_cookie:
            return env_cookie.strip()

        if self.cookie_file.exists() and self.key_file.exists():
            try:
                cipher = Fernet(self.key_file.read_bytes())
                return cipher.decrypt(self.cookie_file.read_bytes()).decode()
            except Exception:
                return None

        return None

    def save_cookie(self, cookie: str) -> None:
        if not self.key_file.exists():
            self._ensure_encryption_key()

        cipher = Fernet(self.key_file.read_bytes())
        self.cookie_file.write_bytes(cipher.encrypt(cookie.strip().encode()))
        try:
            self.cookie_file.chmod(0o600)
        except OSError:
            pass

    def has_cookie(self) -> bool:
        return self.get_cookie() is not None

    def get_skill_mode(self) -> str:
        mode = os.getenv("XHS_SKILL_MODE", "local").strip().lower()
        return mode if mode in {"local", "remote"} else "local"

    def get_backend_url(self) -> str:
        return os.getenv("BACKEND_URL", "http://127.0.0.1:8000")

    def get_backend_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "backend"

    def get_project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    def get_spider_dir(self) -> Path:
        return self.get_project_root() / "Spider_XHS-master"

    def get_artifact_dir(self) -> Path:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        return self.artifact_dir

    def get_timeout(self) -> int:
        return int(os.getenv("ANALYSIS_TIMEOUT", "300"))
