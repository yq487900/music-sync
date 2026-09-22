"""云盘索引自测

两种跑法：
  python3 tests/cloud_index_test.py            只用构造数据（不联网也能跑）
  python3 tests/cloud_index_test.py <快照.json> 再拿真实云盘快照对一遍

快照格式：[{"sid": "...", "title": "...", "artist": "...", "has_cover": true}, ...]
（has_cover 用来近似表示「这条云盘条目已匹配到正式曲目」）
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

# 本地跑（仓库根）和容器内跑（/app）都能找到 app 包
ROOT = Path(__file__).resolve().parents[1]
for cand in (ROOT, Path("/app"), Path.cwd()):
    if (cand / "app" / "cloud_index.py").exists() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

import app.cloud_index as ci_mod          # noqa: E402

OK, FAIL = [], []


def check(name, got, want):
    if got == want:
        OK.append(name)
    else:
        FAIL.append(f"{name}: 得到 {got!r}，期望 {want!r}")


def make(items):
    """用构造数据建一个索引（走和线上一样的 _rebuild）"""
    idx = ci_mod.CloudIndex()
    idx._rebuild(items)
    return idx


print("=" * 60)
print("一、判定逻辑（构造数据）")

# ① 匹配到正式曲目的条目：sid 就是网易云歌曲 id
matched = {"sid": "2101642204", "title": "阿萨阿萨 牛奶歌", "artist": "小喇叭合唱团",
           "matched": True, "al_id": 333}
# ② 自己上传、网易云没认出来的条目：歌名/歌手来自文件标签，sid 只是云盘自己的 id
unmatched = {"sid": "3439565756", "title": "圣诞星 (feat. 杨瑞代)", "artist": "周杰伦 / 杨瑞代",
             "matched": False, "al_id": 0}
# ③ 另一条没认出来的：文件名带下划线、歌名带括号
unmatched2 = {"sid": "3439000001", "title": "Monica [Remastered]", "artist": "张国荣",
              "matched": False, "al_id": 0}

idx = make([matched, unmatched, unmatched2])

check("匹配条目按 sid 命中", idx.lookup("2101642204", "阿萨阿萨 牛奶歌", "小喇叭合唱团"), True)
check("匹配条目按 sid 命中（不靠歌名）", idx.lookup("2101642204", "", ""), True)
check("自己传的：歌名带 feat.、歌单里干净 → 命中",
      idx.lookup(186016, "圣诞星", "周杰伦 / 杨瑞代"), True)
check("自己传的：只差括号 → 命中", idx.lookup(270000, "Monica", "张国荣"), True)
check("不在云盘 → False", idx.lookup(999999, "晴天", "周杰伦"), False)
check("歌名太短且歌手对不上 → 不猜（False）", idx.lookup(999998, "精卫", "银翼杀手"), False)

# 本地库记录着我们上传时拿到的云盘 id → 核对它还在这条云盘条目上
check("本地记录的上传条目还在云盘 → True",
      idx.lookup(999997, "随便", "随便", uploaded_sid="3439565756"), True)
check("本地记录的上传条目已被删掉 → 不再算在云盘",
      idx.lookup(999997, "随便", "随便", uploaded_sid="999999999"), False)

# 索引还没建好：不能谎报「不在」
empty = ci_mod.CloudIndex()
empty.sids, empty.keys, empty.titles, empty.count = set(), set(), set(), 0
empty.entry_ids = set()
check("索引没建好 → None（未知）", empty.lookup(186016, "圣诞星", "周杰伦"), None)
check("索引没建好但有上传记录 → True", empty.lookup(1, "x", "y", uploaded_sid="123"), True)

print("=" * 60)
print("二、落盘 / 重载")

with tempfile.TemporaryDirectory() as tmp:
    ci_mod.INDEX_PATH = Path(tmp) / "cloud_index.json"
    a = ci_mod.CloudIndex()
    a._rebuild([matched, unmatched])
    a._save_disk()
    b = ci_mod.CloudIndex()          # 模拟容器重启后从磁盘读回
    check("重启后条目数一致", b.count, 2)
    check("重启后仍能命中", b.lookup(186016, "圣诞星", "周杰伦"), True)
    check("重启后 sids 一致", b.sids, a.sids)

print("=" * 60)
print("三、真实云盘快照对比（可选）")

if len(sys.argv) > 1:
    snap = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    items = [{"sid": str(x["sid"]), "title": x["title"], "artist": x["artist"],
              "matched": bool(x.get("has_cover")), "al_id": 1 if x.get("has_cover") else 0}
             for x in snap]
    real = make(items)
    print(f"云盘条目 {real.count}：已匹配 {len(real.sids)}、"
          f"未匹配(靠歌名认) {len(real.titles)}")
    songs = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")) if len(sys.argv) > 2 else []
    hits = 0
    for s in songs:
        got = real.lookup(s["sid"], s["title"], s["artist"])
        sid_hit = str(s["sid"]) in real.sids
        hits += bool(got)
        flag = "sid精确" if sid_hit else ("歌名命中" if got else "不在云盘")
        print(f"  {str(s['sid']):<12} {s['title'][:22]:<24} {flag}")
    print(f"命中 {hits} / {len(songs)}")
else:
    print("（跳过：没给快照文件）")

print("=" * 60)
print("四、限流截断防御（网易云偶尔只回一部分，别用它覆盖好索引）")
print("=" * 60)


class _Flaky(ci_mod.CloudIndex):
    """假的索引：_fetch 返回我们指定的条数（不联网）"""

    def __init__(self, fake_items):
        super().__init__()
        self._fake = fake_items

    async def _fetch(self, cookie):
        return self._fake


big = [{"sid": str(1000 + i), "title": f"歌{i}", "artist": "歌手",
        "matched": True, "al_id": 1} for i in range(1000)]
idx = _Flaky(big)
r = asyncio.run(idx.refresh("ck"))
check("正常刷新成功", r.get("ok"), True)
check("索引条数", idx.count, 1000)
idx._fake = big[:600]                 # 模拟被限流截断（实测 3931 → 2600）
r2 = asyncio.run(idx.refresh("ck"))
check("疑似限流时不覆盖", r2.get("ok"), False)
check("保留原索引条数", idx.count, 1000)
check("保留原判定（不会误报不在云盘）", idx.lookup("1000"), True)
idx._fake = big + [{"sid": "9999", "title": "新加的", "artist": "歌手",
                    "matched": True, "al_id": 1}]
r3 = asyncio.run(idx.refresh("ck"))
check("变多时正常接收", (r3.get("ok"), idx.count), (True, 1001))

print("=" * 60)
print(f"通过 {len(OK)} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
