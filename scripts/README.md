# 脚本入口索引

在项目根目录运行脚本；日常启动方法见[项目说明](../README.md)。本索引说明当前文件的用途与数据边界，**列出脚本不代表建议在已有正式演示库直接运行**。历史阶段评估的过程与结果保留在[企业项目文档](../docs/enterprise/当前进度.md)，一次性付费评估程序不再作为当前入口。

脚本文件仍在本目录，调用方式不变；它们使用 `travel_assistant/core/` 中的配置和身份工具、`travel_assistant/db/` 中的数据库模型，以及 `travel_assistant/services/` 中的业务服务；现行旅客接口和受控助手分别位于 `travel_assistant/api/`、`travel_assistant/agents/`。迁移版本仍保留在根目录 `migrations/`。

## 先选对入口

| 需求 | 从哪里开始 | 边界 |
| --- | --- | --- |
| 启动或演示项目 | [项目说明](../README.md) | 日常启动不重建数据库 |
| 检查交付样本与代码 | 下方“当前常用入口”及[自动化测试](../tests/README.md) | 可选择纯离线核验 |
| 升级或重置数据库 | 下方 `migrate.py`、`import_network_data.py` | 先核对目标库和备份；会写数据 |
| 历史旅程或真实模型验收 | 下方“数据库专项核验”“隔离环境专项入口” | 可能写测试记录或产生费用，按独立计划执行 |

## 当前常用入口

| 脚本 | 作用 | 读写与前置条件 |
| --- | --- | --- |
| `init_local_env.py` | 创建本机 `.env` 凭证 | 写 Git 忽略的本机文件；不打印密钥。已有配置先核对，不覆盖或分享 |
| `build_network_fixture.py` | 从获授权原始 SQLite 导出无旅客信息的交付样本 | 只读输入、写指定新输出；拒绝覆盖。日常运行不依赖 `data/source/` |
| `build_recommendation_geography.py` | 从交付样本和时区资料生成城市地理表 | 只读样本，写生成文件；仅在样本变更后使用 |
| `verify_network_fixture.py` | 核对交付样本、两日期排程与可达城市 | 只读 SQLite；无需 PostgreSQL、模型或联网密钥 |
| `batch_environment.py` | 为新演示批次创建独立本机凭证，并用该配置运行指定命令 | 文件只写入 Git 忽略的 `data/generated/`；不会输出凭证，拒绝覆盖已有文件；普通 `run` 关闭付费服务；`run-live` 只允许已验收的 5436 批次在本机 8000 使用现有 DeepSeek 与阿里云配置 |
| `run_p33c2_live.py` | 核对正式演示批次身份后启动本机页面；`--smoke` 限制切换验收的付费调用 | 仅读取 5436 库的日期、迁移与供给摘要；拒绝错误目标；需先完成 P3.3c2 备份、恢复与切换前核对，不执行导入或重置 |
| `create_container_env.py` | 为独立容器项目生成一次性本机凭证 | 只写 Git 忽略的 `data/generated/p34a-container.env`，已有文件拒绝覆盖；不打印密码 |
| `create_cloud_env.py` | 生成独立云端配置模板 | 要求已确定域名、证书邮箱和演示日期；只写 Git 忽略的配置，不复制本机模型密钥，仍需填入云端独立密钥与镜像摘要 |
| `cloud_compose.py` | 统一执行 2 GiB 云端 Compose 的服务清单、构建、首次初始化、账号管理、启停和状态核对 | 固定叠加正式资源限额文件、私有环境文件及指定项目；没有删除数据卷入口。需要已购买服务器、最终域名和私有配置后才可实际启动 |
| `manage_travelers.py` | 云端授权账号的创建、轮换、停用 | 只在隔离的管理容器中使用；一次性口令写私有文件，不打印口令；先停用初始账号再开放页面 |
| `usage_report.py` | 管理员查看模型与搜索调用次数 | 只读取云端用量元数据，不读取消息正文或密钥；实际费用仍以服务商账单为准 |
| `oss_backup.py` | 云端独立数据库密文备份到私有 OSS | 需服务器 root、专用 RAM 凭证和恢复公钥；先校验正式容器与批次，再加密上传，绝不把恢复私钥放入服务器。执行步骤以 P3.4c3c2 操作计划及后续验收记录为准。 |
| `oss_backup_alert.py` | 云端 OSS 备份失败、超时及恢复的邮件提醒 | 需仅管理员可读的 SMTP 授权码配置；测试邮件实际送达后再启用提醒和备份定时器。 |
| `create_oss_backup_config.py` | 为专用桶生成本机私有配置模板和最小前缀 RAM 策略 | 只写 Git 忽略的 `data/generated/`；已有文件拒绝覆盖，凭证由旅客在本机填写，不在聊天传输。 |
| `verify_oss_backup_bundle.py` | 在隔离恢复前核对已解密备份的批次和内部哈希 | 只读旅客提供的私有解密包，不连接正式库；解密包验收后应移除。 |
| `install_oss_backup_tools.py` | 安装固定版本的 age 和 ossutil | 先校验发布方 SHA256；不配置 OSS 凭证，也不启用备份定时任务。 |
| `create_cloud_2g_env.py`／`create_cloud_2g_restore_env.py` | 生成 P3.4b2 独立 2 GiB 限额和恢复演练配置 | 仅写 Git 忽略目录，使用不可调用真实服务的测试密钥；已有文件拒绝覆盖 |
| `monitor_cloud_2g.py`／`verify_cloud_2g.py`／`verify_cloud_2g_restart.py` | 采样隔离容器内存并验收双账号、查询、交易卡和重启 | 只针对本机 8021／8022 与 `travel-p34b-2g`；结果不代表真实香港服务器或付费模型性能 |
| `lock_dependencies.py` | 为云端 Linux 构建固定依赖版本 | 从当前虚拟环境生成 `requirements.lock`；更换依赖后需重建并在全新镜像验证 |
| `initialize_container_batch.py` | 容器项目的一次性数据库迁移与虚构数据导入 | 仅接受 `db:5432`、空库及已验证的空库备份；正常启动不会调用它，失败后不得在非空库重跑 |
| `run_container.py` | 容器化 API 启动入口 | 核对同项目数据库地址、批次日期、迁移和供给摘要；不匹配即拒绝启动，运行中 `/ready` 检查数据库 |
| `inspect_container_batch.py` | 恢复演练的只读摘要 | 仅接受 Compose 内 `db:5432`，输出日期、计数及订单／供给摘要，不输出密码或对话正文 |
| `inspect_demo_batch.py` | 只读摘要指定端口的演示库日期、数量、订单状态与迁移 | 仅输出脱敏计数和摘要，不读取消息正文、密码或令牌；须先核对实际容器和端口 |
| `verify_batch_token_isolation.py` | 验证旧批次签名的短时令牌在新批次返回 401，且新批次账号可以正常登录 | 只在已核对的隔离数据库运行；仅输出状态码，不输出令牌或密码，不创建订单 |
| `migrate.py` | 执行版本化 PostgreSQL 迁移 | **写数据库结构和迁移记录**；宿主机新批次用 `--fresh-batch-port`，容器独立项目用 `--fresh-batch-container`，均在首个结构写入前拒绝错误目标和已有表；升级前备份 |
| `import_network_data.py` | 导入／按新演示日重置全航线模拟数据 | 不带 `--apply` 只预览；宿主机新批次用 `--fresh-batch-port`，容器独立项目用 `--fresh-batch-container`，均要求当前完整迁移和空业务库；带 `--apply --reset-demo --backup` 会**替换演示订单、会话与动作记录**，须先核对目标与备份；不能在演示中静默运行 |
| `verify_network_data.py` | 核对导入后的机场、航线、产品和种子订单 | 只读所连接的 PostgreSQL；先核对目标库 |
| `audit_p31e5_schedule.py` | 全量核对虚构航班时长、到达日及落地酒店覆盖 | 只读 PostgreSQL；正式库需显式 `--formal-read-only`，且先核对目标端口 |
| `audit_p26d_secrets.py` | 检查 Git 中疑似凭证暴露，不打印凭证值 | 只读仓库；发布前可运行，发现疑点需人工核对 |

