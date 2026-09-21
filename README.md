# MusicSync - 多平台音乐同步下载工具

支持网易云、QQ音乐、酷狗、酷我、汽水等平台歌单同步，多源按质量下载，自动刮削元数据。

## 快速开始

```bash
git clone https://github.com/yq487900/music-sync.git
cd music-sync

# 1. 配置环境变量
cp .env.example .env
# 编辑 .env，至少修改 MUSIC_DIR 指向宿主机音乐目录

# 2. 选择部署模式启动

# 基础版（仅核心应用，HTTP 8000）
docker compose up -d

# 生产版（含 Nginx 反代 + HTTPS 自动证书）
docker compose --profile prod up -d

# 开发版（代码热重载，端口 8001）
docker compose --profile dev up -d

# 初始化数据库（首次部署或版本升级）
docker compose --profile tools run --rm migrate
```

访问：
- 基础/开发：`http://<host>:8000` / `http://<host>:8001`
- 生产：`https://<your-domain>`

## 部署模式对比

| 模式 | 适用场景 | 端口 | 特性 |
|------|----------|------|------|
| `docker compose up -d` | 个人/内网测试 | 8000 | 纯应用，无反代 |
| `--profile prod` | 公网生产环境 | 80/443 | Nginx + Certbot 自动 HTTPS、安全头、WS 支持 |
| `--profile dev` | 本地开发调试 | 8001 | 代码热重载、挂载源码目录 |
| `--profile tools run migrate` | 首次部署/升级 | - | 仅初始化 SQLite 表结构 |

## 目录映射说明

| 容器路径 | 说明 | 默认宿主机路径 |
|----------|------|----------------|
| `/data` | SQLite 数据库、配置文件、Cookie | `./data` (命名卷 `musicsync_data`) |
| `/music` | 下载目录、need_scrape 目录 | `${MUSIC_DIR:-/mnt/music}` |
| `/config` | 只读配置模板（可选） | `${CONFIG_DIR:-./config}` |

## 首次使用流程

1. 打开 Web UI → **网易云扫码登录** → 扫码授权
2. 进入 **配置** 页面：
   - 设置下载目录（默认 `/music`）
   - 调整音源优先级（netease > qq > luoshe）
   - 勾选「定时自动同步/下载」并设置时间
3. 回到首页点击 **手动同步网易云歌单** / **手动下载待办曲目**
4. 之后按计划自动运行，或随时手动触发

## 生产环境 HTTPS 配置

1. 域名解析到服务器 IP
2. 编辑 `.env` 填入 `DOMAIN` 与 `SSL_EMAIL`
3. 修改 `nginx/nginx.conf` 中 `server_name` 与证书路径
4. 启动生产栈：
   ```bash
   docker compose --profile prod up -d
   ```
5. Certbot 会自动申请/续期 Let's Encrypt 证书

## 常用运维命令

```bash
# 查看日志
docker compose logs -f musicsync

# 进入容器调试
docker compose exec musicsync sh

# 备份数据库
docker compose exec musicsync cp /data/musicsync.db /data/musicsync.db.bak

# 更新镜像并重启
docker compose pull && docker compose up -d --build

# 完全卸载（含数据卷）
docker compose down -v
```

## 自定义音源

在 `app/sources/` 下新建 `my_source.py` 实现 `MusicSource` 抽象类，随后在 Web 配置页的「源优先级」加入名称即可。

## 许可证

MIT License