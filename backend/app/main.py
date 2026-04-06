import asyncio
import os
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Windows requires ProactorEventLoop to support subprocess (used by MCP stdio client)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

load_dotenv()

provider = os.getenv("LLM_PROVIDER", "qianfan").strip().lower()
if provider == "longcat":
    if not os.getenv("LONGCAT_API_KEY"):
        logger.warning("未配置 LONGCAT_API_KEY，大模型分析调用将不可用")
    logger.info(
        "当前 LLM Provider=Longcat, model={}",
        os.getenv("LONGCAT_MODEL") or "deepseek-chat",
    )
elif provider == "modelscope":
    if not os.getenv("MODELSCOPE_API_KEY"):
        logger.warning("未配置 MODELSCOPE_API_KEY，大模型分析调用将不可用")
    logger.info(
        "当前 LLM Provider=ModelScope, model={}",
        os.getenv("MODELSCOPE_MODEL") or "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
    )
else:
    if not os.getenv("QIANFAN_BEARER_TOKEN"):
        logger.warning("未配置 QIANFAN_BEARER_TOKEN，大模型分析调用将不可用")
    logger.info(
        "当前 LLM Provider=Qianfan, model={}",
        os.getenv("QIANFAN_MODEL") or "ernie-4.5-21b-a3b",
    )

from app.api.v1.routes_analysis import router as analysis_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AI 产品舆情分析系统启动")
    yield
    logger.info("AI 产品舆情分析系统关闭")


app = FastAPI(
    title="AI 产品舆情分析系统",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(analysis_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 生产环境：提供前端静态文件
# ---------------------------------------------------------------------------
_DIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "dist"
)

if os.path.isdir(_DIST):
    _assets = os.path.join(_DIST, "assets")
    if os.path.isdir(_assets):
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/LOGO2.ico")
    async def favicon():
        return FileResponse(os.path.join(_DIST, "LOGO2.ico"))

    @app.get("/config-guide.png")
    async def config_guide():
        return FileResponse(os.path.join(_DIST, "config-guide.png"))

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        return FileResponse(os.path.join(_DIST, "index.html"))
