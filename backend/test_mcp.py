"""独立测试脚本：直接测试 XhsMcpClient，打印完整错误信息。"""
import asyncio
import sys
import os
import traceback

# 加载 .env
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.tools.mcp_client import XhsMcpClient

async def main():
    print('=== 测试 XhsMcpClient ===' )
    try:
        async with XhsMcpClient() as client:
            print('[OK] MCP client 连接成功，开始搜索...')
            posts = await client.search_posts('华为Mate80', require_num=3)
            print(f'[OK] 搜索成功，获取 {len(posts)} 篇帖子')
            if posts:
                print(f'  示例: {posts[0].get("title", "")[:40]}')
    except Exception as e:
        print(f'[FAILED] {type(e).__name__}: {e}')
        traceback.print_exc()
    except BaseException as e:
        print(f'[FAILED-BASE] {type(e).__name__}: {e}')
        traceback.print_exc()

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
asyncio.run(main())
