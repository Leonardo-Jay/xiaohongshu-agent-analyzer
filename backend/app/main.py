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
from loguru import logger

load_dotenv()

if not os.getenv("QIANFAN_BEARER_TOKEN"):
    logger.warning("未配置 QIANFAN_BEARER_TOKEN，大模型分析调用将不可用")

logger.info(
    "当前 LLM Provider=Qianfan, model={} ",
    os.getenv("QIANFAN_MODEL") or "qwen3-14b",
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
