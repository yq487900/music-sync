#!/bin/bash
# 真实歌曲上传云盘 + 元数据补齐验证（全程临时目录，结束后清理云盘与本地）
B=${B:-http://127.0.0.1:13570}   # 换地址：B=http://你的NAS:13570 ./tests/cloud_real_test.sh
# 在容器里跑脚本：有 docker 权限就直接 docker exec；没有（比如以普通用户跑）就用
# ROOTRUN 指向一个能执行 docker 的转发脚本，例如 ROOTRUN=/path/rootrun.sh
run_in_ct() {  # 用法：run_in_ct /tmp/xxx.sh [参数...]
  if [ -n "${ROOTRUN:-}" ]; then "${ROOTRUN}" "$@"; else docker exec music-sync bash "$@"; fi
}

j() { python3 -c "import sys,json;d=json.load(sys.stdin);$1"; }

cfg() {
  curl -s -X POST "$B/config" -d "download_dir=$1&layout=album&naming=&chain=jymaster,hires,lossless,exhigh,standard&upgrade_existing=true&lrc=true&embed=true&nfo=true&concurrency=1&api_delay=0.35&max_per_run=0&fail_backoff=3&backoff_hours=24&auto_sync=false&auto_download=false&auto_upload=false&sync_time=02:00" >/dev/null
}

cat > /tmp/realpick.py <<'PY'
import sys
sys.path.insert(0, "/app")
from app.db.models import SessionLocal, Track
db = SessionLocal()
rows = (db.query(Track).filter(Track.status == "new")
        .filter(Track.duration > 40).filter(Track.duration < 170)
        .order_by(Track.duration.asc()).limit(5).all())
for r in rows:
    print(f"{r.id}\t{int(r.duration)}\t{r.title}\t{r.artist}")
db.close()
PY
cat > /tmp/realpick.sh <<'EOS'
#!/bin/bash
docker cp /tmp/realpick.py music-sync:/tmp/realpick.py >/dev/null
docker exec music-sync python /tmp/realpick.py
EOS

echo "=== 上传前云盘数量 ==="
BEFORE=$(curl -s -m 30 "$B/api/cloud/quota" | j "print(d.get('count'))")
echo "  count = $BEFORE"

echo
echo "=== 1. 挑一首短歌并切到临时下载目录 ==="
PICK=$(run_in_ct /tmp/realpick.sh | head -1)
TID=$(echo "$PICK" | cut -f1)
DUR=$(echo "$PICK" | cut -f2)
TITLE=$(echo "$PICK" | cut -f3)
echo "  选中: id=$TID 时长=${DUR}s 《$TITLE》"
cfg "/data/tmp/cloudreal"

echo
echo "=== 2. 下载到临时目录 ==="
curl -s -X POST "$B/api/tracks/$TID/download" -H 'Content-Type: application/json' -d '{"source":"netease"}' >/dev/null
for i in $(seq 1 60); do
  ST=$(curl -s -m 20 "$B/api/queue" | TID=$TID python3 -c "
import os,sys,json
tid=int(os.environ['TID']); d=json.load(sys.stdin)
t=[x for x in d['download']['tasks'] if x['track_id']==tid]
print(t[0]['state'] if t else 'gone')")
  [ "$ST" = "done" ] || [ "$ST" = "failed" ] && break
  sleep 2
done
curl -s -m 20 "$B/api/queue" | TID=$TID python3 -c "
import os,sys,json
tid=int(os.environ['TID']); d=json.load(sys.stdin)
t=[x for x in d['download']['tasks'] if x['track_id']==tid][0]
print(f\"  下载: {t['state']} | 音质 {t['level']} | 音源 {t['source']} | 错误 {t['error'] or '无'}\")"

cat > /tmp/showfile.py <<PYEOF
import sys
sys.path.insert(0, "/app")
from app.db.models import SessionLocal, Track
db = SessionLocal()
r = db.query(Track).filter_by(id=$TID).first()
print("  本地文件:", r.file_path or "(无)")
print("  大小:", r.size, "字节 | md5:", (r.md5 or "")[:12])
db.close()
PYEOF
cat > /tmp/showfile.sh <<'EOS'
#!/bin/bash
docker cp /tmp/showfile.py music-sync:/tmp/showfile.py >/dev/null
docker exec music-sync python /tmp/showfile.py
EOS
run_in_ct /tmp/showfile.sh

echo
echo "=== 3. 上传到云盘（含元数据补齐） ==="
curl -s -X POST "$B/api/cloud/upload/$TID" >/dev/null
for i in $(seq 1 90); do
  ST=$(curl -s -m 20 "$B/api/queue" | TID=$TID python3 -c "
import os,sys,json
tid=int(os.environ['TID']); d=json.load(sys.stdin)
t=[x for x in d['upload']['tasks'] if x['track_id']==tid]
print(t[0]['state'] if t else 'gone')")
  [ "$ST" = "done" ] || [ "$ST" = "failed" ] && break
  sleep 2
done
curl -s -m 20 "$B/api/queue" | TID=$TID python3 -c "
import os,sys,json
tid=int(os.environ['TID']); d=json.load(sys.stdin)
t=[x for x in d['upload']['tasks'] if x['track_id']==tid][0]
print(f\"  上传: {t['state']} | 错误 {t['error'] or '无'}\")"

echo
echo "=== 4. 核对元数据补齐结果 ==="
cat > /tmp/ckmeta.sh <<EOS
#!/bin/bash
docker exec music-sync python -c "
import shutil, sys
sys.path.insert(0,'/app')
from pathlib import Path
from app.db.models import SessionLocal, Track
db=SessionLocal(); r=db.query(Track).filter_by(id=int($TID)).first()
print(f'  DB: title={r.title} | artist={r.artist} | album={r.album}')
print(f'  cloud_sid={r.cloud_sid} | state={r.cloud_state} | error={r.cloud_error or \"无\"}')
p = Path(r.file_path) if r.file_path else None
if p and p.exists() and p.suffix == '.mp3':
    from mutagen.id3 import ID3
    t = ID3(str(p))
    print(f'  标签: 标题={t.get(\"TIT2\")} 歌手={t.get(\"TPE1\")} 专辑={t.get(\"TALB\")} 封面帧={len(t.getall(\"APIC\"))} 歌词帧={len(t.getall(\"USLT\"))}')
elif p and p.exists():
    print(f'  标签: 跳过（{p.suffix} 文件，已由落地时写入）')
sid = r.cloud_sid
db.close()
print('CLOUDSID=' + (sid or ''))
"
EOS
run_in_ct /tmp/ckmeta.sh | grep -v '^CLOUDSID'
CSID=$(run_in_ct /tmp/ckmeta.sh | grep '^CLOUDSID=' | cut -d= -f2 | tr -d '[:space:]')
curl -s -m 30 "$B/api/cloud/quota" | j "print('  配额:', {k:d.get(k) for k in ('count','uploaded_by_us')})"

echo
echo "=== 5. 清理：删除云盘条目 + 撤销本次下载 ==="
if [ -n "$CSID" ]; then
  curl -s -X DELETE "$B/api/cloud/$CSID" | j "print('  云盘删除:', d)"
else
  echo "  无 cloud_sid"
fi
cat > /tmp/undo.py <<'PY'
import shutil, sys
sys.path.insert(0, "/app")
from app.db.models import SessionLocal, Track
db = SessionLocal()
tid = int(sys.argv[1])
r = db.query(Track).filter_by(id=tid).first()
if r:
    r.status, r.file_path, r.ext, r.size, r.md5 = "new", "", "", 0, ""
    r.level, r.br, r.downloaded = "", 0, False
    r.src_used = ""
    r.cloud_sid, r.cloud_state, r.cloud_error, r.cloud_at = "", "", "", ""
    r.fail_count, r.last_error = 0, ""
db.commit(); db.close()
shutil.rmtree("/data/tmp/cloudreal", ignore_errors=True)
print("  已撤销下载记录并删除临时文件")
PY
cat > /tmp/undo.sh <<EOS
#!/bin/bash
docker cp /tmp/undo.py music-sync:/tmp/undo.py >/dev/null
docker exec music-sync python /tmp/undo.py $TID
EOS
run_in_ct /tmp/undo.sh
cfg "/music"

sleep 3
echo
echo "=== 6. 最终核对 ==="
AFTER=$(curl -s -m 30 "$B/api/cloud/quota" | j "print(d.get('count'))")
curl -s -m 20 "$B/api/config" | j "print('  下载目录已还原:', d['download_dir'])"
echo "  上传前 = $BEFORE ，清理后 = $AFTER"
[ "$BEFORE" = "$AFTER" ] && echo "  ✅ 云盘已恢复原状" || echo "  ⚠️ 差 $((AFTER-BEFORE)) 首"
