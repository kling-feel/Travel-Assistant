# 旅行助手（企业项目改造中）

这是一个使用虚构数据的旅行助手求职作品，保留 FastAPI 和 LangGraph 主助手／专门助手结构。**P3.2 目录整理和 P3.3 新演示批次已进入本机 `main`；本机 8000 使用 5436 批次，GitHub 同步状态见[当前进度](docs/enterprise/当前进度.md)。**旅客可以在 `/app` 登录、进行多轮对话、查询本人行程和航班／酒店／租车、读取虚构平台政策，并为九类交易生成待确认卡；旅客须逐笔确认或拒绝，未确认不会修改订单。现有 95 座可达城市、每城 2 个经逐页核查的经典地点。配置阿里云密钥后，联网检索默认开启。游览预订不在最终范围。旧交易接口已移除。

项目文档从[企业项目文档导航](docs/enterprise/README.md)进入；当前方案见 [产品需求](docs/enterprise/产品需求.md)、[软件架构](docs/enterprise/软件架构.md) 和 [当前进度](docs/enterprise/当前进度.md)。当前脚本的用途、数据库写入范围和付费调用条件见[脚本入口索引](scripts/README.md)；已结束阶段的专项验收命令以历史文档保存，不作为日常启动步骤。

当前页面文件位于 `travel_assistant/web/static/`，由根目录 `main.py` 继续以 `/app` 和 `/static/*` 提供；项目根目录的 `.env` 与 `data/fixtures/` 仍是配置和交付样本的位置。目录整理期间的当前模块位置见[软件架构](docs/enterprise/软件架构.md)，日常启动命令不变。

配置、身份与通用 HTTP 处理位于 `travel_assistant/core/`，数据库连接和当前表模型位于 `travel_assistant/db/`，航班、住宿、租车、订单与推荐服务位于 `travel_assistant/services/`，受控助手和旅客接口分别位于 `travel_assistant/agents/`、`travel_assistant/api/`。版本化迁移仍从根目录 `migrations/` 运行；脚本命令保留原路径，测试按职责分组。

## 目录导航

| 位置 | 用途 |
| --- | --- |
| `main.py` | 本机 API 与页面的启动入口 |
| `travel_assistant/core/`、`db/` | 环境配置、身份与数据库结构 |
| `travel_assistant/services/`、`agents/`、`api/` | 业务规则、受控对话与旅客接口 |
| `travel_assistant/web/static/` | 同源旅客页面 |
| `migrations/`、`data/fixtures/` | 版本化迁移与可交付虚构样本 |
| [`scripts/`](scripts/README.md) | 本机准备、数据核验与专项验收命令；先看读写／费用边界 |
| [`tests/`](tests/README.md) | 按业务、对话、推荐、页面分组的自动化回归 |
| [`docs/enterprise/`](docs/enterprise/README.md) | 当前需求、架构、进度与保留的阶段记录 |


P3.1e4 的隔离测试启动、登录与验收步骤见[本机操作指南](docs/enterprise/P3.1e4-验收记录与本机操作指南.md)。跨洲航班排程已按[排程质量计划](docs/enterprise/操作计划/P3.1e5-航班时长与到达日质量修复.md)修复并在正式演示库验收；它仍是虚构展示时间，不能用于真实行程。

## 旅客页面快速体验

首次安装先完成下文“Windows 本机准备”的环境文件、数据库迁移和虚构数据导入。**当前这台机器已经有独立的新演示批次**：启动 Docker Desktop 后，在项目目录的普通 PowerShell 先运行 `docker start travel-date-next-db-1`；确认容器健康，再在另一个保持开启的终端运行：

```powershell
.\.venv\Scripts\python.exe scripts\batch_environment.py run-live data/generated/p33b-next.env -- .\.venv\Scripts\python.exe scripts\run_p33c2_live.py
```

然后打开 `http://127.0.0.1:8000/app`，使用本机 Git 忽略文件 `data/generated/p33b-next.env` 中的 `DEMO_TRAVELER_1_PASSWORD` 或 `DEMO_TRAVELER_2_PASSWORD` 分别登录 `demo1`、`demo2`；不要将密码发到聊天或提交到仓库。页面和 API 共用本机 8000，联网检索默认开启。此启动器先核对连接确为 5436、批次日为 2026-10-24、迁移与供给摘要正确；若不符会拒绝启动。旧 `.env` 仍对应保留的旧批次，因此**当前演示不要用 `main.py` 或无项目名的 `docker compose up` 替代上述命令**。新机器尚无这个独立数据卷和忽略凭证文件时，按下文首次安装流程准备，不能直接使用本机批次命令。健康检查 `/health` 应正常，页面打不开时检查终端是否仍在运行及 8000 端口是否被占用。

