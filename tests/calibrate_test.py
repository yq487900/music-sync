"""上传后「按歌单数据校准」自测

跑法（容器里）：python tests/calibrate_test.py
它只在 /tmp 里造测试文件，不动音乐库、不动云盘；只读网易云接口。

覆盖：
  ① 封面占位图 / 太小 / 非图片 → 一律不采用
  ② 文件缺歌名/歌手/专辑/封面/歌词 → 按歌单数据补齐
  ③ 封面是别的图（比如被写成占位图/换错了）→ 换成歌单那张
  ④ 已经一致 → 不再改动文件（幂等）
"""
import asyncio
import hashlib
import http.server
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for cand in (ROOT, Path("/app"), Path.cwd()):
    if (cand / "app" / "runner.py").exists() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

import app.config as config          # noqa: E402
import app.organize as organize      # noqa: E402
import app.runner as runner          # noqa: E402

OK, FAIL = [], []


def check(name, got, want):
    if got == want:
        OK.append(name)
    else:
        FAIL.append(f"{name}: 得到 {got!r}，期望 {want!r}")


def ffmpeg(*args) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def make_flac(path: Path, seconds: int = 2) -> None:
    """造一个没有标签、没有封面的音频"""
    ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
           "-ac", "1", "-ar", "44100", str(path))


def make_image(path: Path, size: int, color: str) -> None:
    """纯色小图（体积很小，用来验证「太小不当封面」）"""
    ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s={size}x{size}:d=1", "-frames:v", "1", str(path))


def make_noisy_image(path: Path, size: int = 800) -> None:
    """噪声图（体积足够大，用来当「正常封面」）"""
    ffmpeg("-f", "lavfi",
           "-i", f"nullsrc=s={size}x{size},geq=random(1)*255:random(2)*255:random(3)*255",
           "-frames:v", "1", str(path))


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


class FakeTrack:
    """够用的 Track 替身（校准只读/写这几个字段）"""

    def __init__(self, sid: str, file_path: Path):
        self.id = 1
        self.platform_track_id = sid
        self.title = ""
        self.artist = ""
        self.album = ""
        self.pic_url = ""
        self.track_no = 0
        self.disc = 0
        self.duration = 0.0
        self.file_path = str(file_path)
        self.track_metadata = {}


class FakeDB:
    def commit(self):
        pass


async def main() -> None:
    cfg = config.load()
    cookie = ((cfg.get("platforms") or {}).get("netease") or {}).get("cookie") or ""
    if not cookie:
        print("⛔ 没登录网易云，跳过（需要真实接口才能核对）")
        return

    # 用歌单里的《奢香夫人》——网易云有正式条目（al.id≠0）、有封面、有歌词
    # （注意：云盘条目自己的 id 不算，那种 al.id=0，校准会正确跳过）
    sid = "354750"

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)

        # ---------- ① 占位图 / 太小的图不能当封面用 ----------
        srv_dir = tmpdir / "srv"
        srv_dir.mkdir()
        make_image(srv_dir / "tiny.jpg", 16, "gray")          # 纯色小图，< 8 KB
        make_noisy_image(srv_dir / "big.jpg")                 # 噪声图，体积够大
        (srv_dir / "notimage.txt").write_text("这是 HTML 错误页" * 500)

        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(srv_dir), **k)  # noqa: E731
        httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"

        check("太小的图不当封面", await runner._cover_from_playlist(f"{base}/tiny.jpg"), None)
        check("非图片不当封面", await runner._cover_from_playlist(f"{base}/notimage.txt"), None)
        big = await runner._cover_from_playlist(f"{base}/big.jpg")
        check("正常封面能取到（且是原图）",
              md5(big or b""), md5((srv_dir / "big.jpg").read_bytes()))
        check("空地址返回 None", await runner._cover_from_playlist(""), None)
        httpd.shutdown()

        # ---------- ② 缺标签/封面/歌词的文件 → 按歌单数据补齐 ----------
        f = tmpdir / "奢香夫人.flac"
        make_flac(f)
        tr = FakeTrack(sid, f)
        before = f.stat().st_mtime
        await runner._enrich_after_upload(cfg, cookie, tr, sid, FakeDB())   # sid==target → 不动云盘

        now = organize.inspect(f)
        check("歌名已按歌单写入", now["title"], "奢香夫人")
        check("歌手已按歌单写入", now["artist"], "凤凰传奇")
        check("专辑已按歌单写入", now["album"], "最炫民族风")
        check("封面已补上", now["has_cover"], True)
        check("歌词已补上", bool(organize.current_lyrics(f).strip()), True)
        check("外挂 lrc 已生成", f.with_suffix(".lrc").exists(), True)
        check("NFO 已生成", f.with_suffix(".nfo").exists(), True)
        check("文件确实被改过", f.stat().st_mtime != before, True)
        cover1 = organize.current_cover(f)
        check("取到歌单封面", bool(cover1), True)

        # ---------- ③ 封面被换成别的图 → 校准回歌单那张 ----------
        make_image(tmpdir / "wrong.jpg", 600, "red")
        wrong = (tmpdir / "wrong.jpg").read_bytes()
        organize.save_meta(f, {"title": now["title"], "artist": now["artist"],
                               "album": now["album"], "album_artist": now["artist"]}, cover=wrong)
        check("先确认封面被换成了红图",
              bool(organize.current_cover(f)) and md5(organize.current_cover(f)) != md5(cover1 or b""),
              True)
        await runner._enrich_after_upload(cfg, cookie, tr, sid, FakeDB())
        check("封面已校准回歌单那张", md5(organize.current_cover(f) or b""), md5(cover1 or b""))

        # ---------- ④ 已经一致 → 不动文件（幂等） ----------
        mtime = f.stat().st_mtime_ns
        await runner._enrich_after_upload(cfg, cookie, tr, sid, FakeDB())
        check("一致时不再改文件", f.stat().st_mtime_ns, mtime)

        # ---------- ⑤ 开关关掉就什么都不做 ----------
        g = tmpdir / "another.flac"
        make_flac(g)
        cfg2 = dict(cfg)
        cfg2["cloud"] = {"auto_upload": False, "calibrate": False}
        tr2 = FakeTrack(sid, g)
        mt = g.stat().st_mtime_ns
        await runner._enrich_after_upload(cfg2, cookie, tr2, sid, FakeDB())
        check("开关关闭时不改文件", g.stat().st_mtime_ns, mt)


asyncio.run(main())
print("=" * 60)
print(f"通过 {len(OK)} 项，失败 {len(FAIL)} 项")
for x in FAIL:
    print("  ✗", x)
sys.exit(1 if FAIL else 0)
