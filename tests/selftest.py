"""核心链路自测：音质协商、目录命名、下载校验、标签、NFO。

在容器内运行（见 tests/run.sh）：
    docker compose exec musicsync python tests/selftest.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, "/app")

CHAIN = ["jymaster", "hires", "lossless", "exhigh", "standard"]
META = {"artist": "周杰伦", "album": "范特西", "title": "爱在西元前", "track": 3, "pos": 3,
        "album_artist": "周杰伦", "sid": 123}

_ok: list = []
_bad: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (_ok if cond else _bad).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra else ""))


def test_quality() -> None:
    from app.quality import chain_from, pick_level, rank
    check("无会员账号降到 standard",
          pick_level({"maxBrLevel": "lossless", "dlLevel": "standard"}, CHAIN) == "standard")
    check("会员+母带歌曲 → jymaster",
          pick_level({"maxBrLevel": "jymaster", "dlLevel": "jymaster"}, CHAIN) == "jymaster")
    check("歌曲最高只到 hires → hires",
          pick_level({"maxBrLevel": "hires", "dlLevel": "jymaster"}, CHAIN) == "hires")
    check("无权限信息 → 取链首", pick_level(None, CHAIN) == "jymaster")
    check("降档序列从 lossless 起",
          chain_from("lossless", CHAIN) == ["lossless", "exhigh", "standard"])
    check("未知新档位排名 -1 不报错", rank("elite") == -1)


def test_paths() -> None:
    from app.tagger import default_naming, song_relative_path
    check("按专辑分类路径",
          str(song_relative_path(META, "album")) == "周杰伦/范特西/03. 爱在西元前")
    check("按歌手分类路径",
          str(song_relative_path(META, "artist")) == "周杰伦/03. 爱在西元前")
    check("平铺路径", str(song_relative_path(META, "flat")) == "03. 爱在西元前")
    check("按歌单分类路径",
          str(song_relative_path(dict(META, playlist="我的最爱"), "playlist"))
          == "我的最爱/03. 周杰伦 - 爱在西元前")
    check("非法字符被清理",
          "/" not in str(song_relative_path(dict(META, title="a/b:c*d?e"), "flat")))
    check("默认命名模板", default_naming("album") == "{track:02d}. {title}")
    # 没有音轨号时不应生成「00. 歌名」
    no_track = song_relative_path({"title": "无号曲目", "artist": "歌手", "album": "专辑"}, "album").name
    check("缺音轨号不出现 00 前缀", no_track == "无号曲目", no_track)
    has_track = song_relative_path(
        {"title": "有号曲目", "artist": "歌手", "album": "专辑", "track": 7}, "album").name
    check("有音轨号仍带序号", has_track == "07. 有号曲目", has_track)


async def test_download() -> None:
    from app.downloader import DownloadError, download, verify

    # 容器内三个进程同处一体，自测直接请求本机 Web 服务
    base = (os.environ.get("SELFTEST_BASE") or "http://127.0.0.1:13570").rstrip("/")
    tmp_dir = Path(tempfile.mkdtemp(dir="/data/tmp"))
    try:
        tmp, md5, size = await download(f"{base}/static/style.css", tmp_dir, "t")
        check("流式下载得到非空文件与 md5", size > 1000 and len(md5) == 32)
        check("大小校验通过", verify(tmp, "", size))
        check("大小不符能被识别", not verify(tmp, "", size + 1))
        check("md5 不符能被识别", not verify(tmp, "0" * 32, size))
        try:
            await download(f"{base}/no-such-file", tmp_dir, "x")
            check("404 抛 DownloadError", False)
        except DownloadError as e:
            check("404 抛 DownloadError", True, str(e))
    except Exception as e:  # noqa: BLE001
        check("下载链路可用", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_tagger_and_nfo() -> None:
    from mutagen.id3 import ID3
    from app.nfo import write_album_nfo, write_song_nfo
    from app.tagger import tag_file

    tmp_dir = Path(tempfile.mkdtemp(dir="/data/tmp"))
    mp3 = tmp_dir / "t.mp3"
    try:
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        str(mp3)], capture_output=True)
        check("ffmpeg 可用（能造测试音频）", mp3.exists())
        if not mp3.exists():
            return
        cover = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
        lrc = "[00:01.00]测试歌词"
        check("写标签未报错", tag_file(mp3, META, cover, lrc) is None)
        tags = ID3(str(mp3))
        check("标题写入正确", str(tags.get("TIT2")) == "爱在西元前")
        check("歌手写入正确", str(tags.get("TPE1")) == "周杰伦")
        check("专辑写入正确", str(tags.get("TALB")) == "范特西")
        check("音轨号写入正确", str(tags.get("TRCK")) == "3")
        uslt = tags.getall("USLT")
        check("歌词写入正确", bool(uslt) and "测试歌词" in str(uslt[0].text))
        check("封面写入正确", len(tags.getall("APIC")) == 1)

        # 接口没给音轨号时用文件自带的编号兜底
        from app.tagger import read_track_number
        from mutagen.id3 import TRCK

        check("读取文件自带音轨号", read_track_number(mp3) == (3, 0), str(read_track_number(mp3)))
        multi = tmp_dir / "multi.mp3"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        str(multi)], capture_output=True)
        mt = ID3()
        mt.add(TRCK(encoding=3, text="7/12"))
        mt.save(str(multi))
        check("「7/12」这种总分格式读出 7", read_track_number(multi) == (7, 0), str(read_track_number(multi)))
        plain = tmp_dir / "plain.mp3"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        str(plain)], capture_output=True)
        check("没有编号的文件读出 (0, 0)", read_track_number(plain) == (0, 0), str(read_track_number(plain)))

        write_song_nfo(tmp_dir / "t.nfo", META, 220)
        write_album_nfo(tmp_dir / "album.nfo", dict(META, genre="Pop", label="索尼", date="2001"))
        snfo = (tmp_dir / "t.nfo").read_text(encoding="utf-8")
        anfo = (tmp_dir / "album.nfo").read_text(encoding="utf-8")
        check("单曲 NFO 含标题/歌手/专辑",
              all(x in snfo for x in ("<title>爱在西元前</title>", "<artist>周杰伦</artist>", "<album>范特西</album>")))
        check("专辑 NFO 含流派/厂牌/年份",
              all(x in anfo for x in ("<genre>Pop</genre>", "<label>索尼</label>", "<year>2001</year>")))
        try:
            ET.fromstring(snfo)
            ET.fromstring(anfo)
            check("NFO 是合法 XML", True)
        except ET.ParseError as e:
            check("NFO 是合法 XML", False, str(e))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    test_quality()
    test_paths()
    asyncio.run(test_download())
    test_tagger_and_nfo()
    print(f"\n通过 {len(_ok)} 项，失败 {len(_bad)} 项")
    if _bad:
        print("失败项：" + "、".join(_bad))
    return 1 if _bad else 0


if __name__ == "__main__":
    sys.exit(main())
