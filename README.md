# VPS 服务器部署看板

这是独立于 `baibiaowang/agu-board` 的 VPS 部署项目。

## 目标

在全新的 Linux/VPS 服务器上，用一条命令从 GitHub 获取完整部署包，自动安装 A 股公告看板、初始化历史数据、配置 systemd 并启动服务。

## 一键部署

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/baibiaowang/vps-server-deploy-board/main/install-from-github.sh)
```

提前设置登录密码：

```bash
BOARD_PASSWORD='你的登录密码' bash <(curl -fsSL https://raw.githubusercontent.com/baibiaowang/vps-server-deploy-board/main/install-from-github.sh)
```

脚本会从本仓库下载 `vps-server-deploy-board-1.0.0.tar.gz`，校验 SHA-256 后再解压安装。

## 数据策略

历史数据库属于重要的初始化数据，不是临时缓存。

首次部署：

```text
GitHub 发布包
  ↓
解压完整程序 + 历史 board.db
  ↓
安装到 /opt/agu-board-v2
  ↓
首次创建 /opt/agu-board-v2/data/board.db
```

后续部署/更新：

```text
下载新程序包
  ↓
更新代码和依赖
  ↓
保留服务器现有 data/
  ↓
保留服务器现有 agu-board.env
```

因此服务器运行期间新增的历史公告数据不会被 GitHub 旧种子数据库覆盖。

## 更新

```bash
sudo /opt/agu-board-v2/update.sh
```

更新前会先自动备份数据库和本地配置，然后下载经过 SHA-256 校验的发布包，再执行安全更新。

## 备份

```bash
sudo /opt/agu-board-v2/backup.sh
```

默认备份到：

```text
/opt/agu-board-backups/
```

## 安装后的主要位置

```text
/opt/agu-board-v2/
├── app/
├── config/
├── web/
├── tools/
├── data/
│   └── board.db
├── logs/
├── .venv/
├── agu-board.env
├── install.sh
├── update.sh
└── backup.sh
```

## 与主项目的关系

- `baibiaowang/agu-board`：主项目开发仓库。
- `baibiaowang/vps-server-deploy-board`：独立的 VPS 初始化、发布包、历史数据种子、更新和备份项目。

两个项目不互相覆盖。

## 发布包

当前版本：`1.0.0`

发布包：`vps-server-deploy-board-1.0.0.tar.gz`

SHA-256：

```text
46e527451fc351ab1c0670037c30fb3abe03f39515cf0a0b186da73427156679
```

后续升级版本时，只需替换版本号、发布包和 SHA-256；一键安装命令保持不变。
