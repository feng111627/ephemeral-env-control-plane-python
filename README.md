# 临时环境自服务控制面

基于 FastAPI 的本地单进程控制面：用户可申请、续期、释放短生命周期测试环境，
并在到期、配额受限或越权时得到可预期且可解释的结果。仅依赖本地内存存储与
常规开源依赖，不接任何外部服务或真实凭据。

## 运行与测试

```bash
uv sync                 # 安装依赖（沿用 uv 管理）
uv run python main.py   # 启动服务（既有入口不变，GET / 行为不变）
uv run pytest           # 运行单测（-v 输出按业务域展示用例通过情况）
```

## 生命周期状态机

```
requested(申请中) -> provisioning(准备中) -> ready(已就绪) -> expiring(到期中) -> reclaimed(已回收)
```

- 合法迁移：
  - `requested -> provisioning -> ready`（`POST /envs/{id}/provision` 本地同步完成）
  - `ready -> expiring -> reclaimed`（到期回收，`POST /envs/sweep` 触发）
  - 任意未回收状态 `-> reclaimed`（主动释放，`POST /envs/{id}/release`）
- 非法迁移被拒绝且原因可区分（响应体 `detail.reason`）：
  - `illegal_transition`：非法跳跃（如对 `requested` 环境续期）、续期次数超限
  - `already_reclaimed`：对已回收环境的任何操作
  - `not_found` / `forbidden` / `quota_exceeded`：见下文

## 配额规则

- 按**用户**与**团队**两个维度统计占用（默认用户 3、团队 8，可在 `Store` 构造时调整）。
- 新申请时若用户或团队占用已达上限，拒绝并返回 `quota_exceeded`。
- 所有状态变更在全局可重入锁内原子完成：并发申请不会超过配额上限；
  并发释放/到期回收同一份额度只归还一次。
- 环境被回收后其占用立即归还，后续申请可复用该额度。

## 权限规则

身份通过请求头识别（本地自包含，无外部认证服务）：

- `X-User-Id`（必填）、`X-Team-Id`、`X-Role: admin`（管理员）

判定规则：

- **本人**（owner）、**同团队成员**、**管理员**可查看和操作环境；
- 其他身份即使目标存在也被拒绝，返回 `403 forbidden`（与 `404 not_found` 可区分）；
- 列表接口按身份过滤，管理员可见全量。

## 幂等语义

- 申请接口要求携带 `idempotency_key`；同一用户重复提交同一键，
  返回首次创建的环境，不会多建环境或重复占用配额（含并发重复提交）。
- 幂等键按用户隔离：不同用户使用相同键互不影响。
- 对已回收环境用同一键重试，返回原记录（结果可解释），不会静默新建。

## 审计

每次状态变化（含申请、续期、释放、到期回收）都会写入审计记录，
可通过 `GET /envs/{id}/audit` 按环境查询（含操作者、动作、前后状态、时间）。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/envs` | 申请环境（幂等） |
| GET | `/envs` | 按身份列出环境 |
| GET | `/envs/usage` | 查看本人/本团队配额占用 |
| GET | `/envs/{id}` | 查看环境 |
| POST | `/envs/{id}/provision` | 推进到已就绪 |
| POST | `/envs/{id}/renew` | 续期（最多 3 次） |
| POST | `/envs/{id}/release` | 主动释放 |
| GET | `/envs/{id}/audit` | 查询审计记录 |
| POST | `/envs/sweep` | 触发到期回收 |

## 本地验证方法

```bash
# 申请 -> 就绪 -> 续期 -> 释放 -> 查审计
H='-H "X-User-Id: u1" -H "X-Team-Id: t1" -H "Content-Type: application/json"'
curl -s -X POST localhost:8000/envs $H -d '{"idempotency_key":"demo-1"}'
# 用返回的 id 依次调用 provision / renew / release / audit

# 到期回收：创建并 provision 后，调用 POST /envs/sweep
# （单测通过将 expires_at 置为过去时间来确定性地验证到期路径）
```

## 测试覆盖

`uv run pytest` 的 verbose 日志按业务域（测试类）分组展示：

- `TestLifecycle`：生命周期合法推进、非法跳跃、重复操作、已回收拒绝、审计完整性
- `TestQuota` / `TestConcurrency`：配额超限拒绝、并发抢配额不超上限、
  并发释放/并发回收额度只释放一次、回收后额度复用
- `TestPermissions`：本人/团队/管理员放行、越权拒绝且与不存在可区分、列表按身份过滤
- `TestIdempotency`：重复提交只建一个环境、并发重复提交、跨用户键隔离
- `TestHttpApi`：HTTP 状态码与 reason 字段、既有根路由行为不变
