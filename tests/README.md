# 自动化测试导航

从项目根目录运行完整回归：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -q
```

| 目录 | 覆盖范围 |
| --- | --- |
| `business/` | 本人订单、航班与产品、报价、确认事务、数据清理 |
| `dialogue/` | 多轮对话、提案、记忆、候选、取消、流式回合 |
| `recommendations/` | 可达城市、地点目录、证据来源与推荐 |
| `interface/` | 页面、中文展示、页面旅程、调用计量 |
| `support/` | 仅供页面测试使用的固定模型与内存应用辅助代码 |
| `operations/` | 加密备份、漏备提醒和恢复包校验 |

每组可用相同的 `unittest discover` 命令单独运行，例如把 `-s tests` 改为 `-s tests/business`。完整回归以根目录命令为准；测试主要使用内存数据库、固定模型和离线样本，不需要正式 PostgreSQL 或付费 API。涉及真实数据库和付费服务的脚本不属于常规测试，见[脚本入口](../scripts/README.md)。
