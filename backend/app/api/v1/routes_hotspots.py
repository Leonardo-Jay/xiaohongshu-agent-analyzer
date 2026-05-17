from __future__ import annotations

from fastapi import APIRouter

from app.services.home_hotspots import home_hotspots_service


router = APIRouter(prefix="/api/v1/hotspots", tags=["hotspots"])


@router.get("/home", summary="获取首页热搜推荐")
async def get_home_hotspots():
    return home_hotspots_service.get_current()


@router.post("/refresh", summary="手动刷新首页热搜推荐")
async def refresh_home_hotspots():
    return await home_hotspots_service.refresh(source="manual")
