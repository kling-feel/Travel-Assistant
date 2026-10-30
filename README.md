# 旅行助手

基于 FastAPI、LangGraph 和 PostgreSQL 的虚构旅行助手演示项目。旅客可通过中文对话查询模拟航班、酒店、租车和本人行程，并对九类模拟交易逐笔核对、确认或拒绝。系统不会出票、收款或连接真实供应商库存。

**在线演示：**[adzyforever.com/app](https://adzyforever.com/app)。登录页可公开访问，账号由项目所有者预先授权；仓库不包含线上账号、密码或服务商密钥。

## 功能

- 多轮对话、本人会话与行程、虚构平台政策、带可核查来源的经典地点推荐。
- 航班预订／改签／取消，酒店预订／改期／取消，租车预订／改期／取消。
- 每笔订单变更先展示独立待确认卡；只有本人在卡片核对后确认，服务端才执行事务。
- 航班两端和租车时段按所在地时区显示；报价、取消费用与退款均为模拟值。
- 账号隔离、登录和对话限流、持久会话、刷新恢复，以及模型／搜索不可用时的明确提示。
- 云端 HTTPS、私有 PostgreSQL、每日加密 OSS 备份和漏备邮件提醒。

## 项目结构

| 路径 | 用途 |
| --- | --- |
| `travel_assistant/api/`、`web/static/` | HTTP 接口与旅客页面 |
| `travel_assistant/agents/`、`services/` | 受控对话和业务规则 |
| `travel_assistant/core/`、`db/` | 配置、认证、数据库访问 |
| `migrations/`、`data/fixtures/` | 版本化迁移与脱敏虚构样本 |
| `scripts/`、`deploy/` | 环境、账号、备份和运维工具 |
| `tests/` | 离线业务、对话、页面及运维测试 |
| [`docs/`](docs/README.md) | 当前需求、架构、部署与恢复说明 |

## 本机体验

需要 Python 3.11 和 Docker Engine／Compose。在**全新克隆且无同名数据卷**的 Windows PowerShell 中，先创建虚拟环境与私有演示配置；将 `YYYY-MM-DD` 换为不晚于当天的演示日期：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe scripts\create_container_env.py --as-of YYYY-MM-DD
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-local build app init
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-local up -d --wait db
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-local --profile setup run --rm backup
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-local --profile setup run --rm init
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-local up -d --wait app
```

打开 <http://127.0.0.1:8018/app>，使用 `demo1` 或 `demo2` 和本机 Git 忽略文件 `data/generated/p34a-container.env` 中各自密码登录。本机隔离配置默认关闭付费模型和联网搜索。`backup`／`init` 是**新空库仅一次**的步骤；现有项目不要重跑。停止服务可执行相同 Compose 参数加 `stop app db`，保留卷，不使用 `down -v`。更多边界见[部署与运维](docs/operations.md)。

## 开发检查

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -q
```

常规测试使用内存数据库、固定模型与离线样本，不需要正式云库或付费 API。公开仓库只保留当前操作文档；历史阶段计划与验收原文由项目所有者另行归档。业务边界见[产品需求](docs/requirements.md)，系统结构见[架构说明](docs/architecture.md)，云端和备份操作见[运维手册](docs/operations.md)与[备份恢复](docs/backup-recovery.md)。
