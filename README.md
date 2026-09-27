# A股公告看板 v2

当前 VPS 生产源码仓库，对应 /opt/agu-board-v2。

一键更新：

curl -fsSL https://raw.githubusercontent.com/baibiaowang/vps-server-deploy-board/main/update.sh | sudo bash

更新会先建立代码和 SQLite 备份，然后只替换应用源码。数据库 data、日志 logs、环境变量 agu-board.env、虚拟环境 .venv 均保留。更新完成后自动执行 Python 编译检查、数据库完整性检查、静态资源重建和本地健康检查；失败自动回滚。

关键修复：
1. Eastmoney 分页超过安全页数或中途断页时直接失败，不再静默截断。
2. 手动、定时、补漏、CLI 更新使用统一跨进程锁，禁止采集任务互相重叠。
3. 运行记录的 peak_rss_mb 改为真实进程历史最大 RSS。
4. APScheduler 周编号修正为周一至周五增量、周日全量。
5. docs 和 Cookie Secure 现在由环境变量控制。

