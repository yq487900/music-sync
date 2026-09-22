"""下载：流式写入临时文件，边下边算 MD5，结束时校验大小与摘要。"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

import aiohttp


class DownloadError(Exception):
    pass


def _safe_name(name: str) -> str:
    """去掉文件名里非法字符，避免 Windows/SMB 共享下写入失败"""
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name)).strip() or "track"


async def download(url: str, dest_dir: Path, filename: str,
                   progress: Optional[Callable[[int, int], None]] = None,
                   proxy: Optional[str] = None,
                   control: Optional[Any] = None) -> Tuple[Path, str, int]:
    """下载到临时文件，返回 (临时文件路径, md5, 字节数)

    control：可选的任务对象（见 app/queue.Task），提供暂停/取消能力；
    每个数据块写入前都会询问一次，因此暂停能立即生效。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest_dir), suffix=".part")
    os.close(fd)
    tmp = Path(tmp_name)
    md5 = hashlib.md5()
    size = 0

    async def gate() -> None:
        if control is not None:
            await control.gate()

    try:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            await gate()
            async with s.get(url, proxy=proxy, allow_redirects=True) as r:
                if r.status != 200:
                    raise DownloadError(f"HTTP {r.status}")
                total = int(r.headers.get("Content-Length") or 0)
                with open(tmp, "wb") as f:
                    async for chunk in r.content.iter_chunked(65536):
                        await gate()
                        f.write(chunk)
                        md5.update(chunk)
                        size += len(chunk)
                        if progress:
                            progress(size, total)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise DownloadError("下载内容为空")
    return tmp, md5.hexdigest(), size


def verify(path: Path, want_md5: str = "", want_size: int = 0) -> bool:
    """校验大小与 MD5（网易云直链给出的 md5 偶尔为空，此时只校验大小）"""
    try:
        if want_size and path.stat().st_size != want_size:
            return False
    except OSError:
        return False
    if want_md5:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest().lower() == str(want_md5).lower()
    return True


def move_into(src: Path, dest: Path) -> None:
    """移动到最终位置（跨设备时退化为复制+删除）"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    try:
        src.replace(dest)
    except OSError:
        import shutil
        shutil.move(str(src), str(dest))


# 常见音频容器magic（用于第三方音源取回的直链校验）
_MAGIC = [
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"),
    (b"\xff\xf3", "mp3"),
    (b"\xff\xf2", "mp3"),
    (b"fLaC", "flac"),
    (b"OggS", "ogg"),
    (b"RIFF", "wav"),
]


def sniff_ext(path: Path) -> str:
    """按文件头判断真实音频格式；返回空串表示不是音频（可能是 HTML/JSON 错误页）"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return ""
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    if head[4:8] == b"ftyp":   # m4a / mp4
        return "m4a"
    return ""


def read_duration(path: Path) -> float:
    """读音频时长（秒）；失败返回 0"""
    try:
        from tinytag import TinyTag
        return float(TinyTag.get(str(path)).duration or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def looks_like_preview(path: Path, expect_sec: float, tolerance: float = 15.0) -> bool:
    """判断是否为试听片段：实际时长明显短于预期（差得比容差还多）"""
    if expect_sec < tolerance * 2:
        return False
    got = read_duration(path)
    return bool(got) and got < expect_sec - tolerance