1. 在登录页输入示例账号 `demo1` 或 `demo2`。当前新批次的密码取自本机 `data/generated/p33b-next.env` 的 `DEMO_TRAVELER_1_PASSWORD` 或 `DEMO_TRAVELER_2_PASSWORD`；首次安装并使用默认数据库时才取自 `.env`。不要把密码或模型密钥发到聊天、截图或代码仓库。登录后页面会读取数据库里的可搜索日期。
2. 在“查询候选”输入中文城市或机场名称，从建议中选定具体机场，再选日期查询航班、酒店或车辆。页面初始给出汉堡至巴塞尔的示例条件；同城多机场需选具体机场。页面只读查询不会建订单，金额按 `¥` 显示，航班号和三字码在次级信息中供核对。查询后回到“对话助手”，说明所选班次／产品及日期；若有多个候选或条件不足，助手会追问。若直接使用 API，机场参数仍为 `HAM`、`BSL` 等代码，酒店城市参数仍为 `Basel` 等数据库值；可用已登录的 `GET /api/v1/locations` 查看映射。
3. 在“对话助手”新建会话，询问行程、虚构规则或经典地点，也可提出预订、改签、改期、取消。每张待确认卡显示变更前后、示例金额和有效期；订单号、规则 ID、版本及完整字段在详情中核对。点“确认”或“拒绝”后，页面先重新读取卡片，再弹出详细核对框；只有在框内确认该动作时才会修改对应的示例订单。双笔需求须逐笔处理。随后在“我的行程”和“操作记录”核对结果。
4. 同一浏览器标签页刷新后会验证短期登录状态并恢复所选会话；令牌过期或主动退出后需重新登录，关闭标签页也会结束该标签页的登录状态。密码不会保存在页面存储中；已保存的会话、卡片和订单仍从数据库读取。切换账号先点“退出登录”。若模型未配置，对话返回服务不可用，但登录、查询和已保存的记录仍可用。地点推荐使用已核查目录；已配置阿里云密钥时会尝试联网搜索，未经核实的网页内容不进入推荐事实。

P3.1h1 之后，在“对话助手”按发送，旅客原文立即显示，助手先显示实际处理状态与等待时间。答复核验并保存后才逐段出现，来源／候选／待确认卡随后从服务器刷新；这不是模型原始 token 直接输出。断网时可用“核对本轮”查看原请求状态，失败时用“重试本轮”继续；同一标签页刷新后会验证登录状态并从当前会话恢复。若卡片暂未显示，先等答复完成，不要重复点发送；订单只会在卡片内由旅客逐笔确认后改变。已有 JSON 回合 API 不变，页面使用 `POST /api/v1/sessions/{id}/turns/stream`；流内 `failed` 事件也表示本轮失败，即使连接最初返回 HTTP 200。境外天气暂无法核实，中国城市原有天气核验保留。

按本节命令在前台启动时，结束演示先在运行 API 的终端按 `Ctrl+C`，再执行 `docker stop travel-date-next-db-1`。若页面由后台进程启动，先核对监听 8000 的进程确为本项目启动器，再停止该进程；不要仅凭端口号终止来源不明的服务。新旧批次及恢复演练的数据卷都保留。不要运行 `down -v`，它会删除数据卷。若本机普通终端能运行 Docker、但某个受限工作台显示找不到 `docker` 或连不上 WSL，先在新开的 PowerShell 运行 `docker --version`、`docker info` 核对 Docker Desktop 引擎；这类差异可能来自该工作台的权限或 PATH，不能据此判断数据库故障。

## 独立容器部署验收（P3.4a）

计划中的云端展示使用另一个 Compose 文件和新数据卷；服务器、域名与公网验收顺序见[云端展示操作指南](docs/enterprise/云端展示操作指南.md)。当前尚未购买或部署，下面仍是本机隔离验收操作。

本节只使用 `travel-p34a` 独立项目，页面绑定本机 `127.0.0.1:8018`，数据库**不映射宿主机端口**。现有 8000→5436 演示入口、旧 5433 批次及其数据卷不会被这些命令替换。第一次运行须已安装并启动 Docker Desktop，且 8018 端口空闲；在项目根目录的普通 PowerShell **逐行**执行：

```powershell
.\.venv\Scripts\python.exe scripts\create_container_env.py --as-of 2026-10-25
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a build app init
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a up -d --wait db
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a --profile setup run --rm backup
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a --profile setup run --rm init
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a up -d --wait app
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a ps
```

