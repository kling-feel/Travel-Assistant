# 部署与运维

## 当前正式站点

正式入口为 <https://adzyforever.com/app>。部署于 Ubuntu 24.04、2 vCPU／2 GiB 的香港轻量应用服务器，使用 `travel-cloud` Compose 项目。网站持续在线，Caddy 发布 80／443；`app` 的 8000 和 PostgreSQL 的 5432 只在容器网络内。域名、服务器和私有凭据由项目所有者管理。

账号只在服务器私有目录生成。所有者本机的 Git 忽略文件 `data/generated/云端部署账号密码-私有.md` 与 `data/generated/演示账号-私有.md` 保存现有授权账号及密码；**这两份文件不属于仓库交付物**。初始 `demo1`／`demo2` 在公网环境停用，三个独立访客账号初始没有行程。

## 本机新环境体验

安装 Python 3.11、Docker Engine／Compose，并从 `requirements.lock` 安装依赖。以下步骤只用于**首次创建的新项目和空卷**；`--as-of` 选不晚于当天的演示日期。私有环境文件已存在时创建脚本会拒绝覆盖。

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

打开 `http://127.0.0.1:8018/app`，使用 `demo1`／`demo2` 与新生成的 `data/generated/p34a-container.env` 中对应密码登录。本机隔离配置默认关闭付费模型与联网搜索。停止服务可用相同 Compose 参数加 `stop app db`；保留卷，不使用 `down -v`。若任何初始化步骤失败，先检查空库与备份，不在非空库重试。

## 正式主机日常检查与启停

用项目所有者的 SSH 专用密钥连接，进入 `/home/travelops/travel-assistant`。以下命令在**服务器**执行，均使用原项目与原数据卷：

```bash
python3 scripts/preflight_cloud_env.py
python3 scripts/cloud_compose.py status
docker ps
free -m
df -h /
```

正常启动：先 `python3 scripts/cloud_compose.py db-up`，再 `python3 scripts/cloud_compose.py app-up`，最后核对 `status` 与 HTTPS 登录页。明确需要暂停时，先核对最近备份，再用 `python3 scripts/cloud_compose.py stop`。这只停止服务并保留五个命名卷。正式站点**不得重跑** `create_cloud_env.py`、`empty-backup`、`init` 或演示批次导入，也不得执行 `down -v`。

## 全新服务器的首次部署

新服务器须先配置专用非 root SSH 管理账号、Docker、域名解析、80／443 入站规则与受限私有目录。将经过审查的固定 Git 提交归档传到新目录并校验 SHA256；不要复制本机 `.env`、`data/generated`、私钥或原始数据源。只有在核对目标 Compose 项目无旧容器、旧卷和订单后，才生成 `data/generated/cloud.env`，填入专用模型／搜索密钥、设为 `0600`，运行 `scripts/preflight_cloud_env.py`。在 2 GiB 主机串行完成 `cloud_compose.py build`、`db-up`、`empty-backup`、`init`；初始化只运行一次。随后创建授权账号、停用种子账号，再运行 `app-up`。确认 HTTPS、授权登录、账号隔离、模拟声明、私有端口与备份恢复后开放展示。

## 更新与回退

每次发布先记录运行中镜像 ID、批次和订单摘要，制作服务器外加密备份。将固定提交归档传到**新发布目录**并核对哈希；在 2 GiB 主机单路构建，保留旧应用镜像回退标签。新镜像私有就绪后只替换 `app`，沿用原 `db`、`proxy`、配置和五卷；核对登录、订单摘要、容器健康及 HTTPS。失败时恢复旧镜像并只重建 `app`，不重置数据库。不在正式目录盲跑 `git pull` 后直接构建／初始化。

## 授权账号

服务器项目目录内使用 `python3 scripts/cloud_compose.py admin create <用户名> <显示名> --output /run/travel-credentials/<新文件>.json` 创建账号；`admin rotate <用户名> --output <另一个新文件>` 轮换，`admin disable <用户名>` 停用。随机密码只写服务器私有文件，命令不打印密码。轮换后更新所有者的本机密码管理器；停用不会抹去历史订单。不要把密码、模型密钥或 OSS AccessKey 放进 Git、截图或日志。

备份、提醒与恢复见[备份与恢复](backup-recovery.md)。
