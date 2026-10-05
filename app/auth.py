"""身份解析：从请求头识别用户、团队与角色（本地自包含，不接外部服务）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    user_id: str
    team_id: str
    is_admin: bool = False
