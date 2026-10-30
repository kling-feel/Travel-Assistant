# 脚本入口

从项目根目录运行命令。`data/generated/` 保存本机私有配置、账号和操作结果，已被 Git 忽略。正式云端使用 `travel-cloud` 项目；运行会写数据库或访问服务商的命令前，先核对目标、备份和费用边界。日常步骤见[部署与运维](../docs/operations.md)。

| 脚本 | 用途 | 边界 |
| --- | --- | --- |
| `create_container_env.py` | 为全新本机隔离项目生成随机凭据 | 只创建忽略文件，拒绝覆盖 |
| `create_cloud_env.py`、`preflight_cloud_env.py` | 全新云环境配置与只读校验 | 不对现有生产环境重建配置；校验不显示密钥 |
| `cloud_compose.py` | 固定 2 GiB 云端 Compose 项目与服务入口 | `init`、`empty-backup` 只用于新空库；`stop` 保留卷 |
| `initialize_container_batch.py` | 容器项目首次迁移和虚构批次导入 | 仅新空库和已验证的空库备份，普通启动不调用 |
| `run_container.py` | 容器应用启动及批次核对 | 只读核对数据库／清单后运行 API |
| `manage_travelers.py` | 创建、轮换、停用授权账号 | 随机密码只写私有文件，不打印 |
| `migrate.py`、`import_network_data.py` | 版本化迁移与虚构数据导入 | 会写库；正式已有订单时不得当作日常重启步骤 |
| `oss_backup.py`、`oss_backup_alert.py` | 加密 OSS 备份及漏备提醒 | 读取服务器受限配置；`check` 只读，`run` 会创建密文对象 |
| `create_oss_backup_config.py` | 生成私有 OSS 模板和最小权限策略 | 只写忽略目录，已有文件拒绝覆盖 |
| `verify_oss_backup_bundle.py` | 检查离线解密包的批次和内部哈希 | 只读，不连接生产数据库 |
| `inspect_container_batch.py`、`inspect_demo_batch.py` | 读取批次与订单摘要 | 不输出旅客密码或对话正文 |
| `usage_report.py` | 汇总服务商调用量 | 实际费用以服务商账单为准 |
| `verify_network_fixture.py`、`verify_network_data.py` | 核对脱敏虚构样本与导入结果 | 只读；数据库目标由运行环境决定 |
| `audit_p26d_secrets.py` | 扫描跟踪文件中的疑似凭据 | 只读，不打印凭据值 |

`build_network_fixture.py`、`build_recommendation_geography.py`、`lock_dependencies.py` 是开发期的数据／依赖生成工具；更改样本或依赖后须复核输出。`batch_environment.py`、`run_p33c2_live.py`、`run_p31h3_metered.py` 与 `create_cloud_2g_env.py` 仍被本机兼容检查或自动化测试引用，不是云端部署入口。

完整离线回归：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -q
```