第一行只创建 Git 忽略的独立密码文件，已有文件时拒绝覆盖。`backup` 为迁移前空库做 PostgreSQL 自定义格式备份；`init` 仅在空库和备份均经核对后执行当前完整迁移并导入虚构样本，写出批次摘要；普通 `up` 和容器重启不会执行这两步。**任何一行失败就停止，不在非空库上重跑 `init` 或 `pg_restore`。** 如需新的演示日期，按[演示批次流程](docs/enterprise/操作计划/P3.3-演示日期维护与部署分步路线.md)另建独立项目与卷，不修改此批次订单日期。

成功后打开 `http://127.0.0.1:8018/app`。示例账号 `demo1`、`demo2` 的密码在本机 `data/generated/p34a-container.env`，不要发送到聊天或提交。容器验收默认关闭付费模型和联网检索；登录、查询、已保存的订单与确认卡仍可用。`/health` 表示 API 进程存活，`/ready` 才检查数据库已有演示批次。检查、停止与再启动：

```powershell
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a ps
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a stop app db
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a up -d --wait app
```

写入验收订单后，如需做独立备份与恢复演练，先在本机 Git 忽略目录选择**尚不存在**的备份文件名；以下示例不得覆盖已有备份。备份目录检查通过后，把文件恢复到另一个**全新项目**的数据卷，比较两库的脱敏摘要。操作前先核对 `travel-p34a-restore` 项目尚无容器或卷；恢复库使用同一独立环境文件，仅用于本机核验：

```powershell
if (Test-Path -LiteralPath data/generated/p34a-after-actions.dump) { throw '备份文件已存在，请先停止并核对' }
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a exec -T db pg_dump -U travel_app -d travel_demo -Fc -f /tmp/p34a-after-actions.dump
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a exec -T db pg_restore -l /tmp/p34a-after-actions.dump
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a cp db:/tmp/p34a-after-actions.dump data/generated/p34a-after-actions.dump
Get-FileHash data/generated/p34a-after-actions.dump -Algorithm SHA256
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a --profile setup run --rm init python scripts/inspect_container_batch.py
$priorRestore = docker volume ls --filter label=com.docker.compose.project=travel-p34a-restore --format '{{.Name}}'
if ($priorRestore) { throw '恢复演练项目已有数据卷，请先停止并核对' }
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a-restore up -d --wait db
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a-restore cp data/generated/p34a-after-actions.dump db:/tmp/p34a-after-actions.dump
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a-restore exec -T db pg_restore -U travel_app -d travel_demo --no-owner --no-privileges --exit-on-error /tmp/p34a-after-actions.dump
docker compose -f compose.container.yaml --env-file data/generated/p34a-container.env -p travel-p34a-restore --profile setup run --rm init python scripts/inspect_container_batch.py
```

两个摘要中的演示日期、迁移版本、各表计数、订单与供给摘要应一致。验收后用各自项目名执行 `stop app db` 或 `stop db`，保留所有卷和备份；**不要运行 `down -v`**。`restart: unless-stopped` 可处理部分容器退出和 Docker 引擎重启，不能覆盖 Docker Desktop 未启动、宿主机故障或数据损坏。镜像只包含程序、可信地点目录与脱敏虚构航线样本，原始 SQLite、`.env`、生成数据和备份均由 `.dockerignore` 排除。具体门槛和失败处理见[P3.4a 操作计划](docs/enterprise/操作计划/P3.4a-本机容器化与恢复验收.md)。

## 数据从哪里来

默认使用 [完整航线样本](data/fixtures/network_source.sqlite)：它只含 115 个机场和 33,121 条虚构航班，其中 15,383 条 `Scheduled` 覆盖 618 条航线；没有旅客、票号、预订或原酒店/租车记录。展示航班号为 `SIM-<航班ID>`，机场坐标和时区随样本提供。两个演示账号与机票由脚本合成；P2.4c 根据 104 个可达机场生成 312 个酒店/房型和 208 个租车产品，覆盖 95 个城市，不导入原始旅客记录。原有 `travel_new.sqlite` 和 `travel2.sqlite` 保存在 Git 忽略的 `data/source/`，仅供可选的样本再生成；运行与部署不读取它们，也不需要放入 Docker 镜像。


可用以下命令核对新样本及两个演示日期：

```powershell
.\.venv\Scripts\python.exe scripts\verify_network_fixture.py --as-of 2026-10-17 --as-of 2026-11-01
```

