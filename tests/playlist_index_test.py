"""歌单索引自测（不联网）：in_playlist / candidates / 落盘重载

跑法（容器里）：python tests/playlist_index_test.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for cand in (ROOT, Path("/app"), Path.cwd()):
    if (cand / "app" / "playlist_index.py").exists() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

import app.playlist_index as pl_mod                 # noqa: E402
from app.playlist_index import PlaylistIndex        # noqa: E402

OK, FAIL = [], []


def check(name, got, want):
    if got == want:
        OK.append(name)
    else:
        FAIL.append(f"{name}: 得到 {got!r}，期望 {want!r}")


def sample() -> dict:
    return {
        "111": {"name": "喜欢的音乐", "track_count": 2, "update_time": 1,
                "ids": [["111", 1000.0], ["222", 5000.0]]},
        "222": {"name": "测试歌单B", "track_count": 1, "update_time": 2,
                "ids": [["111", 900.0], ["333", 9999999999.0]]},
    }


def main():
    tmp = tempfile.TemporaryDirectory()
    real_path = pl_mod.INDEX_PATH
    pl_mod.INDEX_PATH = Path(tmp.name) / "pl_index.json"
    try:
        idx = PlaylistIndex()
        check("空索引：没就绪", idx.ready, False)
        check("空索引：in_playlist 返回未知", idx.in_playlist("111"), None)

        idx.playlists = sample()
        idx.at = 1790000000.0
        idx._rebuild_sets()
        check("就绪", idx.ready, True)
        check("歌曲合集去重", sorted(idx.sids), ["111", "222", "333"])
        check("在歌单里", idx.in_playlist("111"), True)
        check("不在歌单里", idx.in_playlist("888"), False)
        check("不是正式歌曲 id → 未知", idx.in_playlist("abcdef"), None)
        check("同一个 id 取最早加入时间", idx.added["111"], 900.0)
        check("记下了所在歌单名", idx.pl_name["333"], "测试歌单B")

        check("new 模式只回新歌", idx.candidates("new", 1000000000), ["333"])
        check("new 模式（很早的起算点）全都要", len(idx.candidates("new", 0)), 3)
        check("full 模式要全部", len(idx.candidates("full", 0)), 3)
        check("按加入时间排序", idx.candidates("full", 0)[0], "111")

        # 没有加入时间（at=0，接口有时不给）的老歌：不能被「从现在开始监控」选中，
        # 否则会把整个歌单当新歌下一遍
        idx.playlists["111"]["ids"].append(["444", 0.0])
        idx._rebuild_sets()
        check("at=0 不算新歌", "444" in idx.candidates("new", 1000000000), False)
        check("at=0 仍在 full 里", "444" in idx.candidates("full", 0), True)

        idx._save_disk()
        idx2 = PlaylistIndex()
        check("落盘能读回歌曲数", idx2.status()["songs"], 4)   # 含上面补的 444
        check("落盘后判定一致", idx2.in_playlist("222"), True)
        check("落盘后 candidates 一致", len(idx2.candidates("full", 0)), 4)
    finally:
        pl_mod.INDEX_PATH = real_path
        tmp.cleanup()


main()
print("=" * 60)
print(f"通过 {len(OK)} 项，失败 {len(FAIL)} 项")
for x in FAIL:
    print("  ✗", x)
sys.exit(1 if FAIL else 0)
