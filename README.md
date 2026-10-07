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

## 排队等候与自动放行

额度不足时，申请（`POST /envs`）仍保持原语义直接返回 `quota_exceeded`；
用户可改用排队接口先占住位置，等额度释放后自动放行，无需本人再次确认。

### 排队登记

- `POST /envs/waits`，携带申请时同一个 `idempotency_key`：
  - 该键已经有环境（含已回收）→ `409 already_requested`，与申请接口的幂等解释一致；
  - 同一用户用同一键**仍在排队中**重复登记 → 返回同一条排队记录，不重复占位；
  - 取消/超时后再用同键登记 → 生成新记录，**重新排到队尾**；
  - 登记时若额度恰好可用（或本人个人额度空、团队额度也空）→ 立即放行。
- 响应体 `{wait, env}`：`env` 非空表示已立即放行；为空表示仍在排队。
- 排队记录有有效期（默认 1800 秒，`EnvService(wait_ttl_seconds=...)` 可调）。

### 四种可区分的结果

排队记录状态 `wait.status`：

- `waiting`（排队中）、`admitted`（已放行）、`cancelled`（已取消）、`expired`（已超时）。

### 自动放行规则

- 触发时机：团队内有额度归还——无论是 `release` 主动释放，还是 `sweep` 到期自动回收。
- 顺序：按入队先后（全局递增序号）**严格 FIFO，先排先得**；队头因用户个人额度已满
  暂时进不来时，后面的人不会越过它。
- 原子性：归还额度与放行排在**同一把全局可重入锁、同一个原子事务**内完成：
  - 多个释放/回收并发发生时，放行数量不会超过释放出的额度，团队/用户上限绝不被突破；
  - 同一个排队者不会被放行两次，排队者也不会被漏掉（释放 N 份、队首有效就放行至多 N 人）；
  - 自动放行与用户同一时刻用同一幂等键手动重试申请竞争时，二者收敛到同一个环境，
    绝不会多建一个环境或多占一份配额。
- 放行结果与直接申请完全一致：创建 `requested` 状态的环境、绑定幂等键、占用用户/团队
  配额、写 `request` 审计；之后照常走 `provision / renew / release / sweep` 生命周期。

### 排队的超时兜底

- 超过有效期仍在排队的记录自动失效：
  - 惰性失效：查询/取消时若已过期即时置为 `expired`；
  - 兜底任务：`POST /envs/waits/sweep`（本地验证/定时任务用）批量失效，
    并立即为受影响团队继续放行（队头超时后，后面的人才能顶上）。
- 已取消/已超时的记录不再占用幂等位与队列位置，可重新登记（排到队尾）。

### 隔离与权限

- 排队比环境更私密：仅**本人**与**管理员**可查看、取消；同团队其他成员也不行
  （环境的"同团队可操作"语义不适用于排队记录），越权返回 `403 forbidden`，
  与 `404 not_found` 可区分。
- 幂等位按用户隔离：不同用户用相同键互不影响。
- 列表 `GET /envs/waits`：普通用户只看到自己的全部记录（含历史），管理员看全量。

### 排队审计

排队的每一步（`join` 登记、`admit_auto` 自动放行、`admit_manual` 手动重试收敛、
`cancel` 取消、`expire_wait` 超时失效）都写独立排队审计，可通过
`GET /envs/waits/{id}/audit` 按排队记录查询；放行创建的环境同时有自己的
`request` 审计（`detail="auto-admitted from waitlist"`），两侧可互查。

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
| POST | `/envs` | 申请环境（幂等；额度不足直接拒绝） |
| GET | `/envs` | 按身份列出环境 |
| GET | `/envs/usage` | 查看本人/本团队配额占用 |
| POST | `/envs/waits` | 排队等候（幂等；可立即放行或排队） |
| GET | `/envs/waits` | 查看本人排队记录（管理员全量） |
| GET | `/envs/waits/{id}` | 查询排队状态（排队中/已放行/已取消/已超时） |
| POST | `/envs/waits/{id}/cancel` | 主动放弃排队 |
| GET | `/envs/waits/{id}/audit` | 查询排队记录留痕 |
| POST | `/envs/waits/sweep` | 触发排队超时失效兜底 |
| GET | `/envs/{id}` | 查看环境 |
| POST | `/envs/{id}/provision` | 推进到已就绪 |
| POST | `/envs/{id}/renew` | 续期（最多 3 次） |
| POST | `/envs/{id}/release` | 主动释放（同时触发排队放行） |
| GET | `/envs/{id}/audit` | 查询审计记录 |
| POST | `/envs/sweep` | 触发到期回收（同时触发排队放行） |

## 本地验证方法

```bash
# 申请 -> 就绪 -> 续期 -> 释放 -> 查审计
H='-H "X-User-Id: u1" -H "X-Team-Id: t1" -H "Content-Type: application/json"'
curl -s -X POST localhost:8000/envs $H -d '{"idempotency_key":"demo-1"}'
# 用返回的 id 依次调用 provision / renew / release / audit

# 到期回收：创建并 provision 后，调用 POST /envs/sweep
# （单测通过将 expires_at 置为过去时间来确定性地验证到期路径）

# 排队 -> 释放触发自动放行 -> 查排队状态与留痕：
# 1) 先把 u1 的 3 个用户额度占满（连续申请 key-1/key-2/key-3）
# 2) 排队（此时返回 waiting、env 为 null）：
curl -s -X POST localhost:8000/envs/waits $H -d '{"idempotency_key":"wait-1"}'
# 3) 释放任意一个已占环境，再查该排队记录 -> status=admitted、env_id 已回填：
curl -s localhost:8000/envs/waits/{wait_id} $H
curl -s localhost:8000/envs/waits/{wait_id}/audit $H
# 4) 超时兜底：调用 POST /envs/waits/sweep（单测将 expires_at 置为过去时间）
```

## 测试覆盖

`uv run pytest` 的 verbose 日志按业务域（测试类）分组展示：

- `TestLifecycle`：生命周期合法推进、非法跳跃、重复操作、已回收拒绝、审计完整性
- `TestQuota` / `TestConcurrency`：配额超限拒绝、并发抢配额不超上限、
  并发释放/并发回收额度只释放一次、回收后额度复用
- `TestPermissions`：本人/团队/管理员放行、越权拒绝且与不存在可区分、列表按身份过滤
- `TestIdempotency`：重复提交只建一个环境、并发重复提交、跨用户键隔离
- `TestWaitlist`：排队登记/状态四态可区分、额度释放（主动释放+到期回收）触发
  自动放行、严格 FIFO 先排先得不漏放、多份额度并发释放不超发/不重放、
  自动放行与手动重试竞争只建一个环境、重复排队只占一位、取消/超时后再排到队尾、
  超时失效（sweep+惰性）、按身份隔离（同团队也不可动他人排队）、排队全步骤审计
- `TestWaitHttpApi`：排队 HTTP 全流程、201/403/404/409 状态码与 reason、
  隔离与管理员放行、waits 审计与 sweep 接口
- `TestHttpApi`：HTTP 状态码与 reason 字段、既有根路由行为不变
