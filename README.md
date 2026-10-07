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

## 额度排队与自动放行

额度不足时，除了直接失败（`quota_exceeded`），还可以带同一个幂等键
`POST /envs/queue` 先排队占位，等额度释放后自动放行。

排队条目状态机：

```
waiting(排队中) -> promoted(已放行) / cancelled(已取消) / expired(已超时)
```

规则：

- **入队**：`POST /envs/queue` 携带 `idempotency_key`。
  - 同键已有环境：直接返回关联该环境的 `promoted` 记录（幂等，不多建）；
  - 同键已有排队中的记录：返回原记录，同人同键只占一个位置；
  - 队列无人在等且额度充足：立即放行，效果与直接申请一致；
  - 否则排到队尾。取消或超时后用同键再排，会生成新记录、排到队尾。
- **自动放行**：任何额度归还（主动释放 `release`、到期回收 `sweep`）都会
  在同一锁内按入队先后（严格 FIFO）尝试放行队首：先排的先拿到，
  队首因配额仍不足放不了时后面的人不跳过。多份释放同时发生也不会
  超发、不漏放、不重复放行；放行与本人同时手动重试申请撞车时，
  幂等键保证只存在一个环境。
- **查询/取消**：`GET /envs/queue/{id}` 查看状态，`POST /envs/queue/{id}/cancel`
  放弃排队（重复取消幂等；已放行/已失效的不可取消，返回 `illegal_transition`）。
- **超时兜底**：排队中的记录超过有效期（默认 1800 秒，`EnvService` 的
  `queue_ttl_seconds` 可调）后由 `POST /envs/queue/sweep`（或调用
  `service.sweep_queue`）置为 `expired`，不再占位。
- **权限**：排队记录仅**本人**或**管理员**可见可操作（与环境不同，
  同团队成员也不行）；列表接口只返回本人记录，管理员可见全量。
- **审计**：入队、放行、取消、失效每步都写排队审计，
  `GET /envs/queue/{id}/audit` 可追溯某申请是否排过队、何时放行。

与既有语义的兼容范围：

- 放行创建的环境从 `requested` 起步，走既有生命周期与配额统计，
  用户看到的结果与当初直接申请成功完全一致；
- 直接申请接口 `POST /envs` 行为不变：额度够就立即建，不够仍返回
  `quota_exceeded`，不隐式排队；排队是显式选择；
- 排队只约束"释放触发的放行"顺序，不阻止额度充足时的直接申请；
- 排队中的记录不占用环境配额，放行时才占用。

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
| POST | `/envs/queue` | 带幂等键排队等额度 |
| GET | `/envs/queue` | 列出本人排队记录（管理员全量） |
| POST | `/envs/queue/sweep` | 触发排队超时失效 |
| GET | `/envs/queue/{id}` | 查看排队状态（本人/管理员） |
| POST | `/envs/queue/{id}/cancel` | 取消排队（本人/管理员） |
| GET | `/envs/queue/{id}/audit` | 查询排队审计记录 |

## 本地验证方法

```bash
# 申请 -> 就绪 -> 续期 -> 释放 -> 查审计
H='-H "X-User-Id: u1" -H "X-Team-Id: t1" -H "Content-Type: application/json"'
curl -s -X POST localhost:8000/envs $H -d '{"idempotency_key":"demo-1"}'
# 用返回的 id 依次调用 provision / renew / release / audit

# 到期回收：创建并 provision 后，调用 POST /envs/sweep
# （单测通过将 expires_at 置为过去时间来确定性地验证到期路径）

# 排队验证：默认用户配额 3，先申请 3 个占满，再排队、释放、观察自动放行
H2='-H "X-User-Id: u2" -H "X-Team-Id: t2" -H "Content-Type: application/json"'
for i in 1 2 3; do curl -s -X POST localhost:8000/envs $H2 -d "{\"idempotency_key\":\"f$i\"}"; done
curl -s -X POST localhost:8000/envs/queue $H2 -d '{"idempotency_key":"q1"}'   # waiting
curl -s -X POST localhost:8000/envs/{env_id}/release $H2                      # 释放一个
curl -s localhost:8000/envs/queue/{entry_id} $H2                              # promoted + env_id
curl -s localhost:8000/envs/queue/{entry_id}/audit $H2                        # enqueue/promote 记录
curl -s -X POST localhost:8000/envs/queue/sweep $H2                           # 触发排队超时失效
```

## 测试覆盖

`uv run pytest` 的 verbose 日志按业务域（测试类）分组展示：

- `TestLifecycle`：生命周期合法推进、非法跳跃、重复操作、已回收拒绝、审计完整性
- `TestQuota` / `TestConcurrency`：配额超限拒绝、并发抢配额不超上限、
  并发释放/并发回收额度只释放一次、回收后额度复用
- `TestPermissions`：本人/团队/管理员放行、越权拒绝且与不存在可区分、列表按身份过滤
- `TestIdempotency`：重复提交只建一个环境、并发重复提交、跨用户键隔离
- `TestEnqueue`：额度满可排队、同键去重、取消后再排排到队尾、不插队
- `TestAutoPromote`：释放/到期回收触发自动放行、FIFO 顺序、并发释放不超发、
  自动放行与手动重试只建一个环境
- `TestQueueExpiry`：排队超时自动失效、终态可区分、失效后可重排
- `TestQueuePermissions` / `TestQueueAudit`：排队记录仅本人/管理员可操作、
  入队/放行/取消/失效全程审计可追溯
- `TestHttpApi` / `TestQueueHttpApi`：HTTP 状态码与 reason 字段、排队接口端到端、
  既有根路由行为不变
