# 客户轨迹自动更新同步服务

从**锦联 ERP** 定时拉取客户运单及物流轨迹，经字段映射后自动写入**企业微信智能表格**的同步服务。
基于 FastAPI + APScheduler 构建，提供 Web 管理页面、JWT 登录鉴权、客户级权限隔离、同步历史与审计日志，支持 Docker 一键部署。

## 功能特性

- **自动同步**：按客户配置 cron 表达式定时同步（APScheduler），也支持页面/API 手动触发
- **增量更新、运单不重复**：本地 SQLite 维护 `(客户, 运单号) → record_id` 映射；有映射走 `update_records`，无映射走 `add_records` 并回填 record\_id，保证同一运单号在智能表格中只出现一次
- **全量历史覆盖**：每次同步从 `2000-01-01` 拉至当前，确保早期运单节点不遗漏
- **已签收跳过**：已签收且已写入智能表格的运单自动跳过（不拉轨迹、不更新），降低接口压力
- **失效自愈**：批量更新遇到 `record not exists` 时自动删除失效映射并转为新增重试
- **并发防护**：同一客户同一时间只允许一个同步任务（进程内锁 + DB 锁兜底）
- **用户与权限**：JWT 登录、bcrypt 密码哈希、admin/user 两种角色、客户 owner + 分配机制
- **可观测**：同步历史、操作审计日志、webhook 连通性测试、`/health` 健康检查
- **容器化**：提供 Dockerfile、docker-compose（含健康检查与数据持久化）、GitHub Actions CI

## 系统架构

```
锦联 ERP (prod.jlfba.com)                企业微信智能表格
  业务员运单管理  ┐                       webhook
  物流轨迹汇总    ├──►  SyncEngine  ──►  add_records / update_records
  轨迹事件明细   ┘        │
                   track_mapper 字段映射
                          │
              SQLite（data/ 目录持久化）
              ├── app.db         用户/客户/分配/历史/审计/锁
              └── record_map.db  (客户+运单号) → record_id
```

### 同步流程

1. 拉取客户全部运单（时间范围 2000/01/01 \~ 现在），按运单号 `serviceOrderNumber` 去重
2. 批量拉取轨迹汇总，预过滤「已签收 + 本地已有记录」的运单
3. 对剩余运单并发拉取轨迹事件明细，并做二次签收校验
4. `track_mapper` 将运单/汇总/轨迹组装为智能表格字段
5. 按本地映射区分新增 / 更新，批量写入企微 webhook；更新失效时自动转新增重试
6. 记录同步结果到 `sync_history`

## 目录结构

```
.
└── sync_service/
    ├── app/
    │   ├── main.py              # FastAPI 入口 + APScheduler 调度
    │   ├── config.py            # .env / customers.yaml / 字段 schema
    │   ├── routers/             # auth、users、me、sync、sync_history、audit_log
    │   ├── services/
    │   │   ├── jinlian_client.py   # 锦联 ERP 接口封装
    │   │   ├── qiwai_client.py     # 企微 webhook + record_id 映射表
    │   │   ├── sync_engine.py      # 同步编排
    │   │   ├── track_mapper.py     # 轨迹 → 智能表格字段映射
    │   │   ├── auth.py             # JWT / 密码校验 / 权限
    │   │   └── db.py               # app.db 存储
    │   └── static/index.html    # 前端管理页面
    ├── data/                    # SQLite 数据（挂载持久化）
    ├── tests/                   # pytest 单元测试
    ├── customers.yaml           # 客户种子配置（首次启动迁移入库）
    ├── .env.example             # 环境变量模板
    ├── Dockerfile
    ├── docker-compose.yml
    ├── requirements.txt
    └── pytest.ini
```

> 说明：服务首次启动时会将 `customers.yaml` 中的客户作为种子数据迁移到 `app.db`。
> 运行时客户配置以数据库为唯一数据源，页面/API 的增删改直接生效并自动重载定时任务。

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
cd sync_service

# 1. 准备配置
cp .env.example .env
# 编辑 .env：必填 JWT_SECRET、ADMIN_USERNAME、ADMIN_PASSWORD 及锦联 ERP 凭证
# 按需编辑 customers.yaml 种子客户

# 2. 构建并启动
docker compose up -d --build

# 3. 查看状态 / 日志
docker compose ps
docker compose logs -f
```

服务启动后：

- 管理页面：<http://localhost:8000/>
- 接口文档（Swagger）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>

`./data` 目录已挂载持久化，容器重建不会丢失用户、客户配置与运单映射。

### 方式二：本地运行

要求 Python 3.12+。

```bash
cd sync_service
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
# source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env   # 按下方说明填写配置

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 配置说明

所有环境变量在 `.env` 中配置（参见 `.env.example`）：

