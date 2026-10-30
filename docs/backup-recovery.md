# 加密备份与隔离恢复

正式站点的 PostgreSQL 数据和演示批次清单由服务器端使用恢复**公钥**加密，再上传至新加坡地域 `ap-southeast-1` 的私有 OSS 桶 `travel-portfolio-backup`，前缀为 `travel-assistant/backups/`。恢复私钥只由项目所有者保存在本机受限文件及密码管理器；服务器、OSS 和 GitHub 均不保存私钥。专用 RAM 上传身份仅能向该前缀写对象并读取对象元数据，不能删除对象或管理桶。

`travel-oss-backup.timer` 每天北京时间约 03:10～03:20 触发；`travel-oss-backup-alert-check.timer` 每小时检查。备份失败或超过 26 小时没有可核验的备份时，邮件提醒所有者。OSS 生命周期规则仅匹配备份前缀：当前版本对象最后修改满 30 天后删除。服务端对象的字节数与 SHA256 元数据在上传后核对。配置与状态位于服务器受限目录 `/etc/travel-assistant/` 与 `/var/lib/travel-assistant-oss-backup/`，不进入仓库。

服务器**只读核对**：

```bash
systemctl status travel-oss-backup.timer travel-oss-backup-alert-check.timer --no-pager
systemctl list-timers 'travel-oss-backup*' --no-pager
sudo /usr/bin/python3 /usr/local/libexec/travel-assistant/oss_backup.py check
journalctl -u travel-oss-backup.service -n 30 --no-pager
```

核对时不重复触发备份或测试邮件。若检查失败，先看定时器、服务日志、OSS 对象元数据和邮件提醒，再定位原因；保留现有密文和正式卷。

恢复演练顺序：选择一份 OSS `.tar.age` 密文下载到所有者本机；比对记录的对象长度与 SHA256；使用本机恢复私钥解密并验证内部 PostgreSQL 自定义格式归档、批次清单与哈希；在**全新隔离 Compose 项目和空卷**中恢复，核对迁移、供给、订单和账号摘要。所有步骤通过前不得对正式卷执行 `pg_restore`，也不得把解密后的临时明文上传或提交。项目已完成一次实际 OSS 取回及全新卷恢复；每日定时触发和连续留存应以运行中的只读记录为准。
