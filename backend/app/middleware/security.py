"""安全中间件：拦截扫描攻击"""
import time
import re
from collections import defaultdict
from typing import Callable
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from loguru import logger


class SecurityMiddleware:
    """安全防护中间件"""

    # 路径白名单（正则表达式）
    ALLOWED_PATHS = [
        r"^/health$",
        r"^/api/v1/analysis/product$",
        r"^/api/v1/analysis/stream/[a-f0-9-]+$",
        r"^/api/v1/analysis/status/[a-f0-9-]+$",
        r"^/api/v1/analysis/check-cookie$",
        r"^/$",
        r"^/assets/.*$",
        r"^/LOGO2\.ico$",
        r"^/config-guide\.png$",
    ]

    # 路径黑名单（高优先级）
    BLOCKED_PATHS = [
        r"\.env", r"\.git", r"\.svn", r"\.htaccess",
        r"admin", r"phpmyadmin", r"backup", r"config$",
        r"wp-admin", r"wp-login", r"actuator",
        r"\.aws", r"\.ssh", r"\.docker",
        r"phpinfo", r"server-status", r"server-info",
    ]

    # User-Agent 黑名单
    BLOCKED_USER_AGENTS = [
        "sqlmap", "nikto", "masscan", "nmap",
        "dirbuster", "gobuster", "wfuzz",
        "burp", "zap", "arachni",
    ]

    # 攻击特征
    ATTACK_PATTERNS = [
        # SQL注入
        r"union\s+select", r"or\s+1\s*=\s*1", r"'--", r";\s*drop",
        # XSS
        r"<script", r"javascript:", r"onerror\s*=",
        # 目录遍历
        r"\.\./", r"\.\.%2f", r"\.\.%5c",
        # 命令注入
        r"\|\s*ls", r";\s*cat", r"&&\s*whoami",
    ]

    def __init__(
        self,
        rate_limit_per_minute: int = 100,
        rate_limit_per_hour: int = 500,
        block_duration_seconds: int = 600,
    ):
        self.rate_limit_per_minute = rate_limit_per_minute
        self.rate_limit_per_hour = rate_limit_per_hour
        self.block_duration = block_duration_seconds

        # IP 访问记录 {ip: [(timestamp, path), ...]}
        self._ip_requests: dict[str, list[tuple[float, str]]] = defaultdict(list)

        # IP 封禁记录 {ip: block_until_timestamp}
        self._blocked_ips: dict[str, float] = {}

        # 编译正则表达式
        self._allowed_paths = [re.compile(p, re.IGNORECASE) for p in self.ALLOWED_PATHS]
        self._blocked_paths = [re.compile(p, re.IGNORECASE) for p in self.BLOCKED_PATHS]
        self._attack_patterns = [re.compile(p, re.IGNORECASE) for p in self.ATTACK_PATTERNS]

    async def __call__(self, request: Request, call_next: Callable) -> Response:
        """中间件入口"""
        client_ip = self._get_client_ip(request)
        path = request.url.path
        method = request.method
        user_agent = request.headers.get("user-agent", "").lower()

        # 1. 检查 IP 封禁状态
        if self._is_ip_blocked(client_ip):
            return self._block_response("IP已被封禁")

        # 2. 检查 User-Agent 黑名单
        if self._is_blocked_user_agent(user_agent):
            self._log_attack(client_ip, path, "恶意User-Agent", user_agent)
            return self._block_response("禁止访问")

        # 3. 检查路径黑名单
        if self._is_blocked_path(path):
            self._log_attack(client_ip, path, "路径扫描", user_agent)
            self._block_ip(client_ip, duration=3600)  # 封禁1小时
            return self._block_response("禁止访问")

        # 4. 检查路径白名单
        if not self._is_allowed_path(path):
            self._log_attack(client_ip, path, "非法路径", user_agent)
            return self._block_response("禁止访问")

        # 5. 检查攻击特征
        query_string = str(request.query_params)
        if self._has_attack_pattern(path + query_string):
            self._log_attack(client_ip, path, "攻击特征", user_agent)
            self._block_ip(client_ip, duration=3600)
            return self._block_response("检测到攻击行为")

        # 6. 频率限制
        if self._is_rate_limited(client_ip):
            self._log_attack(client_ip, path, "频率限制", user_agent)
            self._block_ip(client_ip)
            return self._block_response("请求过于频繁")

        # 记录请求
        self._record_request(client_ip, path)

        # 继续处理请求
        response = await call_next(request)
        return response

    def _get_client_ip(self, request: Request) -> str:
        """获取客户端真实 IP"""
        # 优先使用 X-Forwarded-For（如果有反向代理）
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        # 否则使用直连 IP
        return request.client.host if request.client else "unknown"

    def _is_allowed_path(self, path: str) -> bool:
        """检查路径是否在白名单"""
        return any(pattern.match(path) for pattern in self._allowed_paths)

    def _is_blocked_path(self, path: str) -> bool:
        """检查路径是否在黑名单"""
        return any(pattern.search(path) for pattern in self._blocked_paths)

    def _is_blocked_user_agent(self, user_agent: str) -> bool:
        """检查 User-Agent 是否在黑名单"""
        return any(ua in user_agent for ua in self.BLOCKED_USER_AGENTS)

    def _has_attack_pattern(self, text: str) -> bool:
        """检查是否包含攻击特征"""
        return any(pattern.search(text) for pattern in self._attack_patterns)

    def _is_ip_blocked(self, ip: str) -> bool:
        """检查 IP 是否被封禁"""
        block_until = self._blocked_ips.get(ip, 0)
        if time.time() < block_until:
            return True
        # 解除过期的封禁
        if ip in self._blocked_ips:
            del self._blocked_ips[ip]
        return False

    def _block_ip(self, ip: str, duration: int = None):
        """封禁 IP"""
        duration = duration or self.block_duration
        self._blocked_ips[ip] = time.time() + duration
        logger.warning(f"[Security] 封禁IP: {ip}, 时长: {duration}秒")

    def _is_rate_limited(self, ip: str) -> bool:
        """检查是否超过频率限制"""
        now = time.time()

        # 清理过期记录（保留最近1小时）
        self._ip_requests[ip] = [
            (ts, p) for ts, p in self._ip_requests[ip]
            if now - ts < 3600
        ]

        requests = self._ip_requests[ip]

        # 检查1分钟限制
        requests_last_minute = sum(1 for ts, _ in requests if now - ts < 60)
        if requests_last_minute >= self.rate_limit_per_minute:
            return True

        # 检查1小时限制
        requests_last_hour = len(requests)
        if requests_last_hour >= self.rate_limit_per_hour:
            return True

        return False

    def _record_request(self, ip: str, path: str):
        """记录请求"""
        self._ip_requests[ip].append((time.time(), path))

    def _log_attack(self, ip: str, path: str, reason: str, user_agent: str):
        """记录攻击日志"""
        logger.warning(
            f"[Security] 拦截攻击 - IP: {ip}, 路径: {path}, "
            f"原因: {reason}, UA: {user_agent[:50]}"
        )

    def _block_response(self, message: str) -> JSONResponse:
        """返回拦截响应"""
        return JSONResponse(
            status_code=403,
            content={"error": message, "status": "blocked"}
        )