| 变量                          | 必填 | 说明                                    |
| --------------------------- | -- | ------------------------------------- |
| `EXTERNAL_ERP_BASE_URL`     | 是  | 锦联 ERP 地址，默认 `https://prod.jlfba.com` |
| `EXTERNAL_ERP_TOKEN`        | 是  | ERP 接口 Token                          |
| `EXTERNAL_ERP_MAC_ADDR`     | 是  | ERP 设备 MAC 地址                         |
| `EXTERNAL_ERP_USERNAME`     | 是  | ERP 登录用户名                             |
| `EXTERNAL_ERP_PASSWORD_MD5` | 是  | ERP 登录密码的 MD5                         |
| `ERP_VERSION_URL`           | 否  | 客户端版本号拉取地址，失败时使用兜底版本 `1.1.5`          |
| `ADMIN_USERNAME`            | 是  | 首次启动自动创建的管理员用户名                       |
| `ADMIN_PASSWORD`            | 是  | 管理员初始密码                               |
| `JWT_SECRET`                | 是  | JWT 签名随机密钥；**缺失或仍为占位符时服务拒绝启动**        |
| `JWT_EXPIRE_DAYS`           | 否  | Token 有效期（天），默认 7                     |
| `SYNC_DEFAULT_PAGE_SIZE`    | 否  | ERP 分页大小，默认 500                       |
| `SYNC_LOOKBACK_DAYS`        | 否  | 回看天数（历史全量拉取时仅作参考），默认 30               |
| `HTTP_VERIFY_SSL`           | 否  | 是否校验 HTTPS 证书，默认 `false`；生产建议 `true`  |
| `LOG_LEVEL`                 | 否  | 日志级别，默认 `INFO`                        |

生成随机 `JWT_SECRET`：

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 客户配置（customers.yaml 种子）

```yaml
customers:
  '8888275373':
    name: 测试客户
    webhook_url: https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/webhook?key=xxxx
    sync_mode: incremental      # 同步模式
    schedule: "0 9,18 * * *"    # 标准 5 段 cron（每天 9:00、18:00）
    enabled: true
```

客户的智能表格 webhook、cron 计划等也可在启动后通过页面或 API 维护，修改后自动重载调度器。

## 主要 API

除 `/auth/login` 与 `/health` 外均需在请求头携带 `Authorization: Bearer <token>`。

### 鉴权

| 方法   | 路径                      | 说明                                    |
| ---- | ----------------------- | ------------------------------------- |
| POST | `/auth/login`           | 登录获取 JWT（OAuth2 表单：username/password） |
| GET  | `/auth/me`              | 当前用户信息                                |
| POST | `/auth/change-password` | 修改自己的密码                               |

### 同步

| 方法   | 路径                      | 权限                  | 说明                                |
| ---- | ----------------------- | ------------------- | --------------------------------- |
| POST | `/sync/{customer_code}` | owner / 被分配 / admin | 手动触发单客户同步（409 表示已有同步进行中）          |
| POST | `/sync`                 | admin               | 同步所有启用客户                          |
| GET  | `/sync-history`         | 登录                  | 同步历史（可按 `customer_code` 过滤，按权限隔离） |
| GET  | `/schedule`             | admin               | 查看定时任务及下次执行时间                     |

### 客户与用户管理

| 方法             | 路径                                     | 权限            | 说明                      |
| -------------- | -------------------------------------- | ------------- | ----------------------- |
| GET/POST       | `/customers`                           | 登录            | 客户列表 / 新建客户（创建者即 owner） |
| GET/PUT/DELETE | `/customers/{code}`                    | owner / admin | 查看 / 更新 / 删除客户          |
| PUT            | `/customers/{code}/schedule`           | owner / admin | 更新 cron 表达式             |
| POST           | `/customers/{code}/webhook-test`       | 可访问者          | 测试 webhook 连通性，不产生脏数据   |
| POST/DELETE    | `/customers/{code}/assign[/{user_id}]` | admin         | 分配 / 取消分配客户             |
| GET/POST       | `/users`                               | admin         | 用户列表 / 创建用户             |
| PUT            | `/users/{id}/enabled`                  | admin         | 启用 / 禁用用户               |
| POST           | `/users/{id}/reset-password`           | admin         | 重置用户密码                  |
| GET            | `/me/customers`、`/me/sync-history`     | 登录            | 我的客户 / 我的同步历史           |
| GET            | `/audit-log`                           | admin         | 审计日志                    |

调用示例：

```bash
# 登录
curl -X POST http://localhost:8000/auth/login \
  -d "username=admin&password=你的密码"

# 触发同步
curl -X POST http://localhost:8000/sync/8888275373 \
  -H "Authorization: Bearer <access_token>"
```

同步响应：

```json
{
  "customer_code": "8888275373",
  "total": 120,
  "new": 3,
  "updated": 40,
  "skipped": 77,
  "failed": 0,
  "errors": []
}
```

## 测试

```bash
cd sync_service
pip install pytest
pytest
```

CI（`.github/workflows/ci.yml`）在推送到 `main` 或发起 PR 时执行：依赖安装 → ruff 检查 → 编译校验 → pytest → 构建 Docker 镜像并导出为 artifact；配置私有仓库 Secrets 后可自动推送镜像。

## 运维说明

- **数据备份**：定期备份 `sync_service/data/` 目录（`app.db` 权限运营数据、`record_map.db` 运单映射）。删除 `record_map.db` 不会丢数据，但下次同步会因找不到 record\_id 而对全部运单执行新增，可能造成重复，务必谨慎。
- **时区**：容器内固定 `TZ=Asia/Shanghai`，cron 按北京时间执行。
- **企微 webhook 限制**：仅支持 `add_records` / `update_records`，不支持删除与查询，因此运单记录的完整生命周期管理需另行使用企微开放 API。
- **排障**：通过 `docker compose logs -f sync-service` 查看日志；同步失败明细见同步历史中的 `errors` 字段或 `/audit-log`。

