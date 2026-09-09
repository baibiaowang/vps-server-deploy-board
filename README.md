# VPS 服务器部署看板

这是独立于 `baibiaowang/agu-board` 的 VPS 部署仓库。

## 作用

用于在新的 Linux/VPS 服务器上快速部署 A 股公告看板，并保留历史数据库作为首次部署的数据种子。

## 一键部署

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/baibiaowang/vps-server-deploy-board/main/install-from-github.sh)
```

也可以提前设置登录密码：

```bash
BOARD_PASSWORD='你的登录密码' bash <(curl -fsSL https://raw.githubusercontent.com/baibiaowang/vps-server-deploy-board/main/install-from-github.sh)
```

## 数据策略

历史数据库不是普通缓存，而是首次部署的数据种子。首次安装时将它导入 `/opt/agu-board-v2/data/board.db`；以后安装或更新不得覆盖服务器已有数据库。

历史种子以 `data/board.db.gz` 压缩分发，部署时自动解压。

服务器运行产生的 `board.db`、WAL/SHM、日志、密码和密钥均属于服务器本地数据，不进入版本更新覆盖范围。

## 更新

```bash
sudo /opt/agu-board-v2/update.sh
```

更新代码、依赖和 systemd 配置，但保留数据库和本地环境文件。

## 备份

```bash
sudo /opt/agu-board-v2/backup.sh
```

默认备份到 `/opt/agu-board-backups/`。

## 与主项目的关系

- `agu-board`：主项目开发仓库。
- `vps-server-deploy-board`：VPS 初始化部署、历史数据种子、更新和备份工具。

二者独立维护，不互相覆盖。

## 发布包

完整应用源码和历史数据库种子以 `agu-board-v2-1.0.0.tar.gz` 为发布包。部署入口会优先使用仓库内的发布包；也支持通过 `PACKAGE_URL` 指定外部下载地址。这样后续可以把大型二进制发布物迁移到 GitHub Release/LFS，而不改变一键部署命令。
