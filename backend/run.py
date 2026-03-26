"""Windows 下必须在 uvicorn 创建 event loop 之前设置 ProactorEventLoop policy。
直接用 `uvicorn app.main:app` 启动时 policy 设置太晚，改用此脚本启动：
  python run.py
"""
import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
