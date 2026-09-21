# MusicSync Docker

多平台歌单同步 + 多源下载 + 元数据刮削

功能：
- 网易云 / QQ / 酷狗 / 酷我 / 汽水 扫码登录同步歌单
- 本地去重：时长 + MD5 + sha256
- 多源优先级下载，默认内置洛雪音乐源，可手动添加
- 自动刮削封面、歌词，失败归入 need_scrape/
- Web GUI 配置下载目录、源优先级

## 使用
```bash
docker compose up -d
浏览器访问 http://localhost:8000
```

配置通过 /config 接口或 Web UI 手动修改，全部可手动覆盖。