校验器以汉堡—巴塞尔的下午班次为锚，将全部计划/实际时间按同一个绝对时间差平移到演示日附近，并按各起飞机场时区计算未来约 30 天可订的 Scheduled 班次；样本本身保存原始时刻。源文件统一的 `-04:00` 偏移不代表各机场当地时间。若拥有获授权的原虚构库，可用 `scripts/build_network_fixture.py --source <原库路径> --output <新的输出路径>` 重新生成；脚本只读原库并拒绝覆盖已有文件。


完整航线导入时，只为演示窗口内的 `Scheduled` 班次按稳定算法生成**模拟报价**（以 2026-10-17 为演示日有 15,374 个）；算法 ID 为 `network-fare-v1`，不会读取真实航司价格或让模型定价。改签卡用候选报价减去原订单金额，只展示应补或应退差额；取消卡按原订单金额的 30% 计算模拟取消费、70% 计算模拟应退金额。旅客页面按人民币样式（例如 `¥4,258`）呈现既有整数示例报价，不代表真实市场价格或汇率，也不会发生真实支付或退款；API 保留模拟数据声明。

源 SQLite 的部分跨洲班次时长明显过短。当前完整航线导入使用 `fictional-flight-time-v1`：机场坐标大圆距离加上 5% 路程余量，按每小时 820 公里和 35 分钟准备时间计算，向上取整到 5 分钟，最短 45 分钟、最长 24 小时。航班号、出发时间和示例报价保持不变；计划到达时间及保留延误偏差后的实际到达时间由导入程序重新计算。这是确定性的虚构展示排程，不能用来判断真实航班。旅客页显示机场当地出发与到达时间及 UTC 偏移，落地酒店按到达机场当地日期查询。至少一种机场周边酒店房型可覆盖可订窗口末端落地后连续两晚；酒店查询退房日上限为演示基准日后第 34 日，租车日期窗不变。本机正式库已于 2026-10-22 经备份后显式重建；今后日期窗过期时仍须重新备份并更新。详情见[当前进度](docs/enterprise/当前进度.md)和[正式库验收记录](docs/enterprise/P3.1e6-正式库排程更新验收记录.md)。

P2.4c 的酒店和租车产品由 [生成规则](travel_assistant/services/city_products.py) 根据可达机场坐标、代码和演示日期产生；相同样本和日期会得到相同 ID 与价格。当前房型可用日期窗已扩展，生成规则为 `reachable-products-v2`，价格种子仍沿用旧版。巴塞尔 `room-1`～`room-3` 保留，以便两张演示酒店订单继续关联。产品名称、价格及坐标均为**模拟值**，不是供应商信息；坐标只支持近似直线距离，不代表道路距离或通行时间，也没有真实剩余库存。至少一个酒店选项覆盖可订航班最晚落地日后的两晚；无产品时搜索返回空列表。每次重新平移演示日期，需要先备份，再通过显式重置命令同时重建航班、报价、酒店、租车和政策；普通服务重启不会重建。若存在待确认动作，重置会拒绝执行。

平台规则见 [虚构政策知识](data/policies/fictional_platform.md)。`GET /api/v1/policies/{scope}` 的 `scope` 为 `flight`、`hotel` 或 `rental`，默认只返回当前规则 ID/版本；旧规则保留用于历史审计。机票新预订按当前模拟报价核算，取消模拟收费 30%；酒店在产品机场当地入住日期前取消，手续费为 0、模拟应退原订单总额；入住当日起不可取消，同日仍可预订或改期。租车按取还车的实际经过时间每开始 24 小时计一个模拟日；原取车前可保持车辆不变修改取还车时间，按新旧总价差额模拟补退且无改期费，也可取消并模拟全额应退；到达原取车时刻后不得改期或取消。内部调用 `create_proposal(db, traveler_id, session_id, "rental_change", {"order_id": "本人订单ID", "expected_version": 1, "pickup_at": "机场当地ISO时间", "dropoff_at": "机场当地ISO时间"})` 创建动作卡，旅客随后通过现有 `/api/v1/sessions/{session_id}/actions/{action_id}/confirm` 逐笔确认。旧 `order_faq.md` 是未使用的历史素材，不作为本平台交易依据。

## Windows 本机准备

