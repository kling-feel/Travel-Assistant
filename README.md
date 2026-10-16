# 旅行助手（企业项目改造中）

这是一个使用虚构数据的旅行助手求职作品。保留 FastAPI 和 LangGraph 主助手／专门助手结构，逐步补上身份隔离、逐笔交易确认、持久化与测试。当前为 **P0 基础搭建阶段**：可以生成演示数据和启动基础 API；旧交易接口默认关闭，新的航班／酒店交易流程尚未完成。

项目方案见 [产品需求](docs/enterprise/产品需求.md)、[软件架构](docs/enterprise/软件架构.md) 和 [当前进度](docs/enterprise/当前进度.md)。

## 数据从哪里来

仓库中的 `data/fixtures/demo_source.sqlite` 是从原有的大型虚构 SQLite 数据精选、匿名化得到的小样本：14 条汉堡至巴塞尔航班、3 家巴塞尔酒店、2 张演示机票，约 24 KB。原有 `travel_new.sqlite` 和 `travel2.sqlite` 保持不变，也不需要放入 Docker 镜像或 Git。脚本每次按运行日期平移航班时间、重设酒店演示可用日期，再把选定数据写入 PostgreSQL 的 `demo_source` 暂存结构。后续阶段会建立正式旅客与订单模型。

如果需要从自己的大型虚构 SQLite 重新精选数据，可以运行 `python scripts/build_demo_fixture.py --source travel_new.sqlite --output data/fixtures/another_demo_source.sqlite`。脚本不会覆盖已有 fixture。默认导入使用仓库内的小样本；可用 `--source` 指向另一份结构相同的 SQLite 文件。

## Windows 本机准备

1. 安装 Python 3.11。安装 [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/) 并启动。Docker 的 WSL 2 后端可能需要先在**管理员 PowerShell** 中运行 `wsl --install` 并重启电脑；按官方安装向导操作。
2. 在项目目录打开 PowerShell，依次运行：

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe scripts\init_local_env.py
   docker compose up -d --wait db
   .\.venv\Scripts\python.exe scripts\import_demo_data.py --apply
   ```

   `.env` 会自动生成随机的本机数据库密码和 JWT 密钥，且已被 Git 忽略。不要把它、真实模型 API 密钥或原始大型 SQLite 文件上传到公开仓库。

3. 查看导入结果：

   ```powershell
   docker compose exec -T db psql -U travel_app -d travel_demo -c "SELECT count(*) FROM demo_source.flights"
   docker compose exec -T db psql -U travel_app -d travel_demo -c "SELECT count(*) FROM demo_source.flight_orders"
   ```

   预期分别为 14 和 2。反复运行导入脚本会在一笔数据库事务中重置此暂存数据。Docker 的命名数据卷保存 PostgreSQL 数据；`docker compose down` 不会清除该卷。

4. 启动基础 API：

   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

   浏览 `http://127.0.0.1:8000/health` 应得到 `{"status":"ok"}`。`/docs` 可打开接口文档。当前旧用户管理和图交易接口默认关闭，因为它们尚未完成旅客身份绑定与逐笔确认；**不要把 `ENABLE_LEGACY_API` 改为 `true` 后对外开放服务**。正式的旅客接口、聊天和订单页面属于后续阶段。

## 模型配置

P0 启动健康检查无需模型密钥，也不会调用模型。后续阶段可通过被忽略的 `.env` 设置 `AI_MODEL`、`AI_API_KEY` 和 `AI_BASE_URL`，分别选择 GPT、DeepSeek 或本地兼容接口。配置有效性和工具调用能力将在 P2 实测；本地模型能聊天不代表它可以安全执行订单工具。Tavily 网页搜索是可选项，未设置 `TAVILY_API_KEY` 时会明确返回未配置。

## 当前限制

- `demo_source` 是精选数据的暂存结构，尚未接到新的业务 API；目前不能演示真实的改签／预订闭环。
- 尚未在本机 Docker 中验证 PostgreSQL 导入；完成 Docker 安装后需要运行上述导入和计数检查。
- 旧代码的第三方密钥曾出现在源码和本地私有配置中。源码引用已移除；若这些密钥仍有效，所有者需要到相应服务后台轮换。
