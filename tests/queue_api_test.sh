#!/bin/bash
# 新功能接口自测：勾选 / 音源选择 / 单曲下载 / 队列暂停恢复取消
B=${B:-http://127.0.0.1:13570}   # 换地址：B=http://你的NAS:13570 ./tests/queue_api_test.sh
DIR=$(dirname "$0")

say() { echo; echo "=== $1 ==="; }
j() { python3 -c "import sys,json;d=json.load(sys.stdin);$1"; }

say "0. 把下载目录临时指向测试目录（避免写入真实音乐库）"
ORIG=$(curl -s -m 20 "$B/api/config" | j "print((d.get('library') or {}).get('layout','album'))")
ORIG_DIR=$(curl -s -m 20 "$B/api/config" | j "print(d['download_dir'])")
echo "  原下载目录: $ORIG_DIR"
post_cfg() {
  curl -s -X POST "$B/config" -d "download_dir=$1&layout=$2&naming=&chain=jymaster,hires,lossless,exhigh,standard&upgrade_existing=true&lrc=true&embed=true&nfo=true&concurrency=1&api_delay=0.35&max_per_run=0&fail_backoff=3&backoff_hours=24&auto_sync=false&auto_download=false&auto_upload=false&sync_time=02:00" >/dev/null
}
post_cfg "/data/tmp/qtest" "$ORIG"
echo "  已切到 /data/tmp/qtest，并发=1"

say "1. 取两首未下载的曲目"
IDS=$(curl -s -m 60 "$B/api/tracks?page=1&size=2&filter=pending" | python3 -c "
import sys,json
d=json.load(sys.stdin)
ids=[x['id'] for x in d['items']]
print(' '.join(str(i) for i in ids))")
echo "  曲目 id: $IDS"
T1=$(echo $IDS | cut -d' ' -f1)
T2=$(echo $IDS | cut -d' ' -f2)

say "2. 勾选状态读写"
curl -s -X POST "$B/api/tracks/select" -H 'Content-Type: application/json' \
     -d "{\"ids\":[$T1],\"selected\":false}" | j "print('  取消勾选:', d)"
curl -s -m 20 "$B/api/tracks?page=1&size=1&filter=all" | j "print('  已勾选总数:', d['selected'])"
curl -s -X POST "$B/api/tracks/select" -H 'Content-Type: application/json' \
     -d "{\"ids\":[$T1],\"selected\":true}" | j "print('  恢复勾选:', d)"

say "3. 音源偏好读写"
for SRC in "" "netease"; do
  curl -s -X POST "$B/api/tracks/$T1/source" -H 'Content-Type: application/json' \
       -d "{\"source\":\"$SRC\"}" | j "print('  source=$SRC ->', d['label'])"
done
SID=$(curl -s -m 20 "$B/api/sources" | python3 -c "
import sys,json
s=json.load(sys.stdin)['sources']
print(s[0]['id'] if s else '')")
if [ -n "$SID" ]; then
  curl -s -X POST "$B/api/tracks/$T1/source" -H 'Content-Type: application/json' \
       -d "{\"source\":\"$SID\"}" | j "print('  指定第三方音源 ->', d['label'])"
fi
curl -s -X POST "$B/api/tracks/$T1/source" -H 'Content-Type: application/json' -d '{"source":""}' >/dev/null
echo "  已还原为自动"

say "4. 单曲下载 → 队列"
curl -s -X POST "$B/api/tracks/$T2/download" -H 'Content-Type: application/json' -d '{}' \
  | j "print('  入队:', d['queued']['state'], d['queued']['title'])"
sleep 1
curl -s -m 20 "$B/api/queue" | j "print('  队列:', {k:v for k,v in d['download'].items() if k not in ('tasks','name')})"

say "5. 暂停 → 检查进度冻结"
sleep 2
curl -s -X POST "$B/api/queue/$T2/pause" | j "print('  暂停后 state =', d['task']['state'])"
P1=$(curl -s -m 20 "$B/api/queue" | python3 -c "
import sys,json
d=json.load(sys.stdin)
t=[x for x in d['download']['tasks'] if x['track_id']==$T2]
print(t[0]['done'] if t else -1)")
sleep 3
P2=$(curl -s -m 20 "$B/api/queue" | python3 -c "
import sys,json
d=json.load(sys.stdin)
t=[x for x in d['download']['tasks'] if x['track_id']==$T2]
print(t[0]['done'] if t else -1)")
echo "  暂停期间已下载字节: $P1 -> $P2 （应相等）"
[ "$P1" = "$P2" ] && echo "  ✅ 暂停生效，进度未继续增长" || echo "  ❌ 暂停无效"

say "6. 继续 → 应跑完"
curl -s -X POST "$B/api/queue/$T2/resume" | j "print('  继续后 state =', d['task']['state'])"
for i in $(seq 1 40); do
  ST=$(curl -s -m 20 "$B/api/queue" | python3 -c "
import sys,json
d=json.load(sys.stdin)
t=[x for x in d['download']['tasks'] if x['track_id']==$T2]
print(t[0]['state'] if t else 'gone')")
  [ "$ST" = "done" ] || [ "$ST" = "failed" ] && break
  sleep 2
done
curl -s -m 20 "$B/api/queue" | python3 -c "
import sys,json
d=json.load(sys.stdin)
t=[x for x in d['download']['tasks'] if x['track_id']==$T2][0]
print(f\"  最终: {t['state']} | 音质 {t['level'] or '-'} | 音源 {t['source'] or '-'} | {t['done']}/{t['total']} 字节 | 错误 {t['error'] or '无'}\")"

say "7. 取消 → 另一首"
curl -s -X POST "$B/api/tracks/$T1/download" -H 'Content-Type: application/json' -d '{}' >/dev/null
sleep 2
curl -s -X POST "$B/api/queue/$T1/cancel" | j "print('  取消后 state =', d['task']['state'])"
sleep 2
curl -s -m 20 "$B/api/queue" | python3 -c "
import sys,json
d=json.load(sys.stdin)
t=[x for x in d['download']['tasks'] if x['track_id']==$T1]
print('  最终 state =', t[0]['state'] if t else '(已清理)')"

say "8. 批量控制"
curl -s -X POST "$B/api/queue/clear" | j "print('  清空已结束:', d)"
curl -s -m 20 "$B/api/queue" | j "print('  剩余任务数:', len(d['download']['tasks']))"

say "9. 还原配置"
post_cfg "$ORIG_DIR" "$ORIG"
curl -s -m 20 "$B/api/config" | j "print('  下载目录已还原:', d['download_dir'])"

say "10. 测试目录内容（应为空或无残留 .part）"
cat > /tmp/lsdir.sh <<'EOS'
#!/bin/bash
docker exec music-sync sh -c 'ls -R /data/tmp/qtest 2>/dev/null | head -20; echo "---"; find /data/tmp -name "*.part" 2>/dev/null | head'
EOS
${ROOTRUN:-echo} /tmp/lsdir.sh      # 需要 root 时用 ROOTRUN 指向转发脚本
