"""小红书第三方 API 封装（apihz.cn）

特点：
- 不需要 Cookie
- 只有帖子详情获取功能
- 没有评论获取功能

接口文档参考：
- 接口地址：https://cn.apihz.cn/api/caiji/xiaohongshu.php
- 必填参数：id（开发者ID）、key（开发者KEY）、url（帖子链接）
"""

import os
import httpx
from loguru import logger

_APIHZ_URL = os.getenv("XHS_APIHZ_URL", "https://cn.apihz.cn/api/caiji/xiaohongshu.php")
_APIHZ_ID = os.getenv("XHS_APIHZ_ID", "")
_APIHZ_KEY = os.getenv("XHS_APIHZ_KEY", "")


def is_apihz_enabled() -> bool:
    """检查是否启用了 apihz.cn 接口"""
    return os.getenv("XHS_API_TYPE", "2") == "1"


def is_apihz_configured() -> bool:
    """检查 apihz.cn 接口是否已配置"""
    return bool(_APIHZ_ID and _APIHZ_KEY)


async def fetch_post_detail_apihz(note_url: str) -> dict:
    """使用 apihz.cn 获取帖子详情

    Args:
        note_url: 帖子完整 URL（如 https://www.xiaohongshu.com/explore/xxx?xsec_token=xxx）

    Returns:
        dict: 帖子详情，包含 note_id, title, desc, nickname 等

    Raises:
        RuntimeError: 接口未配置或调用失败
    """
    if not _APIHZ_ID or not _APIHZ_KEY:
        raise RuntimeError("XHS_APIHZ_ID 或 XHS_APIHZ_KEY 未配置")

    # 处理 URL 中的 & 符号（按文档要求替换为 <>）
    encoded_url = note_url.replace("&", "<>")

    params = {
        "id": _APIHZ_ID,
        "key": _APIHZ_KEY,
        "url": encoded_url,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(_APIHZ_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    if data.get("code") != 200:
        raise RuntimeError(f"apihz.cn 接口错误: {data.get('msg', '未知错误')}")

    return {
        "note_id": data.get("noteId", ""),
        "title": data.get("title", ""),
        "desc": data.get("desc", ""),
        "nickname": data.get("nickname", ""),
        "user_id": data.get("userId", ""),
        "avatar": data.get("avatar", ""),
        "images": [img.get("urlDefault", "") for img in data.get("data", [])],
        "video": data.get("video", []),
    }


async def fetch_posts_detail_batch(note_urls: list[str]) -> list[dict]:
    """批量获取帖子详情

    Args:
        note_urls: 帖子 URL 列表

    Returns:
        list[dict]: 帖子详情列表（失败的帖子会被跳过）
    """
    results = []
    for url in note_urls:
        try:
            detail = await fetch_post_detail_apihz(url)
            results.append(detail)
            logger.debug(f"[apihz] 获取帖子详情成功: {detail.get('note_id')}")
        except Exception as e:
            logger.warning(f"[apihz] 获取帖子详情失败: {url}, 错误: {e}")
    return results
