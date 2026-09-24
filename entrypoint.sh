#!/usr/bin/env bash
set -e

CONFIG=/data/config.json

if [ ! -f "$CONFIG" ]; then
    cat >"$CONFIG" <<'EOF'
{
  "download_dir": "/music",
  "platforms": {
    "netease": {"cookie": "", "user_id": ""},
    "qq":      {"cookie": "", "user_id": ""}
  },
  "scheduler": {"auto_sync": false, "auto_download": false, "time": "02:00"},
  "playlists": [],
  "library": {"layout": "album", "naming": ""},
  "quality": {"chain": ["jymaster","hires","lossless","exhigh","standard"], "upgrade_existing": true},
  "lyrics": {"lrc": true, "embed": true},
  "nfo": true,
  "limits": {"download_concurrency": 3, "api_delay": 0.35, "max_per_run": 0,
             "fail_backoff": 3, "backoff_hours": 24},
  "music_sources": []
}
EOF
fi

TZ_NAME=$(jq -r '.timezone // "Asia/Shanghai"' "$CONFIG" 2>/dev/null || echo "Asia/Shanghai")
if [ -f "/usr/share/zoneinfo/$TZ_NAME" ]; then
    ln -sf "/usr/share/zoneinfo/$TZ_NAME" /etc/localtime
    echo "$TZ_NAME" > /etc/timezone
fi

mkdir -p /data/tmp
# ncm-api 的临时目录（放在 /data 下持久化）：它把「匿名 token / xeapi 公钥」缓存在临时目录里，
# 放 /tmp 的话每次容器重建都要重新去上游拉一次，拉取慢的时候接口服务会几分钟起不来（页面 502）
mkdir -p /data/ncm-tmp

# 关掉 ncm-api 启动时的「npm 版本检查」（只是提示有没有新版本）。
# 它会在 app.listen() 之前 exec('npm info …')，而 npm 在本机环境里会长时间卡住
# （实测 35 秒仍未返回）→ listen 一直不执行 → 进程活着但不监听端口，页面整片 502。
NCM_APP=/opt/ncm-api/node_modules/@neteasecloudmusicapienhanced/api/app.js
if [ -f "$NCM_APP" ] && grep -q 'checkVersion: true' "$NCM_APP"; then
    sed -i 's/checkVersion: true/checkVersion: false/' "$NCM_APP" \
        && echo "ncm-api: 已关闭启动时的 npm 版本检查"
fi

python - <<'PY'
from app.db.models import Base, engine
Base.metadata.create_all(engine)
print("DB initialized", flush=True)
PY

# 三个进程（网易云接口服务 / 音源沙箱 / Web+同步引擎）由 supervisor 统一托管
exec supervisord -c /app/supervisord.conf
