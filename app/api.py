"""HTTP 路由：挂载到既有 FastAPI 入口，不改变原有启动方式。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.auth import Identity
from app.errors import ControlPlaneError
from app.models import Environment, EnvStatus, RejectReason
from app.service import EnvService
from app.store import Store

# 单进程本地实例（测试可替换）
store = Store()
service = EnvService(store)

router = APIRouter(prefix="/envs", tags=["envs"])

_REASON_STATUS = {
    RejectReason.NOT_FOUND: 404,
    RejectReason.FORBIDDEN: 403,
    RejectReason.QUOTA_EXCEEDED: 409,
    RejectReason.UNSETTLED_ENVS: 409,
    RejectReason.ILLEGAL_TRANSITION: 409,
    RejectReason.ALREADY_RECLAIMED: 409,
}


def get_identity(
    x_user_id: str = Header(...),
    x_team_id: str = Header(default=""),
    x_role: str = Header(default="user"),
) -> Identity:
    return Identity(user_id=x_user_id, team_id=x_team_id, is_admin=x_role == "admin")


def _err(exc: ControlPlaneError) -> HTTPException:
    return HTTPException(
        status_code=_REASON_STATUS[exc.reason],
        detail={"reason": exc.reason.value, "message": str(exc)},
    )


class EnvOut(BaseModel):
    id: str
    owner_id: str
    team_id: str
    status: EnvStatus
    expires_at: float
    renewed_count: int

    @staticmethod
    def of(env: Environment) -> "EnvOut":
        return EnvOut(
            id=env.id,
            owner_id=env.owner_id,
            team_id=env.team_id,
            status=env.status,
            expires_at=env.expires_at,
            renewed_count=env.renewed_count,
        )


class CreateEnvIn(BaseModel):
    idempotency_key: str = Field(min_length=1)


class AuditOut(BaseModel):
    env_id: str
    from_status: EnvStatus | None
    to_status: EnvStatus
    actor_id: str
    action: str
    at: float
    detail: str


@router.post("", response_model=EnvOut, status_code=201)
def create_env(body: CreateEnvIn, actor: Identity = Depends(get_identity)):
    try:
        return EnvOut.of(service.request_env(actor, body.idempotency_key))
    except ControlPlaneError as exc:
        raise _err(exc)


@router.get("", response_model=list[EnvOut])
def list_envs(actor: Identity = Depends(get_identity)):
    return [EnvOut.of(e) for e in service.list_envs(actor)]


@router.get("/usage")
def get_usage(actor: Identity = Depends(get_identity)):
    return service.usage(actor)


@router.get("/{env_id}", response_model=EnvOut)
def get_env(env_id: str, actor: Identity = Depends(get_identity)):
    try:
        return EnvOut.of(service.get_env(actor, env_id))
    except ControlPlaneError as exc:
        raise _err(exc)


@router.post("/{env_id}/provision", response_model=EnvOut)
def provision_env(env_id: str, actor: Identity = Depends(get_identity)):
    try:
        return EnvOut.of(service.provision(actor, env_id))
    except ControlPlaneError as exc:
        raise _err(exc)


@router.post("/{env_id}/renew", response_model=EnvOut)
def renew_env(env_id: str, actor: Identity = Depends(get_identity)):
    try:
        return EnvOut.of(service.renew(actor, env_id))
    except ControlPlaneError as exc:
        raise _err(exc)


@router.post("/{env_id}/release", response_model=EnvOut)
def release_env(env_id: str, actor: Identity = Depends(get_identity)):
    try:
        return EnvOut.of(service.release(actor, env_id))
    except ControlPlaneError as exc:
        raise _err(exc)


@router.get("/{env_id}/audit", response_model=list[AuditOut])
def get_audit(env_id: str, actor: Identity = Depends(get_identity)):
    try:
        records = service.audit_trail(actor, env_id)
    except ControlPlaneError as exc:
        raise _err(exc)
    return [
        AuditOut(
            env_id=r.env_id,
            from_status=r.from_status,
            to_status=r.to_status,
            actor_id=r.actor_id,
            action=r.action,
            at=r.at,
            detail=r.detail,
        )
        for r in records
    ]


@router.post("/sweep", response_model=list[EnvOut])
def sweep_expired(actor: Identity = Depends(get_identity)):
    """触发到期回收（本地验证用；生产可由定时任务调用）。"""
    return [EnvOut.of(e) for e in service.sweep_expired()]