1. 安装 Python 3.11。安装 [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/) 并启动。Docker 的 WSL 2 后端可能需要先在**管理员 PowerShell** 中运行 `wsl --install` 并重启电脑；按官方安装向导操作。
2. 在项目目录打开 PowerShell，依次运行：

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe scripts\init_local_env.py
   docker compose up -d --wait db
   .\.venv\Scripts\python.exe scripts\migrate.py
   New-Item -ItemType Directory -Force data/generated | Out-Null
   $backupName = "travel-before-$(Get-Date -Format yyyyMMdd-HHmmss).dump"
   docker compose exec -T db pg_dump -U travel_app -d travel_demo -Fc -f "/tmp/$backupName"
   docker compose cp "db:/tmp/$backupName" "data/generated/$backupName"
   docker compose exec -T db pg_restore -l "/tmp/$backupName" | Select-Object -Last 5
   .\.venv\Scripts\python.exe scripts\import_network_data.py --as-of (Get-Date -Format yyyy-MM-dd)
   .\.venv\Scripts\python.exe scripts\import_network_data.py --as-of (Get-Date -Format yyyy-MM-dd) --apply --reset-demo --backup "data/generated/$backupName"
   .\.venv\Scripts\python.exe scripts\verify_network_data.py
   .\.venv\Scripts\python.exe scripts\verify_p1.py
   .\.venv\Scripts\python.exe scripts\verify_p2_sessions.py
   .\.venv\Scripts\python.exe scripts\verify_p2_actions.py
   .\.venv\Scripts\python.exe scripts\verify_p2_confirmations.py
   .\.venv\Scripts\python.exe scripts\verify_p25_flight_booking.py
   .\.venv\Scripts\python.exe scripts\verify_p25_rentals.py --phase prepare
   docker compose restart db
   .\.venv\Scripts\python.exe scripts\verify_p25_rentals.py --phase finish
   .\.venv\Scripts\python.exe scripts\verify_p25_rental_changes.py --phase prepare
   docker compose restart db
   .\.venv\Scripts\python.exe scripts\verify_p25_rental_changes.py --phase finish
   .\.venv\Scripts\python.exe scripts\verify_p25_all_actions.py --phase prepare
   docker compose restart db
   .\.venv\Scripts\python.exe scripts\verify_p25_all_actions.py --phase finish
   .\.venv\Scripts\python.exe scripts\verify_p26a_dialogue.py
   .\.venv\Scripts\python.exe scripts\verify_p26a_restart.py prepare
   docker compose restart db
   .\.venv\Scripts\python.exe scripts\verify_p26a_restart.py finish
   .\.venv\Scripts\python.exe scripts\verify_p26b_dialogue.py prepare
   docker compose restart db
   .\.venv\Scripts\python.exe scripts\verify_p26b_dialogue.py finish
   ```

   `.env` 会自动生成随机的本机数据库密码、两名演示账号密码和 JWT 密钥，且已被 Git 忽略。命令中的 `pg_dump` 在首次使用时备份空库，在后续重置时备份旧演示数据；`pg_restore -l` 用于检查备份目录可读。请确认备份文件存在且有内容，再运行带 `--apply --reset-demo` 的命令。备份可能含密码哈希和会话，也已被 Git 忽略，不要分享。不要把 `.env`、模型 API 密钥或原始大型 SQLite 文件上传到公开仓库。

3. 查看导入结果：

   ```powershell
   docker compose exec -T db psql -U travel_app -d travel_demo -c "SELECT count(*) FROM flights"
   docker compose exec -T db psql -U travel_app -d travel_demo -c "SELECT count(*) FROM flight_offers"
   docker compose exec -T db psql -U travel_app -d travel_demo -c "SELECT as_of FROM demo_state"
   ```

   前两条计数在该全航线样本下预期分别为 33,121 和 15,374；第三条是本次重置的演示基准日。`scripts/migrate.py` 同时初始化 LangGraph 的 PostgreSQL 检查点表；升级现有环境时先备份，再重新执行迁移。`verify_network_data.py` 核对机场、航线、报价、时区、酒店/租车覆盖和演示订单；`verify_p1.py` 验证账号隔离与写服务回滚；后续脚本依次验证会话和交易，**不要并行运行这些会修改测试记录的脚本**。租车、八类动作总回归与 P2.6a 数据库重启验收均分两步：`prepare` 留下临时记录，重启数据库后 `finish` 读取并清理。状态文件在 Git 忽略的 `data/generated/` 中；旧交易脚本的状态可能含临时测试密码，勿分享。若中途失败，应先检查并完成清理。以 2026-10-17 为演示日，两班次在次日汉堡当地 17:43 和 17:58。每次开始新的演示或日期变化时，先重新备份并核对现有数据，再显式运行 `import_network_data.py --as-of <新日期> --apply --reset-demo --backup <备份路径>`；它会替换两个演示账号的订单，**清除旧会话、图检查点、轮次、动作、结果与审计以及租车订单**。演示期间不要执行重置。正常启动、停止 API 或数据库不会清除记录。迁移可以重复运行，已应用版本会跳过；Docker 命名卷保留 PostgreSQL 数据，`docker compose down` 不会清除该卷。

   **当前本机批次不要执行上述 `--reset-demo`**。今后刷新演示日期应另建隔离批次，先备份并保留旧会话与订单，再经独立验收后切换入口；详见[演示日期维护路线](docs/enterprise/操作计划/P3.3-演示日期维护与部署分步路线.md)。

4. 首次安装并使用默认 `.env` 批次时启动 API（上方当前本机 5436 批次使用专用命令）：

   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

   浏览 `http://127.0.0.1:8000/health` 应得到 `{"status":"ok"}`，`/docs` 可打开接口文档。用 `.env` 中的 `DEMO_TRAVELER_1_PASSWORD` 和 `DEMO_TRAVELER_2_PASSWORD` 分别登录账号 `demo1`、`demo2`。可在本机 PowerShell 中查看密码：`Select-String -Path .env -Pattern '^DEMO_TRAVELER_[12]_PASSWORD='`；这些是本机演示密码，不要复制到公开仓库或直播画面。

   在 `/docs` 中调用 `POST /api/v1/login`，请求体为 `{"username":"demo1","password":"从 .env 读取的密码"}`。复制响应中的 `access_token`，点页面右上方 **Authorize**，粘贴令牌。随后调用 `GET /api/v1/me`、`GET /api/v1/me/itinerary`；切换到 `demo2` 能看到另一名旅客的订单。航班、酒店、租车产品与规则查询分别为 `GET /api/v1/flights`、`/api/v1/hotels`、`/api/v1/rentals`、`/api/v1/policies/{scope}`。例如以 2026-10-18 为演示基准日，在次日查询 `GET /api/v1/hotels?city=Basel&check_in=2026-10-19&check_out=2026-10-20&airport=BSL&sort=distance&limit=3`，以及 `GET /api/v1/rentals?airport=BSL&pickup_date=2026-10-19&drop_off_date=2026-10-20`；若演示基准日不同，将日期同步改成数据库 `demo_state.as_of` 的次日。酒店与租车支持 `limit`、`offset`、`sort=price|price_desc|distance`，其中 `price_desc` 按有效产品单价从高到低；租车另支持 `city` 与 `max_daily_price`。距离排序须指定机场，超出演示日期窗返回空列表。对话中的“最贵／最便宜”会在完整地点和日期条件下比较全部有效产品；价格并列时请旅客指定产品。旧用户管理和图交易接口已不注册。

   会话接口：调用 `POST /api/v1/sessions`，请求体填 `{}`，从结果复制 `id`。模型配置完成后，调用 `POST /api/v1/sessions/{id}/turns`，请求体例如 `{"request_key":"demo-turn-001","content":"机票取消政策是什么？"}`；每个新问题换一个 `request_key`，网络重试同一问题沿用原值。响应的 `answer` 是助手答复，`references` 给出政策规则 ID/版本或候选 ID，`status=completed` 表示该轮已保存。继续用同一会话 ID 和新的请求键提问，就能多轮对话；`GET /api/v1/sessions/{id}` 按顺序读取双方消息，`GET /api/v1/sessions` 列出自己的会话。另一个演示账号访问该 `id` 会得到 404。`POST /messages` 仍可用于尚未开始图对话的旧式手工旅客消息；会话进入 `/turns` 后它返回 409，避免两套历史分叉。请求不能指定旅客 ID、图线程 ID 或伪造助手角色。

   动作接口：`GET /api/v1/sessions/{id}/actions` 列出本人卡片，`GET .../actions/{action_id}` 看详情；`POST .../actions/{action_id}/confirm` 确认一张卡，`POST .../actions/{action_id}/reject` 拒绝一张卡；`GET /api/v1/me/action-history` 查看本人操作事件。确认和拒绝请求无需内容，若在 `/docs` 填请求体，只填 `{}`；旅客 ID、订单、金额等覆盖字段会被拒绝。成功确认返回最新订单与模拟金额；同一动作再次确认返回相同结果，不重复写入。已拒绝、已过期或内容变化返回 409，非本人动作返回 404。航班新预订卡引用正式班次和模拟报价，确认前不新增机票；同一旅客同一班次只保留一张有效票，取消后可重订。租车卡须有产品 ID 和取还车时刻（例如巴塞尔机场当地 `2026-10-19T12:00:00+02:00`），按产品机场的 IANA 时区校验并以 UTC 保存；夏令时重复时刻要带正确偏移，不存在的当地时刻会拒绝。卡片显示当地时刻、日价、计费日数及模拟金额，取消卡显示原额全退；`GET /api/v1/me/itinerary` 包含本人租车订单。

   对话生成卡片：在同一会话用 `/turns` 查询本人行程和模拟候选，明确具体班次、酒店房型或车辆以及所需日期。旅客可以说“把我的机票改签到第二班，并订第一家酒店，2026-10-19 入住一晚”；演示日期应换成当前 `demo_state.as_of` 后的可用日期。若候选或时间仍不明确，响应只有追问和 `actions: []`；补齐信息后用**新的** `request_key` 再发一轮。条件齐备时 `/turns` 返回一到两张 `actions` 待确认卡，各有自己的动作 ID、模拟金额和有效期；此时订单没有变化。先通过 `GET .../actions/{action_id}` 核对最新状态，再对每张卡分别调用 `/confirm` 或 `/reject`。最后调用 `GET /api/v1/me/itinerary` 核对订单。重试同一轮时沿用原 `request_key` 和完全相同的 `content`，返回保存的卡片快照，不会重复建卡；快照可能已过期或被处理，以动作详情为准。两笔需求有一笔不明确时两张卡都不创建；卡片没有真实支付或供应商交易。P2.6e 已修复 P2.6d 发现的高影响对话边界问题，旅客仍须逐张核对确认卡。

