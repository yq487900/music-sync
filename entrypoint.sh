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

python - <<'PY'
from app.db.models import Base, engine
Base.metadata.create_all(engine)
print("DB initialized", flush=True)
PY

# 三个进程（网易云接口服务 / 音源沙箱 / Web+同步引擎）由 supervisor 统一托管
exec supervisord -c /app/supervisord.conf
