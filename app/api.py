"""HTTP 接口层：身份从请求头解析，业务错误映射为可区分的响应。

身份头（本地自服务，不接外部认证）：
  X-User-Id / X-Team-Id / X-Role(member|admin)
错误响应统一为 {"error": {"code": ..., "message": ...}}，code 可区分原因。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import ControlPlaneError
from .models import AuditRecord, Environment, Principal, Role
from .service import DEFAULT_TTL_SECONDS, EnvService
from .store import Store


# ---- 序列化 ----

def env_to_dict(e: Environment) -> dict:
    return {
        "env_id": e.env_id,
        "owner_id": e.owner_id,
        "team_id": e.team_id,
        "status": e.status.value,
        "idempotency_key": e.idempotency_key,
        "created_at": e.created_at,
        "updated_at": e.updated_at,
        "expires_at": e.expires_at,
        "version": e.version,
    }


def audit_to_dict(a: AuditRecord) -> dict:
    return {
        "audit_id": a.audit_id,
        "env_id": a.env_id,
        "actor_id": a.actor_id,
        "action": a.action,
        "from_status": a.from_status.value if a.from_status else None,
        "to_status": a.to_status.value if a.to_status else None,
        "detail": a.detail,
        "at": a.at,
    }


# ---- 请求体 ----

class ApplyRequest(BaseModel):
    idempotency_key: str = Field(min_length=1)
    ttl_seconds: float = DEFAULT_TTL_SECONDS


class RenewRequest(BaseModel):
    extend_seconds: float = DEFAULT_TTL_SECONDS


# ---- 依赖：身份解析 ----

def get_principal(
    x_user_id: str = Header(...),
    x_team_id: str = Header(...),
    x_role: str = Header(default="member"),
) -> Principal:
    role = Role.ADMIN if x_role.lower() == "admin" else Role.MEMBER
    return Principal(user_id=x_user_id, team_id=x_team_id, role=role)


def install_exception_handlers(app) -> None:
    @app.exception_handler(ControlPlaneError)
    async def business_error(_: Request, exc: ControlPlaneError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )


def create_router(service: EnvService) -> APIRouter:
    router = APIRouter()

    @router.post("/envs", status_code=201)
    def apply(body: ApplyRequest, p: Principal = Depends(get_principal)):
        return env_to_dict(service.apply(p, body.idempotency_key, body.ttl_seconds))

    @router.get("/envs")
    def list_envs(p: Principal = Depends(get_principal)):
        return [env_to_dict(e) for e in service.list_visible(p)]

    @router.get("/envs/{env_id}")
    def get_env(env_id: str, p: Principal = Depends(get_principal)):
        return env_to_dict(service.get(p, env_id))

    @router.post("/envs/{env_id}/renew")
    def renew(env_id: str, body: RenewRequest, p: Principal = Depends(get_principal)):
        return env_to_dict(service.renew(p, env_id, body.extend_seconds))

    @router.post("/envs/{env_id}/release")
    def release(env_id: str, p: Principal = Depends(get_principal)):
        return env_to_dict(service.release(p, env_id))

    @router.get("/envs/{env_id}/audits")
    def audits(env_id: str, p: Principal = Depends(get_principal)):
        return [audit_to_dict(a) for a in service.audits(p, env_id)]

    @router.post("/expire")
    def expire(p: Principal = Depends(get_principal)):
        reclaimed = service.expire_due()
        return {"reclaimed": [e.env_id for e in reclaimed]}

    @router.get("/usage")
    def usage(p: Principal = Depends(get_principal)):
        return service.usage(p)

    return router


def build_service() -> EnvService:
    return EnvService(Store())