## 数据库专项核验

这些脚本是历史阶段仍保留的回归入口。它们可能创建临时旅客、会话、卡片或订单，并在完成时清理；**不要在正式演示中随手运行，也不要并行运行**。若脚本分 `prepare`／`finish` 两阶段，中间需按对应操作计划重启隔离数据库，`finish` 成功前不要丢弃 Git 忽略目录内的状态文件。先核对端口、数据库项目、备份及当前已有记录。

| 脚本 | 核验主题 | 数据边界 |
| --- | --- | --- |
| `verify_p1.py` | 基础身份、数据隔离与回滚 | 连接 PostgreSQL 和本机 API；可能写入后回滚或清理 |
| `verify_p2_sessions.py` | 本人会话与访问边界 | 写临时会话并清理 |
| `verify_p2_actions.py` | 交易提案边界 | 写临时动作记录并清理 |
| `verify_p2_confirmations.py` | 逐笔确认与拒绝 | 写临时旅客及交易记录并清理 |
| `verify_p25_flight_booking.py` | 新订机票 | 写临时订单，要求目标库状态符合预期 |
| `verify_p25_rentals.py` | 租车预订与取消 | 分阶段写临时记录，要求数据库重启 |
| `verify_p25_rental_changes.py` | 租车改期 | 分阶段写临时记录，要求数据库重启 |
| `verify_p25_all_actions.py` | 多类动作综合回归 | 分阶段写临时记录，要求数据库重启 |
| `verify_p26a_dialogue.py` | 固定模型的只读对话 | 不调用付费模型；会写入并清理测试会话 |
| `verify_p26a_restart.py` | 对话检查点恢复 | 分阶段写临时会话，要求数据库重启 |
| `verify_p26b_dialogue.py` | 对话产生的交易卡片 | 分阶段写临时动作，要求数据库重启 |
| `verify_city_products.py` | 可达城市产品与 API | 只读 PostgreSQL 和认证 API；先核对目标库 |

## 隔离环境专项入口

以下脚本不属于首次搭建或日常启动。只在其操作计划规定的隔离端口、数据卷、备份和账号条件满足时使用。P3.2b3 已移除被当前自动化或全量审计覆盖的旧专项入口；剩余历史旅程脚本仍待后续升级。

| 脚本 | 作用 | 数据／费用边界 |
| --- | --- | --- |
| `run_p31h3_metered.py` | 在隔离页面运行有计数与费用上限的真实模型／搜索验收 | **可能产生模型和联网费用**，并写隔离库及忽略的本机流水；只允许配置的 5435 数据库与 8014 页面，运行前另行确认预算 |
| `verify_p31e4_journey.py` | 完整旅程与重启的历史隔离验收 | 仅接受旧 5434 隔离库且要求演示日期为当天，会写临时订单、会话与卡片；**暂不作为当前日常命令**，不得改端口后用于正式库 |

普通代码回归使用 `python -m unittest discover -s tests -p 'test_*.py'`，测试主要使用隔离内存数据库和替身，不应连正式库。真实数据库专项脚本的执行方法仍以其独立操作计划为准。