### 从页面候选生成待确认卡

登录 `/app` 后可在“对话助手”询问航班、酒店、租车，也可在“查询候选”填写地点和日期。每项候选都有“选择并生成待确认卡”；租车还须输入当地取车、还车时刻。选择后页面会切回对话区，在右侧或窄屏下方显示卡片。先查看对象、出发／入住／取还时间、参考总价、规则与有效期，再点击“确认”或“拒绝”。**选择候选只创建卡片；点击卡片中的最终确认按钮后才修改本人示例订单**。确认后到“我的行程”看有效安排；取消后的订单进入可展开的“订单历史”。查询页报错“候选条件、报价或可售状态已变化”时，重新查询并选择，不要根据旧页面金额继续确认。

API 使用者可先通过对应 `GET /flights`、`GET /hotels` 或 `GET /rentals` 取得候选，再调用 `POST /api/v1/sessions/{id}/candidate-proposals`。例如航班请求体含 `kind=flight`、整数 `candidate_id`、所见 `unit_price` 与 `offer_version`、`departure`、`arrival` 和 `travel_date`；酒店含 `kind=hotel`、房型 `candidate_id`、所见每晚 `unit_price`、`city`、`check_in`、`check_out`，可附 `airport`；租车含 `kind=rental`、车辆 `candidate_id`、所见每日 `unit_price`、`airport` 或 `city`、`pickup_date`、`drop_off_date`，以及 `pickup_clock`、`dropoff_clock`，时刻格式为 `HH:MM`。服务端重新核对这些条件并返回卡片；鉴权、逐笔确认和拒绝仍沿用上文动作接口。`GET /api/v1/sessions/{id}/references` 返回最近已完成对话轮次的候选，供页面刷新恢复；只允许本人访问。

