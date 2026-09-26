# 低温探测器标定记录系统

低温探测器的标定结论依赖原始读数与前序结论。当一条读数被发现失真时，
系统必须在**同一持久化提交**中使该记录及全部可达下游结论失效，
并以操作标识保证裁决的幂等与冲突可检测。

## 架构

```
宿主机 ──WEB_PORT──▶ web (nginx) ──/api,/health──▶ api (FastAPI + SQLite/WAL)
        ──API_PORT─────────────────────────────────▶
                       verify (一次性验收：pytest → 构建检查 → API/HTTP 冒烟 → 退出码)
```

- **api**：FastAPI 服务，SQLite 持久化（命名卷 `calib_data`），单 worker。
- **web**：nginx 提供静态页面，并将 `/api`、`/health` 同源代理到 api。
- **verify**：一次性验收服务，运行完毕后退出并以退出码报告结果。

## 快速开始

```bash
docker compose up --build          # 启动页面与服务（verify 会顺带执行一次验收后退出）
# 页面:  http://localhost:8080     （WEB_PORT 可配）
# API:   http://localhost:8000     （API_PORT 可配）
# 健康:  curl http://localhost:8080/health  或  http://localhost:8000/health
```

端口配置：复制 `.env.example` 为 `.env` 修改，或直接设置环境变量
`WEB_PORT` / `API_PORT`。

## 一次性验收（verify）

```bash
docker compose up --build verify --exit-code-from verify   # 退出码即验收结果
# 或
docker compose build && docker compose run --rm verify
```

verify 依次执行并以退出码报告（0=通过，1=失败）：

1. **代码测试**（pytest）：级联失效、幂等重放、操作冲突、可定位错误、
   并发竞争不变量、重启持久化；
2. **构建检查**：字节码编译、应用可装配、前端资产与编排文件完整性；
3. **API/HTTP 冒烟**：对运行中的服务复现级联失效与并发竞争不变量，
   并冒烟页面与 `/api`、`/health` 代理。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/api/records?valid=true|false` | 记录列表（编号、有效性、直接依据、失效来源） |
| POST | `/api/records` | 创建原始/推导记录（推导记录需 `depends_on` ≥ 1） |
| GET | `/api/records/{id}` | 单条记录 |
| GET | `/api/records/{id}/lineage` | 谱系：直接依据/被引、全部上下游 |
| POST | `/api/records/{id}/invalidate` | 携带 `operation_id` 的失效裁决（级联） |
| GET | `/api/operations/{operation_id}` | 操作重放结果查询 |

错误统一为 `{"error": {"code", "message", "details"}}`，错误码可定位：
`DEPENDENCY_NOT_FOUND`、`SELF_REFERENCE`、`CYCLE_DETECTED`、
`DEPENDENCY_INVALID`、`OPERATION_CONFLICT`、`RECORD_NOT_FOUND`、
`VALIDATION_ERROR` 等。

## 关键不变量与设计

- **级联失效单提交**：目标记录与全部可达下游记录的失效标记、失效来源
  （`invalidation_root` + `operation_id`）以及操作流水在同一事务提交。
  失效来源只在首次失效时写入，后续裁决不会覆盖，保证稳定。
- **幂等裁决**：`operation_id` 唯一。相同标识 + 相同目标 → 原样返回首次
  结果（响应头 `X-Idempotent-Replay: true`）；相同标识 + 不同目标 →
  `409 OPERATION_CONFLICT`，状态不变。失败的裁决（如目标不存在）不消耗
  操作标识。
- **竞争不变量**：所有写事务经单连接可重入锁 + `BEGIN IMMEDIATE` 串行化。
  创建推导记录在事务内重新校验依据有效性；裁决事务级联覆盖当时全部下游。
  因此任意交错下，创建要么先于裁决（被级联失效），要么后于裁决（被拒绝），
  绝不会出现有效记录依赖失效记录。故 api 以单 worker 运行。
- **持久化**：记录、依赖边、失效状态、操作流水均落盘于命名卷；
  重启后谱系、失效状态与操作重放结果仍可查询，编号序列不回退。
- **只增 DAG**：依据必须在创建时已存在且有效，依赖边不可修改；
  环检测为防御性兜底（含自引用检查）。

## 本地开发（无 Docker）

```bash
cd api
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest                       # 代码测试
DATABASE_PATH=/tmp/dev.db uvicorn app.asgi:app --port 8000
# 另开终端：
API_BASE_URL=http://127.0.0.1:8000 python -m verify.run   # 完整验收（页面冒烟自动跳过）
```

## 目录结构

```
compose.yaml          # 编排：api / web / verify
api/
  Dockerfile          # 单一镜像：api 服务与 verify 共用
  app/                # FastAPI 应用（db / service / main / asgi）
  tests/              # pytest：级联、幂等、冲突、并发、持久化
  verify/             # 一次性验收：run / buildcheck / smoke
web/                  # 静态页面 + nginx 配置
```
