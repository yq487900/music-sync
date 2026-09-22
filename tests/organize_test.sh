#!/bin/bash
# 整理页自测：扫描 → 自动刮削 → 手动保存 → 清空 → 回收站 → 越权拦截
# 全部在 /music/_orgtest 临时目录里做，结束后自动清理，不碰你的音乐库
B=${B:-http://127.0.0.1:13570}   # 换地址：B=http://你的NAS:13570 ./tests/organize_test.sh
T=/music/_orgtest
P="$T/屋顶 - 周杰伦.flac"
J='Content-Type: application/json'

echo "=== 1. 造一个无标签文件（模拟手动放进音乐库的老歌）==="
docker exec music-sync python -c "
import shutil
from pathlib import Path
from mutagen.flac import FLAC
src = sorted(Path('/music').rglob('*.flac'))[0]
d = Path('$T'); d.mkdir(parents=True, exist_ok=True)
dst = d / '屋顶 - 周杰伦.flac'
shutil.copy(src, dst)
f = FLAC(str(dst)); f.delete(); f.save()
print('  已造好:', dst.name, '| 标签数:', len(FLAC(str(dst)).keys()))"

echo "=== 2. 扫描 + 体检 ==="
curl -s -m 60 -X POST "$B/api/organize/scan" | python3 -c "
import sys,json; d=json.load(sys.stdin); print('  扫描', d['scanned'], '首 | 体检', d['audit'])"

echo "=== 3. 自动刮削（按文件名匹配）==="
curl -s -m 120 -X POST "$B/api/organize/scrape" -H "$J" -d "{\"path\":\"$P\"}" | python3 -c "
import sys,json; d=json.load(sys.stdin)
m = d.get('matched') or {}
print('  ok:', d.get('ok'), '| 匹配:', m.get('name'), '-', m.get('artist'),
      '| 封面:', d.get('cover'), '| 歌词:', d.get('lyrics'), d.get('reason') or '')"

echo "=== 4. 写进文件的内容 ==="
docker exec music-sync python -c "
from pathlib import Path
from mutagen.flac import FLAC
p = Path('$P'); f = FLAC(str(p)); lrc = p.with_suffix('.lrc')
print('  标签:', f.get('title',[''])[0], '|', f.get('artist',[''])[0], '|', f.get('album',[''])[0])
print('  网易云id:', f.get('netease_id',['(无)'])[0])
print('  内嵌封面:', len(f.pictures), '张 | 内嵌歌词:', len((f.get('lyrics') or [''])[0]), '字符')
print('  外挂 lrc:', lrc.exists(), '|', len(lrc.read_text('utf-8')) if lrc.exists() else 0, '字符')"

echo "=== 5. 按网易云 ID 匹配（远例：屋顶 5257138）==="
curl -s -m 60 -X POST "$B/api/organize/match" -H "$J" -d '{"id":"5257138"}' | python3 -c "
import sys,json; s = json.load(sys.stdin).get('song') or {}
print(' ', s.get('name'), '|', s.get('artist'), '|', s.get('album'),
      '| 歌词', len(s.get('lyric') or ''), '字符 | 封面', '有' if s.get('cover_data') else '无')"

echo "=== 6. 手动保存（改标签 + 写歌词）==="
curl -s -m 60 -X POST "$B/api/organize/save" -H "$J" \
  -d "{\"path\":\"$P\",\"title\":\"手改歌名\",\"artist\":\"测试歌手\",\"album\":\"测试专辑\",\"track\":7,\"date\":\"2020-01-01\",\"lyrics\":\"[00:00.00]测试歌词\"}" >/dev/null
docker exec music-sync python -c "
from mutagen.flac import FLAC
f = FLAC('$P')
print('  歌名:', f.get('title',[''])[0], '| 歌手:', f.get('artist',[''])[0], '| 音轨号:', f.get('tracknumber',[''])[0])"

echo "=== 7. 清空封面与歌词 ==="
curl -s -m 60 -X POST "$B/api/organize/save" -H "$J" \
  -d "{\"path\":\"$P\",\"title\":\"手改歌名\",\"artist\":\"测试歌手\",\"lyrics\":\"\",\"clear_cover\":true}" >/dev/null
docker exec music-sync python -c "
from pathlib import Path
from mutagen.flac import FLAC
f = FLAC('$P')
print('  内嵌封面:', len(f.pictures), '张 | 内嵌歌词:', len((f.get('lyrics') or [''])[0]), '字符')
print('  外挂 lrc 还在:', Path('$P').with_suffix('.lrc').exists())"

echo "=== 8. 移入回收站 + 越权拦截 ==="
curl -s -m 60 -X POST "$B/api/organize/delete" -H "$J" -d "{\"paths\":[\"$P\",\"/etc/passwd\"]}" | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('  已移动:', d.get('moved'), '| 越权被拦下:', [f for f in (d.get('failed') or [])] or '（/etc/passwd 已被过滤）')
print('  本地状态同步行数:', d.get('synced'))"

echo "=== 9. 回收站列表（应能看到刚删的这首）==="
curl -s -m 60 "$B/api/trash?page=1&size=50" | python3 -c "
import sys,json; d=json.load(sys.stdin)
hit = [x for x in (d.get('items') or []) if '_orgtest' in x.get('rel','')]
print('  回收站共', d['summary']['count'], '首 | 本次测试的:', [x['rel'] for x in hit])
print('  时间显示:', hit[0]['deleted_at'] if hit else '(没找到)')"

echo "=== 10. 从回收站恢复（文件应回到音乐库，本地状态变回已下载）==="
TRASH_PATH=$(docker exec music-sync python -c "
from pathlib import Path
hit = [p for p in Path('/data/_trash').rglob('*') if p.is_file() and '_orgtest' in str(p) and p.suffix == '.flac']
print(hit[0] if hit else '')")
curl -s -m 60 -X POST "$B/api/trash/restore" -H "$J" -d "{\"paths\":[\"$TRASH_PATH\"]}" | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('  恢复:', d.get('count'), '首 | 路径:', (d.get('restored') or [''])[0], '| 同步行数:', d.get('synced'))"
docker exec music-sync python -c "
from pathlib import Path
print('  文件回来了:', Path('$P').exists() or Path('$P'.replace('.flac', ' (回收站恢复).flac')).exists())"

echo "=== 11. 再删一次并彻底清空（真实删除）==="
curl -s -m 60 -X POST "$B/api/organize/delete" -H "$J" -d "{\"paths\":[\"$P\"]}" >/dev/null
TRASH_PATH2=$(docker exec music-sync python -c "
from pathlib import Path
hit = [p for p in Path('/data/_trash').rglob('*') if p.is_file() and '_orgtest' in str(p) and p.suffix == '.flac']
print(hit[0] if hit else '')")
curl -s -m 60 -X POST "$B/api/trash/purge" -H "$J" -d "{\"paths\":[\"$TRASH_PATH2\"]}" | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('  彻底删除:', d.get('count'), '首 | 文件还在吗:', __import__('os').path.exists('$TRASH_PATH2') if '$TRASH_PATH2' else '（没有可删的）')"

echo "=== 12. 清理（只清测试自己造的，绝不动回收站里别的东西）==="
docker exec music-sync python -c "
import shutil
from pathlib import Path
my_file = Path('$P')
shutil.rmtree('/music/_orgtest', ignore_errors=True)          # 测试目录
alts = list(Path('/music/_orgtest').glob('*')) if Path('/music/_orgtest').exists() else []
# 只清空「只剩测试文件」的空批次目录，别碰别人的
t = Path('/data/_trash')
left = 0
if t.exists():
    for d in list(t.iterdir()):
        if d.is_dir() and not any(d.rglob('*')):
            d.rmdir()
        else:
            left += 1
print('  测试文件已删:', not my_file.exists() and not Path('$T').exists())
print('  回收站里别人的东西还在（批次目录数）:', left)"
