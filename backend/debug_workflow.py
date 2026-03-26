"""诊断脚本 — 直接运行 run_analysis，打印完整异常和 queue 事件。运行完可删除。"""
import asyncio
import os
import sys
import traceback

# 设置工作目录和路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

print("=== 导入 workflow ===", flush=True)
try:
    from app.graph.workflow import run_analysis
    print("=== 导入成功 ===", flush=True)
except Exception as e:
    print(f"=== 导入失败: {e} ===", flush=True)
    traceback.print_exc()
    sys.exit(1)


async def main():
    q: asyncio.Queue = asyncio.Queue()
    print("=== 开始运行 run_analysis ===", flush=True)
    try:
        await run_analysis("iPhone 16", "debug-001", q)
    except BaseException as e:
        print(f"=== run_analysis 顶层异常: [{type(e).__name__}] {e!r} ===", flush=True)
        traceback.print_exc()

    print("\n=== Queue 事件 ===", flush=True)
    while not q.empty():
        item = q.get_nowait()
        print("EVENT:", item, flush=True)


asyncio.run(main())