## 模型配置

健康检查及正式查询/确认 API 无需模型密钥。`/turns` 需要编辑 Git 忽略的 `.env`，设置 `AI_PROVIDER=deepseek`、`AI_MODEL=deepseek-flash`、`AI_BASE_URL=https://api.deepseek.com`、`AI_API_KEY=<你的密钥>`；保存后重启 API，再按上一节通过 `/docs` 测试。不要在聊天、截图或 Git 提交中展示密钥。`AI_PROVIDER` 也允许 `gpt` 或 `local`；本地兼容服务可省略 API 密钥，但仍需模型名和地址。P2.6e 已通过受限真实 DeepSeek 定向复核；详情见《当前进度》。缺少模型配置时 `/turns` 返回 503，其他 API 仍可用。

**经典地点推荐**：旅客可在同一会话询问“推荐上海的经典地点”“豫园适合文化游吗”或“推荐我行程目的地的景点”。服务器先从当前有模拟航班报价的可达城市和本人已确认行程确定范围。[第二版地点目录](data/fixtures/reviewed_places_v2.psv) 含 95 座可达城市、每城 2 个真实地点；[第一版目录](data/fixtures/reviewed_places_v1.psv) 保留用于核对与回滚。[中文展示审查表](data/fixtures/reviewed_place_display_v1.psv)按地点 ID 提供中文名称、主题和名称证据，正文与页面引用优先显示中文名并附原名。API 的 `references` 保留原 `place`、来源 URL 和审查日期，另增 `display_name`。不配置搜索密钥也能给出地点的简短介绍。地点资料审查超过 180 天会停止用于推荐；无匹配主题或不可达城市会明确降级。地点目录不包含实时票价、营业时间或可核实的酒店距离；询问这些信息时明确提示无法核实。

