# 临时环境自服务控制面

在既有 FastAPI 工程上提供短生命周期测试环境的申请、续期、释放与到期回收能力。
单进程本地运行，仅使用内存存储与常规开源依赖，不接任何外部服务或真实凭据。

## 启动与测试

```bash
uv sync                 # 安装依赖（沿用 uv 管理）
uv run python main.py   # 启动服务（既有入口不变，GET / 行为不变）
uv run pytest -v        # 运行单测，末尾按业务分组汇总通过情况
```

## 生命周期状态机

```
requested(申请中) -> provisioning(准备中) -> ready(已就绪) -> expiring(到期中) -> reclaimed(已回收)
```

- 申请成功后本地同步完成准备，沿 `requested -> provisioning -> ready` 推进；
  到期清扫沿 `ready -> expiring -> reclaimed` 推进；任意活跃状态可主动 `release -> reclaimed`。
- 状态只能沿上述路径推进。非法跳跃（如 ready -> requested）、重复释放、
  对已回收环境的任何操作都会被拒绝，且原因可区分：
  - `invalid_transition`(409)：非法状态迁移或对非 ready 环境续期；
  - `already_reclaimed`(409)：对已回收环境的操作；
  - `not_found`(404)：目标不存在；
  - `forbidden`(403)：目标存在但越权；
  - `quota_exceeded`(409)：用户或团队配额超限。
- 每次状态变化（含续期）都会写入审计记录，可通过 `GET /api/envs/{id}/audits` 按环境查询，
  记录包含操作者、动作、迁移前后状态与时间。

## 配额与权限规则

- 配额分用户（默认 3）与团队（默认 8）两个维度；`requested/provisioning/ready/expiring`
  均为未结清状态并占用配额，任一维度超限或有未结清环境占满配额时新申请被拒绝（`quota_exceeded`）。
- 环境被释放或到期回收后，其占用的额度立即可被后续申请复用。
- 权限按身份与归属判定：本人、同团队成员、管理员可查看和操作；其余一律 `forbidden`，
  与 `not_found` 可区分。列表接口按身份过滤（管理员全量）。
- 并发安全：所有"检查 + 变更"复合操作都在同一把进程内锁中完成——并发申请不会超配额，
  并发释放/到期回收同一份额度只释放一次。
- `GET /api/usage` 返回当前调用者用户与团队维度的配额与占用统计。

## 幂等语义

- 申请必须携带 `idempotency_key`，幂等范围是（用户， 幂等键）。
- 同一键重复提交（含并发重试）只产生一个环境，直接返回既有环境，不多建、不重复占用配额；
  即使该环境已被回收，重试仍返回原环境而不新建。
- 不同用户使用同一键互不影响。

## 本地验证方法

身份通过请求头声明（本地自服务，不接外部认证）：
`X-User-Id`、`X-Team-Id`、`X-Role`(member|admin)。

```bash
H='-H "X-User-Id: alice" -H "X-Team-Id: team-a"'
# 申请（幂等键 k1），重复提交返回同一环境
curl -s -X POST localhost:8000/api/envs -H "X-User-Id: alice" -H "X-Team-Id: team-a" \
     -H 'Content-Type: application/json' -d '{"idempotency_key":"k1","ttl_seconds":600}'
# 续期 / 查看 / 审计 / 用量
curl -s -X POST localhost:8000/api/envs/<env_id>/renew -H ... -d '{"extend_seconds":900}'
curl -s localhost:8000/api/envs/<env_id>/audits -H "X-User-Id: alice" -H "X-Team-Id: team-a"
curl -s localhost:8000/api/usage -H "X-User-Id: alice" -H "X-Team-Id: team-a"
# 释放；到期清扫（本地验证钩子）
curl -s -X POST localhost:8000/api/envs/<env_id>/release -H ...
curl -s -X POST localhost:8000/api/expire -H ...
```

错误响应统一为 `{"error": {"code": ..., "message": ...}}`，`code` 即上文列出的可区分原因。
