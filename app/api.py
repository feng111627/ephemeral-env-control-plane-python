"""HTTP 接口层（骨架）。"""
from __future__ import annotations

from fastapi import APIRouter

from .service import EnvService
from .store import Store


def create_router(service: EnvService) -> APIRouter:
    router = APIRouter()
    # 路由在后续步骤填充
    return router


def build_service() -> EnvService:
    return EnvService(Store())