阿里云 CleverSee 国际版 `GlobalBasic` 已有适配器，但只用于候选发现或新鲜度检查：回答仍只会使用人工核查目录中的地点事实，搜索失败会回退目录。**已配置阿里云密钥时，联网搜索默认开启**；如需停止付费搜索，在本机 `.env` 设置 `ALIYUN_IQS_ENABLED=false` 并重启 API。未配置密钥时仍从人工目录提供可核实的地点。

**联网天气**：在真实对话中可问“明天上海天气怎么样”。助手使用 `GlobalBasic` 搜索公开城市和日期，再重新读取国家气象中心页面；只有城市、目标日期与发布时间均可核对时才给出预报，API 引用保留官方页面。当前仅覆盖北京、成都、广州、昆明、上海未来七天内的天气；搜索或官方页面不可用时会说明未能核实。天气预报会变化，机票和酒店仍只查询本项目的虚构数据库。中文城市、多机场航班和“今天／明天／后天／大后天”已支持跨轮查询；若演示数据库日期窗未跟上真实日期，助手会明确提示可查范围，数据库整体平移仍须先备份并显式操作。

1. 在阿里云控制台确认 CleverSee 已开通、`GlobalBasic` 可用以及本账号的计费或试用额度，创建专用于本项目的 **HTTP API Key**。公开计费说明目前未给出该引擎的单价；请以控制台或阿里云支持给出的价格为准，勿套用 `GlobalAdvanced` 的价格。
2. 在项目根目录的本机 `.env` 配置 `ALIYUN_IQS_API_KEY=<你的密钥>`，保留原有 DeepSeek 和数据库配置。本机当前已配置该密钥；不要重复粘贴或把密钥发到聊天、日志或 GitHub。该文件已被 Git 忽略。
3. 本机已有密钥时不必另外设置开启开关；显式设置 `ALIYUN_IQS_ENABLED=true` 也可。P2.7e 隔离抽查 18 次发现实时结果可利用匹配较少，仍有泛化、打不开的页面；所以人工目录仍是唯一地点事实来源，未核实的实时结果不进入答案。`GlobalBasic` 单价在控制台尚未显示，实际费用以服务商账单为准。

开启后，符合条件的地点推荐每轮至多一次阿里云搜索；重复发送同一 `request_key` 和完全相同的内容会读取保存结果。请求不发送旅客账号、订单、酒店或完整对话。外部网页文字不参与交易确认或虚构政策；游览不能预订、改期或取消。目录来源和模型抽测结果见《当前进度》、[地点来源核查记录](docs/enterprise/P2.7d2-地点来源核查记录.md)、[地点中文展示审查记录](docs/enterprise/地点中文展示审查记录.md)与 [整体验收记录](docs/enterprise/P2.7e-推荐整体验收记录.md)，实际扣费以服务商账单为准。

## 当前限制

P2.6d 在独立隔离数据库发现待确认卡撤回、短序号选班及真实航司政策分流的问题；P2.6e 已修复并以真实 DeepSeek 定向复测。当前仍不承诺公网生产安全或真实供应商交易。模型的口头“同意”不会确认订单；始终通过动作详情核对对象和金额，再由旅客调用对应确认接口。P2.7e 已验收 95 座可达城市的人工目录推荐；实时搜索有密钥时默认尝试，未核实结果不作为事实。演示日期更新和云部署须另行规划。

评估详情在 [当前进度](docs/enterprise/当前进度.md)和 [安全审查](security_best_practices_report.md)。本机 `data/generated/p26d-budget-ledger.json` 与 `p26d-*-report.json` 是 Git 忽略的测试记录，只供本机复核；不要提交或分享可能包含临时账号、会话 ID 的原始报告。P2.6d 隔离数据库项目 `travel-eval` 已停止；默认正式演示数据库已按 P3.1e6 更新并保留卷。后续任何测试、重置或恢复都先按相应操作计划核对端口、容器项目与备份，避免误改正式演示数据。

- 逐笔确认、事务写入和审计已建立；受控对话可生成九类待确认卡。P2.6e 已定向修复短序号、撤回卡、口头同意与真实航司政策分流。`GlobalBasic` 联网适配器已实现，有密钥时默认尝试联网；人工核查目录仍是 95 座可达城市推荐的事实基线。P3.1d 的天气核验范围有限，复杂自由对话的答复仍以受控事实模板为主。
- 旧代码的第三方密钥曾出现在源码和本地私有配置中。源码引用已移除；若这些密钥仍有效，所有者需要到相应服务后台轮换。
