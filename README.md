# VPS 服务器部署看板

这是独立于 `baibiaowang/agu-board` 的 VPS 部署仓库。

## 目标

把 A 股公告看板做成可重复部署的 VPS 安装包：新服务器执行一条命令，自动从 GitHub 下载已校验的发布包、安装依赖、导入历史数据库种子并启动服务。

## 一键部署

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/baibiaowang/vps-server-deploy-board/main/install-from-github.sh)
```

支持 root 和普通用户执行：root 直接运行；普通用户由脚本调用 sudo。

也可以先设置密码：

```bash
BOARD_PASSWORD='你的强密码' bash <(curl -fsSL https://raw.githubusercontent.com/baibiaowang/vps-server-deploy-board/main/install-from-github.sh)
```

## 历史数据库

发布包中的 `data/board.db` 是历史数据种子。第一次部署到全新 VPS 时，自动复制到 `/opt/agu-board-v2/data/board.db`。

以后重新安装和更新都不会覆盖服务器已有数据库、日志或 `agu-board.env`。

## 更新

```bash
sudo /opt/agu-board-v2/update.sh
```

更新过程：停止服务 → SQLite 一致性备份 → 下载最新发布包 → SHA-256 校验 → 只替换程序文件 → 执行数据库迁移 → 启动并检查服务。

更新不依赖 `/opt/agu-board-v2/.git`。

## 备份

```bash
sudo /opt/agu-board-v2/backup.sh
```

默认备份到 `/opt/agu-board-backups/`。备份前会停止服务、执行 `wal_checkpoint(TRUNCATE)` 和 `integrity_check`，再复制数据库主文件。

## 安全

生产环境强制开启 Cookie 登录；`data_list.js`、K 线、健康检查和静态资源接口均受登录态保护。FastAPI 文档默认关闭；服务使用专用系统用户 `agu-board`，并启用 systemd 权限限制。

## 并发

更新任务使用跨进程 `flock` 文件锁，手动任务、定时任务和直接 CLI 不能同时启动多个更新。K 线优先使用数据源的批量并发接口。

## 发布机制

仓库通过 `manifest.json` 指定当前发布包文件名与 SHA-256。应用代码与历史数据库随版本发布包统一更新，避免“入口脚本”和“压缩包内部脚本”漂移。

## 仓库关系

- `baibiaowang/agu-board`：主项目开发仓库。
- `baibiaowang/vps-server-deploy-board`：VPS 初始化部署、历史数据种子、更新与备份工具。

二者独立维护。
