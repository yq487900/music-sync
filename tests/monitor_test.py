"""歌单监控的判定逻辑自测（不联网、不写真实库、不排队）

用临时 SQLite + 假索引 + 打桩的入队函数，验证：
  ① new 模式：只处理「开启监控之后」加进歌单的歌
  ② full 模式：云盘里没有的 → 本地有就排队补传，本地没有就排队下载
  ③ 已在云盘的（full 模式）不重复下载
  ④ 每轮上限生效，剩下的下一轮继续
  ⑤ dry=True 时什么都不做（不建行、不排队）
  ⑥ 开关关着时直接跳过

跑法（容器里）：python tests/monitor_test.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for cand in (ROOT, Path("/app"), Path.cwd()):
    if (cand / "app" / "runner.py").exists() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

import app.config as config                       # noqa: E402
import app.runner as runner                       # noqa: E402
from sqlalchemy import create_engine                              # noqa: E402
from sqlalchemy.orm import sessionmaker                           # noqa: E402

from app.db.models import Base, Track                             # noqa: E402

OK, FAIL = [], []


def check(name, got, want):
    if got == want:
        OK.append(name)
    else:
        FAIL.append(f"{name}: 得到 {got!r}，期望 {want!r}")


class FakePlIndex:
    """假歌单索引：sids / added（加入时间）/ 名字"""

    def __init__(self, sids, added):
        self.sids = set(str(s) for s in sids)
        self.added = {str(k): float(v) for k, v in added.items()}
        self.pl_name = {s: "假歌单" for s in self.sids}

    async def refresh(self, cookie, uid):
        return {"ok": True, "playlists": 1, "songs": len(self.sids), "refreshed": 0}

    def candidates(self, mode="new", since=0.0):
        from app.playlist_index import PlaylistIndex
        return PlaylistIndex.candidates(self, mode, since)


class FakeCloud:
    """假云盘索引：只有 sids 里的歌算「在云盘」"""

    def __init__(self, in_cloud):
        self._in = set(str(x) for x in in_cloud)
        self.at = 0.0
        self.count = 0

    def ensure_async(self, cookie, force=False):
        pass

    def lookup(self, sid, title="", artist="", uploaded_sid=""):
        return str(sid) in self._in


class FakeCloudHolder:
    def __init__(self, idx):
        self.index = idx


class FakeNcm:
    """假的网易云接口：只为「新歌建库行」提供歌曲详情"""

    async def song_detail(self, ids):
        return [{"id": int(i), "name": f"新歌{i}", "ar": [{"name": "新歌手"}],
                 "al": {"id": 900 + int(i), "name": "新专辑", "picUrl": "http://x/p.jpg"},
                 "dt": 200000, "no": 1, "cd": "1", "publishTime": 0} for i in ids]

    async def lyric(self, sid):
        return ""

    async def close(self):
        pass


async def main():
    tmp = tempfile.TemporaryDirectory()
    engine = create_engine(f"sqlite:///{tmp.name}/t.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine)

    # 曲目：1=本地已有、2=没下载、3=本地已有
    db = Sess()
    for tid, sid, title in ((1, "111", "本地已有的A"), (2, "222", "没下载的B"),
                            (3, "333", "本地已有的C")):
        tr = Track(platform="netease", platform_track_id=sid, title=title, artist="歌手",
                   status="ok" if tid in (1, 3) else "new")
        tr.file_path = f"/music/{sid}.flac" if tid in (1, 3) else ""
        db.add(tr)
    db.commit()
    db.close()

    # 歌单索引：111/222/333 都是老歌（at=1000），444 是新歌（at=9999999999）
    pl = FakePlIndex(["111", "222", "333", "444"], {111: 1000, 222: 1000, 333: 1000, 444: 9999999999})
    # 云盘：111 在云盘（所以 full 模式不用管），222/333/444 不在
    cloud = FakeCloudHolder(FakeCloud(["111"]))

    import app.playlist_index as pl_mod
    import app.cloud_index as ci_mod
    real_pl, real_ci, real_sess = pl_mod.index, ci_mod.index, runner.SessionLocal
    real_dl, real_up = runner.enqueue_download_one, runner.enqueue_upload
    real_load, real_save = config.load, config.save
    real_ncm = runner._ncm
    calls = {"down": [], "up": []}

    try:
        pl_mod.index = pl
        ci_mod.index = cloud.index
        runner.SessionLocal = Sess
        runner.enqueue_download_one = lambda tid, cfg, **kw: calls["down"].append(tid)
        runner.enqueue_upload = lambda tid, **kw: calls["up"].append(tid)
        base_cfg = {"platforms": {"netease": {"cookie": "ck", "user_id": "1"}},
                    "monitor": {"on": True, "mode": "new", "since": 1000000, "batch": 20}}
        config.load = lambda: dict(base_cfg)
        config.save = lambda cfg: base_cfg.update(cfg)
        runner._ncm = lambda cfg: FakeNcm()      # 新歌建库行用它取详情

        # ---------- ① new 模式：只处理 since 之后加进歌单的歌 ----------
        calls["down"].clear(); calls["up"].clear()
        r = await runner.monitor_playlists()
        check("new 模式只下新歌", r["download"], 1)
        check("new 模式不补传", r["upload"], 0)
        check("new 模式扫到的数量（只算新歌）", r["scanned"], 1)
        db2 = Sess()
        got = db2.query(Track).filter(Track.platform_track_id == "444").first()
        check("新歌已建库行", got is not None and got.title, "新歌444")
        db2.close()

        # ---------- ② full 模式 ----------
        calls["down"].clear(); calls["up"].clear()
        base_cfg["monitor"] = {"on": True, "mode": "full", "since": 0, "batch": 20}
        r = await runner.monitor_playlists()
        check("full 模式：下载 2（没本地+云盘没有）", r["download"], 2)
        check("full 模式：补传 1（本地有+云盘没有）", r["upload"], 1)
        check("已在云盘的 111 不动", 1 in calls["down"] or 1 in calls["up"], False)

        # ---------- ④ 每轮上限 ----------
        base_cfg["monitor"] = {"on": True, "mode": "full", "since": 0, "batch": 1}
        r = await runner.monitor_playlists()
        check("上限 1：只排 1 首下载", r["download"], 1)
        check("上限 1：说明还剩多少", r["left"], 1)   # 该轮共 2 首待下，处理 1 首，剩 1

        # ---------- ⑤ dry：只算计划 ----------
        calls["down"].clear(); calls["up"].clear()
        base_cfg["monitor"] = {"on": True, "mode": "full", "since": 0, "batch": 20}
        r = await runner.monitor_playlists(dry=True)
        check("dry 标记", r.get("dry"), True)
        check("dry 不排队下载", calls["down"], [])
        check("dry 不排队上传", calls["up"], [])
        check("dry 也给出计划数量", (r["download"], r["upload"]), (2, 1))

        # ---------- ⑥ 开关关着 ----------
        base_cfg["monitor"] = {"on": False, "mode": "new", "since": 0, "batch": 20}
        r = await runner.monitor_playlists()
        check("关着时跳过", bool(r.get("skipped")), True)
    finally:
        pl_mod.index, ci_mod.index = real_pl, real_ci
        runner.SessionLocal = real_sess
        runner.enqueue_download_one, runner.enqueue_upload = real_dl, real_up
        config.load, config.save = real_load, real_save
        runner._ncm = real_ncm
        tmp.cleanup()


asyncio.run(main())
print("=" * 60)
print(f"通过 {len(OK)} 项，失败 {len(FAIL)} 项")
for x in FAIL:
    print("  ✗", x)
sys.exit(1 if FAIL else 0)
