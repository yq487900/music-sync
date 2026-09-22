"""上传链路自测：上传成功后有没有**立刻**登记进云盘索引

背景（真实踩到的）：云盘索引每 10 分钟才整表刷新一次，刚上传完的歌在刷到之前
会被页面上判定成「不在云盘」，看着像上传失败。修法是上传成功后立刻登记。

这个自测**不碰真实云盘**：把 cloud.upload 和数据库会话都换成假的，只验证 runner 的接线。
跑法（容器里）：python tests/upload_test.py
"""
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for cand in (ROOT, Path("/app"), Path.cwd()):
    if (cand / "app" / "runner.py").exists() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

import app.config as config                      # noqa: E402
import app.runner as runner                      # noqa: E402
from app.cloud_index import index                # noqa: E402
from app.queue import Task                       # noqa: E402

INDEX_PATH = Path("/data/cloud_index.json")
SID_SONG = "888800001"        # 假设的歌单正式 id
SID_CLOUD = "999900001"       # 假上传返回的云盘条目 id
TITLE, ARTIST = "自测用假歌", "自测歌手"

OK, FAIL = [], []


def check(name, got, want):
    if got == want:
        OK.append(name)
    else:
        FAIL.append(f"{name}: 得到 {got!r}，期望 {want!r}")


class FakeTrack:
    def __init__(self, path):
        self.id = 1
        self.platform_track_id = SID_SONG
        self.title = TITLE
        self.artist = ARTIST
        self.album = "自测专辑"
        self.pic_url = ""
        self.track_no = 0
        self.disc = 0
        self.duration = 0.0
        self.file_path = str(path)
        self.track_metadata = {}
        self.cloud_sid = ""
        self.cloud_state = ""
        self.cloud_at = ""
        self.cloud_error = ""


class FakeSession:
    def __init__(self, tr):
        self._tr = tr

    def query(self, *a, **k):
        return self

    def filter_by(self, **k):
        return self

    def first(self):
        return self._tr

    def commit(self):
        pass

    def close(self):
        pass


async def fake_upload(cookie, path):
    """假装网易云传成功了（真身：POST /cloud/upload 返回 200 + privateCloud.simpleSong.id）"""
    return {"code": 200, "privateCloud": {"simpleSong": {"id": int(SID_CLOUD), "name": TITLE}}}


def cleanup_keys():
    from app.cloud_index import _base_title, _first_artist, _norm
    index.entry_ids.discard(SID_CLOUD)
    base = _base_title(TITLE)
    ar = _first_artist(ARTIST)
    index.keys.discard(f"{base}|{ar}")
    index.keys.discard(f"{_norm(TITLE)}|{ar}")
    index.titles.discard(base)


async def main():
    # 索引快照：测完原样还回去（别把自测的假条目留在正式索引里）
    backup = INDEX_PATH.read_bytes() if INDEX_PATH.exists() else None
    cleanup_keys()

    try:
        check("起始状态：这首歌还没被登记", index.entry_ids.isdisjoint({SID_CLOUD}), True)
        check("起始状态：判定为不在云盘",
              index.lookup(SID_SONG, TITLE, ARTIST, SID_CLOUD), False)

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "自测用假歌.flac"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=1", "-ac", "1", str(f)],
                           check=True)
            tr = FakeTrack(f)

            # 打桩：数据库 + 上传接口
            runner.SessionLocal = lambda: FakeSession(tr)          # type: ignore[assignment]
            real_upload = runner.cloud.upload
            runner.cloud.upload = fake_upload                       # type: ignore[assignment]
            try:
                cfg = config.load()
                cfg["cloud"] = {"auto_upload": False, "calibrate": False}   # 跳过联网校准
                task = Task("upload", 1, TITLE, ARTIST)
                await runner._upload_runner(task, cfg)
            finally:
                runner.cloud.upload = real_upload                   # type: ignore[assignment]

            # 这里是直接调 runner（没经过队列），所以 state 仍是 queued；
            # 队列跑完会把 state 置成 done —— 这里看 runner 的产物：无错 + 记了来源
            check("上传无报错", task.error, "")
            check("任务来源=网易云云盘", task.source, "网易云云盘")
            check("库里记下云盘 id", tr.cloud_sid, SID_CLOUD)
            check("库里云盘状态=uploaded", tr.cloud_state, "uploaded")
            check("上传后**立刻**判定为在云盘（本次修复的点）",
                  index.lookup(SID_SONG, TITLE, ARTIST, SID_CLOUD), True)
            check("只按本地记的云盘 id 也能认",
                  index.lookup("100000000", "别的歌", "别的歌手", SID_CLOUD), True)
            saved = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            check("已落盘（容器重建也认得）", SID_CLOUD in (saved.get("entry_ids") or []), True)
    finally:
        # 还原索引
        cleanup_keys()
        if backup is not None:
            INDEX_PATH.write_bytes(backup)
        else:
            INDEX_PATH.unlink(missing_ok=True)
        index.sids, index.entry_ids, index.keys, index.titles = set(), set(), set(), set()
        index._load_disk()


asyncio.run(main())
print("=" * 60)
print(f"通过 {len(OK)} 项，失败 {len(FAIL)} 项")
for x in FAIL:
    print("  ✗", x)
sys.exit(1 if FAIL else 0)
