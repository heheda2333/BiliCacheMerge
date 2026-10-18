#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站缓存合并器 (BiliCacheMerge)
================================
把 PiliPlus 的 DASH 缓存（video.m4s + audio.m4s）无损拼接成 MP4，
并把缓存里的 cover.jpg 作为封面（MP4 covr 原子）写入文件。

只处理 PiliPlus 的缓存目录结构；其他客户端未做测试。
只依赖 Python 标准库 + ffmpeg。GUI 使用 tkinter。

本项目由 DeepSeek Harness (DSH) 协助开发，作者不提供后续维护。

用法：
    python BiliCacheMerge.py
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

APP_TITLE = "B站缓存合并器"
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------- 路径 / 配置

if getattr(sys, "frozen", False):  # 打包成 exe 的情况
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
STATE_PATH = os.path.join(APP_DIR, "merge_state.json")


def _resolve_thumb_dir() -> str:
    """选一个真正可写的缩略图缓存目录（LocalAppData → 程序目录 → 临时目录）。"""
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "BiliCacheMerge", "thumbs"))
    candidates.append(os.path.join(APP_DIR, "thumbs"))
    candidates.append(os.path.join(tempfile.gettempdir(), "BiliCacheMerge", "thumbs"))
    for cand in candidates:
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".write_test")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("1")
            os.remove(probe)
            return cand
        except Exception:
            continue
    return tempfile.gettempdir()


THUMB_DIR = _resolve_thumb_dir()

DEFAULT_CONFIG: Dict[str, object] = {
    "input": "",
    "output": "",
    "ffmpeg": "",
    "embed_cover": True,
    "sidecar_cover": False,
    "faststart": False,
    "open_when_done": False,
    "delete_countdown": True,
    "sort": "名称",
    "filter": "全部",
}

VIDEO_EXT = ".mp4"
DELETE_COUNTDOWN_SECONDS = 5  # 删除确认的等待时间；可在设置里关掉


def load_json(path: str) -> Optional[dict]:
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc) as fh:
                return json.load(fh)
        except UnicodeDecodeError:
            continue
        except Exception:
            return None
    return None


def save_json(path: str, data) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


# ---------------------------------------------------------------- 命名规则

_ILLEGAL = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_SPACES = re.compile(r"\s+")
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_NAME_LEN = 120


def sanitize_name(text: str) -> str:
    """把任意标题变为 Windows 合法文件名片段。"""
    if not text:
        return ""
    text = _ILLEGAL.sub("_", str(text))
    text = _SPACES.sub(" ", text).strip(" .")
    if text.upper() in _RESERVED:
        text = "_" + text
    return text


def build_output_name(season: str, title: str, owner: str, vid: str) -> str:
    """命名规则：合集名（如果有）-标题-作者-av/bv号"""
    season = sanitize_name(season)
    title = sanitize_name(title) or "未命名视频"
    owner = sanitize_name(owner) or "未知作者"
    vid = sanitize_name(vid)

    head = f"{season}-" if season else ""
    tail = f"-{owner}-{vid}" if vid else f"-{owner}"
    budget = MAX_NAME_LEN - len(head) - len(tail)
    if budget < 12:
        budget = 12
    if len(title) > budget:
        title = title[:budget].rstrip(" ._-") or "未命名视频"
    return (head + title + tail).strip(" ._-") or "video"


def suggest_output_dir(input_dir: str) -> str:
    """没填输出地址时给一个默认位置：缓存目录旁边的 merged 文件夹。"""
    if not input_dir:
        return ""
    try:
        return os.path.join(os.path.dirname(os.path.normpath(input_dir)), "merged")
    except Exception:
        return ""


def token_info() -> dict:
    """读当前进程令牌：AppContainer 标志 + 完整性级别 RID。"""
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008,
                                         ctypes.byref(token)):
            return {"error": "无法打开进程令牌"}
        info: Dict[str, object] = {}
        try:
            value = wintypes.DWORD()
            size = wintypes.DWORD(4)
            if advapi32.GetTokenInformation(token, 29, ctypes.byref(value), size,
                                            ctypes.byref(size)):
                info["app_container"] = bool(value.value)

            size = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
            if size.value:
                buf = ctypes.create_string_buffer(size.value)
                if advapi32.GetTokenInformation(token, 25, buf, size, ctypes.byref(size)):
                    sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                    raw = ctypes.string_at(sid_ptr, 12)
                    info["integrity_rid"] = int.from_bytes(raw[8:12], "little")
        finally:
            kernel32.CloseHandle(token)
        return info
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


INTEGRITY_NAMES = {0x0000: "不可信", 0x1000: "低", 0x2000: "中",
                   0x3000: "高", 0x4000: "系统"}


def token_summary() -> str:
    """可读的令牌描述，写日志/诊断框用。"""
    info = token_info()
    if "error" in info:
        return f"读取失败：{info['error']}"
    parts = []
    if "app_container" in info:
        parts.append("AppContainer=" + ("是" if info["app_container"] else "否"))
    rid = info.get("integrity_rid")
    if isinstance(rid, int):
        parts.append("完整性=" + INTEGRITY_NAMES.get(rid, hex(rid)))
    return "，".join(parts) or "未知"


def limited_environment() -> bool:
    """是否在受限环境里跑：DSH 沙箱环境变量，或进程处于低完整性（写不了中完整性目录）。"""
    if any(os.environ.get(k) for k in ("DSH_SESSION_ID", "DSH_SHELL", "DSH_PROFILE")):
        return True
    rid = token_info().get("integrity_rid")
    return isinstance(rid, int) and rid <= 0x1000


def write_diagnostics(path: str) -> str:
    """对目标目录做几项写入测试，返回可读的诊断文本。"""
    lines = []
    for label, name in (("新建文件", "bilicache_probe.tmp"),
                        ("新建隐藏名文件", ".bilicache_probe.tmp")):
        probe = os.path.join(path, name)
        try:
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("x")
            os.remove(probe)
            lines.append(f"· {label}：成功")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"· {label}：{type(exc).__name__}: {exc}")
    sub = os.path.join(path, "bilicache_probe_dir")
    try:
        os.makedirs(sub, exist_ok=True)
        os.rmdir(sub)
        lines.append("· 新建文件夹：成功")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"· 新建文件夹：{type(exc).__name__}: {exc}")
    return "\n".join(lines)


def quick_writable(path: str) -> str:
    """返回空串表示可写，否则返回错误描述。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, "bilicache_probe.tmp")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.remove(probe)
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- 数据模型


@dataclass
class CacheItem:
    page_dir: str                      # .../avid/c_cid
    avid_dir: str                      # .../avid
    entry: dict = field(default_factory=dict)
    stream_dir: str = ""
    video_path: str = ""
    audio_path: str = ""
    cover_path: str = ""
    quality: str = ""
    duration_ms: int = 0
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    output_name: str = ""
    output_path: str = ""
    status: str = "new"                # new / merged / running / ok / error
    message: str = ""
    selected: tk.BooleanVar = None  # type: ignore[assignment]

    # -------- 元数据快捷属性
    @property
    def avid(self) -> str:
        v = self.entry.get("avid")
        if v:
            return str(v)
        return os.path.basename(self.avid_dir)

    @property
    def bvid(self) -> str:
        return str(self.entry.get("bvid") or "")

    @property
    def vid(self) -> str:
        return self.bvid or (f"av{self.avid}" if self.avid else "")

    @property
    def title(self) -> str:
        page = self.entry.get("page_data") or {}
        return str(self.entry.get("title") or page.get("part") or "")

    @property
    def part(self) -> str:
        page = self.entry.get("page_data") or {}
        return str(page.get("part") or "")

    @property
    def page_no(self) -> int:
        page = self.entry.get("page_data") or {}
        try:
            return int(page.get("page") or 1)
        except Exception:
            return 1

    @property
    def cache_time(self) -> float:
        """缓存时间：优先用 entry 里的下载/更新时间，其次用目录修改时间。"""
        for key in ("time_update_stamp", "time_create_stamp"):
            try:
                value = float(self.entry.get(key) or 0)
                if value > 0:
                    return value
            except Exception:
                continue
        try:
            return os.path.getmtime(self.page_dir)
        except OSError:
            return 0.0

    @property
    def date_text(self) -> str:
        stamp = self.cache_time
        if not stamp:
            return ""
        try:
            return time.strftime("%Y-%m-%d", time.localtime(stamp))
        except Exception:
            return ""

    @property
    def owner(self) -> str:
        return str(self.entry.get("owner_name") or "")

    @property
    def season(self) -> str:
        info = self.entry.get("season_info") or {}
        return str(info.get("title") or "")

    @property
    def season_index(self) -> int:
        info = self.entry.get("season_info") or {}
        try:
            return int(info.get("index") or 0)
        except Exception:
            return 0

    @property
    def quality_desc(self) -> str:
        desc = str(self.entry.get("quality_pithy_description") or "").strip()
        return desc or (f"{self.quality}P" if self.quality else "未知画质")

    @property
    def duration_text(self) -> str:
        return format_duration_ms(self.duration_ms)

    @property
    def size_text(self) -> str:
        return format_size(self.size_bytes)

    @property
    def resolution_text(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return ""

    @property
    def status_text(self) -> str:
        return {
            "new": "未合并",
            "merged": "已合并",
            "running": "合并中",
            "ok": "已完成",
            "error": "失败",
        }.get(self.status, self.status)

    @property
    def selected_state(self) -> bool:
        return bool(self.selected.get()) if self.selected is not None else False


def format_size(num: float) -> str:
    if not num:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{int(num)} B" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"


def format_duration_ms(ms: int) -> str:
    if not ms:
        return "--:--"
    total = int(round(ms / 1000.0))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------- 缓存扫描


def find_pages(root: str, skip_paths: List[str]) -> List[str]:
    """递归找出所有含 entry.json 的缓存分P目录。"""
    pages: List[str] = []
    skip = {os.path.normcase(os.path.abspath(p)) for p in skip_paths if p}
    for dirpath, dirnames, filenames in os.walk(root):
        cur = os.path.normcase(os.path.abspath(dirpath))
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".")
            and os.path.normcase(os.path.join(cur, d)) not in skip
        ]
        if "entry.json" in filenames:
            pages.append(dirpath)
            dirnames[:] = []  # 分P目录内部不再继续找
    return pages


def quality_dirs(page_dir: str) -> List[tuple]:
    """返回 [(画质标识, 目录绝对路径, 视频文件)]"""
    result = []
    try:
        names = os.listdir(page_dir)
    except OSError:
        return result
    for name in names:
        sub = os.path.join(page_dir, name)
        if not os.path.isdir(sub):
            continue
        video = os.path.join(sub, "video.m4s")
        if os.path.isfile(video):
            result.append((name, sub, video))
    return result


def pick_stream(page_dir: str, entry: dict) -> Optional[tuple]:
    """选择画质：优先 entry.video_quality，否则取数字最大的目录。"""
    cands = quality_dirs(page_dir)
    if not cands:
        return None
    preferred = str(entry.get("video_quality") or entry.get("type_tag") or "")
    for name, sub, video in cands:
        if name == preferred:
            return name, sub, video
    try:
        cands.sort(key=lambda x: int(x[0]))
    except Exception:
        cands.sort(key=lambda x: x[0])
    return cands[-1]


def find_cover(page_dir: str) -> str:
    for name in ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp"):
        p = os.path.join(page_dir, name)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    try:
        for name in os.listdir(page_dir):
            low = name.lower()
            if low.startswith("cover.") and os.path.isfile(os.path.join(page_dir, name)):
                return os.path.join(page_dir, name)
    except OSError:
        pass
    return ""


def dir_size(path: str, limit_files: int = 400) -> int:
    total = 0
    count = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for fn in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
                count += 1
                if count >= limit_files:
                    return total
    except OSError:
        pass
    return total


def scan_items(root: str, output_dir: str, state: dict) -> List[CacheItem]:
    items: List[CacheItem] = []
    skip = [output_dir] if output_dir else []
    for page_dir in find_pages(root, skip):
        entry = load_json(os.path.join(page_dir, "entry.json")) or {}
        if not isinstance(entry, dict):
            entry = {}
        item = CacheItem(page_dir=page_dir, avid_dir=os.path.dirname(page_dir), entry=entry)
        stream = pick_stream(page_dir, entry)
        if stream is None:
            continue  # 没有视频流，无法合并
        item.quality, item.stream_dir, item.video_path = stream
        audio = os.path.join(item.stream_dir, "audio.m4s")
        item.audio_path = audio if os.path.isfile(audio) else ""
        item.cover_path = find_cover(page_dir)
        try:
            item.duration_ms = int(entry.get("total_time_milli") or 0)
        except Exception:
            item.duration_ms = 0
        page_data = entry.get("page_data") or {}
        try:
            item.width = int(page_data.get("width") or 0)
            item.height = int(page_data.get("height") or 0)
        except Exception:
            pass
        item.size_bytes = dir_size(item.stream_dir)
        if item.cover_path:
            try:
                item.size_bytes += os.path.getsize(item.cover_path)
            except OSError:
                pass

        item.output_name = build_output_name(item.season, item.title, item.owner, item.vid)
        if item.page_no > 1 and item.part and item.part != item.title:
            item.output_name = build_output_name(
                item.season, f"{item.title} P{item.page_no}", item.owner, item.vid
            )
        item.output_path = (
            os.path.join(output_dir, item.output_name + VIDEO_EXT) if output_dir else ""
        )

        rec = state.get(page_dir) if isinstance(state, dict) else None
        rec_out = str((rec or {}).get("output") or "")
        if rec_out and os.path.isfile(rec_out):
            item.status = "merged"
            item.output_path = rec_out
        elif item.output_path and os.path.isfile(item.output_path) and os.path.getsize(item.output_path) > 0:
            item.status = "merged"
        items.append(item)

    items.sort(key=lambda it: (it.season_index, it.owner, it.output_name))
    return items


# ---------------------------------------------------------------- ffmpeg 定位


def app_relative(*parts: str) -> str:
    return os.path.join(APP_DIR, *parts)


def find_ffmpeg(configured: str = "") -> str:
    """按优先级找 ffmpeg。"""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    cands: List[str] = []
    if configured:
        cands.append(configured)
        if os.path.isdir(configured):
            cands.append(os.path.join(configured, exe))
    cands += [app_relative("bin", exe), app_relative(exe), app_relative("ffmpeg", "bin", exe)]
    found = shutil.which("ffmpeg")
    if found:
        cands.append(found)
    cands += [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links", exe),
        os.path.join(os.environ.get("ProgramData", ""), "chocolatey", "bin", exe),
        os.path.join(os.environ.get("USERPROFILE", ""), "scoop", "shims", exe),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\FormatFactory\ffmpeg.exe",
    ]
    for path in cands:
        if path and os.path.isfile(path):
            return os.path.abspath(path)

    # 常见安装根目录做一次有限深度搜索
    roots = [
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
    ]
    for base in roots:
        if not base or not os.path.isdir(base):
            continue
        base_depth = base.rstrip("\\/").count(os.sep)
        for dirpath, dirnames, filenames in os.walk(base):
            depth = dirpath.rstrip("\\/").count(os.sep) - base_depth
            if depth >= 4:
                dirnames[:] = []
            dirnames[:] = [
                d for d in dirnames
                if d.lower() not in ("windowsapps", "node_modules", "cache", "temp")
                and not d.startswith(".")
            ]
            if "ffmpeg.exe" in filenames:
                return os.path.join(dirpath, "ffmpeg.exe")
    return ""


FFMPEG_DOWNLOAD_URLS = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
]


def download_ffmpeg(dest_dir: str, progress_cb) -> str:
    """下载静态 ffmpeg 到 dest_dir，返回 exe 路径。"""
    os.makedirs(dest_dir, exist_ok=True)
    zip_path = os.path.join(dest_dir, "_ffmpeg_download.zip")
    target = os.path.join(dest_dir, "ffmpeg.exe")
    last_err = None
    for url in FFMPEG_DOWNLOAD_URLS:
        try:
            progress_cb(f"正在下载 ffmpeg：{url}")
            with urllib.request.urlopen(url, timeout=60) as resp, open(zip_path, "wb") as fh:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if total and done % (4 * 1024 * 1024) < 262144:
                        progress_cb(
                            f"下载 ffmpeg {done * 100 // total}%（{format_size(done)} / {format_size(total)}）"
                        )
            progress_cb("正在解压 ffmpeg…")
            with zipfile.ZipFile(zip_path) as zf:
                member = None
                for name in zf.namelist():
                    low = name.replace("\\", "/").lower()
                    if low.endswith("/bin/ffmpeg.exe") or low == "bin/ffmpeg.exe":
                        member = name
                        break
                if member is None:
                    raise RuntimeError("压缩包里没有 ffmpeg.exe")
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            try:
                os.remove(zip_path)
            except OSError:
                pass
            progress_cb("ffmpeg 下载完成")
            return target
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            progress_cb(f"下载失败：{exc}")
    raise RuntimeError(f"无法下载 ffmpeg：{last_err}")


# ---------------------------------------------------------------- 合并核心

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def popen_kwargs() -> dict:
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW
    return kw


def parse_hhmmss(text: str) -> float:
    try:
        h, m, s = text.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return 0.0


def build_merge_cmd(ffmpeg: str, item: CacheItem, cfg: dict, out_path: str) -> List[str]:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1"]
    cmd += ["-i", item.video_path]
    has_audio = bool(item.audio_path)
    if has_audio:
        cmd += ["-i", item.audio_path]
    use_cover = bool(cfg.get("embed_cover")) and bool(item.cover_path)
    if use_cover:
        cmd += ["-i", item.cover_path]

    cmd += ["-map", "0:v:0"]
    if has_audio:
        cmd += ["-map", "1:a:0"]
    if use_cover:
        cmd += ["-map", "2:v:0"]
    cmd += ["-c", "copy"]
    if use_cover:
        cmd += ["-disposition:v:1", "attached_pic"]

    for key, value in (
        ("title", item.title),
        ("artist", item.owner),
        ("album", item.season),
        ("comment", f"{item.vid}  https://www.bilibili.com/video/{item.bvid or 'av' + item.avid}"),
    ):
        if value:
            cmd += ["-metadata", f"{key}={value}"]
    if has_audio:
        cmd += ["-metadata:s:a:0", "language=chi"]
    if cfg.get("faststart"):
        cmd += ["-movflags", "+faststart"]
    cmd += [out_path]
    return cmd


def probe_duration(ffmpeg: str, path: str) -> float:
    """用 ffmpeg 读出媒体时长（秒），失败返回 0。"""
    kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, **kw,
        )
        text = (proc.stderr or "") + (proc.stdout or "")
        m = re.search(r"Duration:\s*(\d+:\d+:\d+(?:\.\d+)?)", text)
        if m:
            return parse_hhmmss(m.group(1))
    except Exception:
        pass
    return 0.0


# ---------------------------------------------------------------- 缩略图


def make_thumbnail(ffmpeg: str, cover: str, out_png: str, width: int, height: int) -> tuple:
    """生成缩略图，返回 (是否成功, 错误信息)。"""
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x15171c"
    )
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-i", cover, "-vf", vf, "-frames:v", "1", out_png]
    kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60, **kw)
        if proc.returncode == 0 and os.path.isfile(out_png):
            return True, ""
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        return False, err.splitlines()[-1] if err else f"ffmpeg 退出码 {proc.returncode}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def thumb_cache_path(base: str, cover: str, width: int, height: int) -> str:
    try:
        st = os.stat(cover)
        key = hashlib.sha1(
            f"{cover}|{st.st_mtime_ns}|{st.st_size}|{width}x{height}".encode("utf-8")
        ).hexdigest()
    except OSError:
        key = hashlib.sha1(cover.encode("utf-8")).hexdigest()
    return os.path.join(base, key + ".png")


# ---------------------------------------------------------------- 主题配色

PALETTE = {
    "bg": "#f2f3f5",
    "card": "#ffffff",
    "border": "#e3e5e8",
    "text": "#18191c",
    "sub": "#61666d",
    "accent": "#fb7299",
    "accent_dark": "#e45c85",
    "ok": "#2f9e63",
    "warn": "#e6a23c",
    "err": "#d93026",
    "hover": "#f7f8fa",
    "sel": "#fff4f8",
    "thumb_bg": "#e6e9ed",
}


def pick_font_family(root: tk.Misc) -> str:
    try:
        families = set(tkfont.families(root))
    except Exception:
        families = set()
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "PingFang SC", "Segoe UI"):
        if name in families:
            return name
    return "Segoe UI"


class HintEntry(ttk.Entry):
    """带灰色占位提示的输入框（Tk 没有原生 placeholder，用浅色标签模拟半透明提示）。"""

    def __init__(self, master, textvariable, hint: str, hint_font: str = "", **kwargs):
        super().__init__(master, textvariable=textvariable, **kwargs)
        self._var = textvariable
        self._hint = tk.Label(
            self, text=hint, fg="#a9aeb5", bg=PALETTE["card"],
            font=(hint_font or "Microsoft YaHei UI", 9),
            anchor="w", cursor="xterm",
        )
        self._hint.bind("<Button-1>", lambda e: self.focus_set())
        self.bind("<FocusIn>", lambda e: self._refresh(), add="+")
        self.bind("<FocusOut>", lambda e: self._refresh(), add="+")
        self._var.trace_add("write", lambda *a: self._refresh())
        self.after_idle(self._refresh)

    def _refresh(self):
        if not self.winfo_exists():
            return
        try:
            # 用变量判断，而不是 self.get()：trace 回调触发时控件里还是旧内容
            has_text = bool(str(self._var.get()).strip())
            focused = self.focus_get() is self
        except Exception:
            return
        if has_text or focused:
            self._hint.place_forget()
        else:
            self._hint.place(x=9, rely=0.5, anchor="w")
            self._hint.lift()


class CanvasCheck(tk.Canvas):
    """自绘勾选框：ttk 主题在高 DPI 下会把对勾渲染成“叉”，这里统一画成粉色圆角勾。"""

    def __init__(self, master, variable: tk.BooleanVar, bg: str = None,
                 scale: float = 1.0, command=None):
        self.size = max(16, int(round(17 * scale)))
        self.bg_color = bg or PALETTE["card"]
        super().__init__(master, width=self.size, height=self.size, bg=self.bg_color,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.variable = variable
        self.scale = scale
        self.command = command
        variable.trace_add("write", lambda *a: self.draw())
        self.bind("<Button-1>", self.toggle)
        self.draw()

    def set_bg(self, color: str):
        if color != self.bg_color:
            self.bg_color = color
            self.configure(bg=color)
            self.draw()

    def toggle(self, _event=None):
        self.variable.set(not self.variable.get())
        if self.command:
            self.command()

    def draw(self):
        try:
            self.delete("all")
        except tk.TclError:
            return
        s = self.size
        lw = max(1, int(round(self.scale)))
        if self.variable.get():
            self.create_rectangle(0.5, 0.5, s - 0.5, s - 0.5, fill=PALETTE["accent"],
                                  outline=PALETTE["accent"], width=lw)
            self.create_line(
                s * 0.26, s * 0.52, s * 0.43, s * 0.71, s * 0.76, s * 0.31,
                fill="#ffffff", width=max(2, int(round(2 * self.scale))),
                capstyle="round", joinstyle="round")
        else:
            self.create_rectangle(0.5, 0.5, s - 0.5, s - 0.5, fill="#ffffff",
                                  outline="#c4c8ce", width=lw)


# ---------------------------------------------------------------- 确认删除对话框


class ConfirmDeleteDialog(tk.Toplevel):
    def __init__(self, master, items: List[CacheItem], total_bytes: int, font: str,
                 countdown: int = 5):
        super().__init__(master)
        self.result = False
        self.countdown_total = max(0, int(countdown))
        self._countdown_left = 0
        self._countdown_job = None
        self.title("确认删除原缓存文件")
        self.configure(bg=PALETTE["card"])
        self.transient(master)
        self.resizable(False, False)
        self.grab_set()

        pad = 18
        tk.Label(
            self, text="⚠  即将永久删除原缓存文件", font=(font, 12, "bold"),
            fg=PALETTE["err"], bg=PALETTE["card"], anchor="w",
        ).pack(fill="x", padx=pad, pady=(pad, 6))

        tk.Label(
            self,
            text=(
                f"共 {len(items)} 个缓存，合计 {format_size(total_bytes)}。\n"
                "删除后这些缓存文件夹会被移除，输出目录里的 MP4 不受影响；\n"
                "如需再次合并必须重新下载缓存。"
            ),
            font=(font, 9), fg=PALETTE["sub"], bg=PALETTE["card"],
            justify="left", anchor="w", wraplength=580,
        ).pack(fill="x", padx=pad)

        box = tk.Frame(self, bg=PALETTE["bg"], highlightbackground=PALETTE["border"],
                       highlightthickness=1)
        box.pack(fill="both", expand=True, padx=pad, pady=10)
        listbox = tk.Listbox(
            box, height=min(10, max(4, len(items))), font=(font, 9),
            bg=PALETTE["card"], fg=PALETTE["text"], bd=0, highlightthickness=0, activestyle="none",
        )
        listbox.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb = ttk.Scrollbar(box, orient="vertical", command=listbox.yview, style="Vertical.TScrollbar")
        sb.pack(side="right", fill="y", pady=8, padx=(0, 4))
        listbox.configure(yscrollcommand=sb.set)
        for it in items:
            name = f"{it.output_name}{VIDEO_EXT}"
            if len(name) > 60:
                name = name[:59] + "…"
            listbox.insert("end", name)
            path = it.page_dir
            if len(path) > 62:
                path = "…" + path[-61:]
            listbox.insert("end", f"      {path}")
        listbox.configure(exportselection=False)
        listbox.xview_moveto(0)

        self.var_ack = tk.BooleanVar(value=False)
        ack_row = tk.Frame(self, bg=PALETTE["card"])
        ack_row.pack(anchor="w", padx=pad)
        self.ack_check = CanvasCheck(ack_row, self.var_ack, bg=PALETTE["card"],
                                     command=self._sync)
        self.ack_check.pack(side="left", padx=(0, 8))
        ack_label = tk.Label(
            ack_row, text="我知道我在干什么（上述缓存将被永久删除，且无法恢复）",
            font=(font, 9, "bold"), bg=PALETTE["card"], fg=PALETTE["text"],
        )
        ack_label.pack(side="left")
        ack_label.bind("<Button-1>", lambda e: self._toggle_ack())

        btns = tk.Frame(self, bg=PALETTE["card"])
        btns.pack(fill="x", padx=pad, pady=(10, pad))
        ttk.Button(btns, text="取消", style="Ghost.TButton", command=self._cancel).pack(side="right")
        self.btn_ok = ttk.Button(
            btns, text=f"确认删除 {len(items)} 项", style="Danger.TButton",
            command=self._ok, state="disabled",
        )
        self.btn_ok.pack(side="right", padx=(0, 8))
        self.hint = tk.Label(
            btns, text="勾选上面的选项后才能删除", font=(font, 8),
            bg=PALETTE["card"], fg=PALETTE["sub"], anchor="w",
        )
        self.hint.pack(side="left")

        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.update_idletasks()
        w = max(self.winfo_reqwidth(), 660)
        h = self.winfo_reqheight()
        x = master.winfo_rootx() + max(0, (master.winfo_width() - w) // 2)
        y = master.winfo_rooty() + max(0, (master.winfo_height() - h) // 3)
        self.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")

    def _toggle_ack(self):
        self.var_ack.set(not self.var_ack.get())
        self._sync()

    def _cancel_countdown(self):
        if self._countdown_job is not None:
            try:
                self.after_cancel(self._countdown_job)
            except Exception:
                pass
            self._countdown_job = None

    def _sync(self):
        """勾选后开始倒计时；倒计时结束前删除按钮保持禁用。"""
        self._cancel_countdown()
        if not self.var_ack.get():
            self._countdown_left = 0
            self.btn_ok.configure(state="disabled")
            self.hint.configure(text="勾选上面的选项后才能删除")
            return
        if self.countdown_total <= 0:
            self._countdown_left = 0
            self.btn_ok.configure(state="normal")
            self.hint.configure(text="")
            return
        self._countdown_left = self.countdown_total
        self.btn_ok.configure(state="disabled")
        self._tick()

    def _tick(self):
        if not self.winfo_exists():
            return
        if not self.var_ack.get():
            self._cancel_countdown()
            return
        if self._countdown_left <= 0:
            self.btn_ok.configure(state="normal")
            self.hint.configure(text="现在可以删除了")
            self._countdown_job = None
            return
        self.btn_ok.configure(state="disabled")
        self.hint.configure(text=f"请稍候：{self._countdown_left} 秒后才可以点击删除…")
        self._countdown_left -= 1
        self._countdown_job = self.after(1000, self._tick)

    def _ok(self):
        if not self.var_ack.get():
            return
        if self.countdown_total > 0 and self._countdown_left > 0:
            return
        self.result = True
        self.destroy()

    def _cancel(self):
        self._cancel_countdown()
        self.result = False
        self.destroy()


# ---------------------------------------------------------------- 设置对话框


class SettingsDialog(tk.Toplevel):
    def __init__(self, master, app: "App"):
        super().__init__(master)
        self.app = app
        self.result = None
        self.title("设置")
        self.configure(bg=PALETTE["card"])
        self.transient(master)
        self.resizable(False, False)
        self.grab_set()
        font = app.font
        pad = 18

        tk.Label(self, text="设置", font=(font, 13, "bold"), bg=PALETTE["card"],
                 fg=PALETTE["text"], anchor="w").pack(fill="x", padx=pad, pady=(pad, 2))
        tk.Label(self, text="修改后点“保存”，下次启动仍然生效。", font=(font, 9),
                 bg=PALETTE["card"], fg=PALETTE["sub"], anchor="w").pack(
            fill="x", padx=pad, pady=(0, 12))

        body = tk.Frame(self, bg=PALETTE["card"])
        body.pack(fill="x", padx=pad)

        tk.Label(body, text="ffmpeg 路径", font=(font, 9), bg=PALETTE["card"],
                 fg=PALETTE["text"], anchor="w").grid(row=0, column=0, sticky="w")
        self.var_ff = tk.StringVar(value=str(app.cfg.get("ffmpeg") or "") or app.ffmpeg_path)
        row = tk.Frame(body, bg=PALETTE["card"])
        row.grid(row=1, column=0, sticky="we", pady=(4, 4))
        ttk.Entry(row, textvariable=self.var_ff, width=56, style="TEntry").pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览", style="Ghost.TButton",
                   command=self._browse_ff).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="自动检测", style="Ghost.TButton",
                   command=self._auto_ff).pack(side="left", padx=(6, 0))
        self.hint = tk.Label(body, text="", font=(font, 8), bg=PALETTE["card"],
                             fg=PALETTE["sub"], anchor="w")
        self.hint.grid(row=2, column=0, sticky="w", pady=(0, 10))
        self._refresh_hint()

        self.var_embed = tk.BooleanVar(value=bool(app.cfg.get("embed_cover", True)))
        self.var_sidecar = tk.BooleanVar(value=bool(app.cfg.get("sidecar_cover", False)))
        self.var_fast = tk.BooleanVar(value=bool(app.cfg.get("faststart", False)))
        self.var_open = tk.BooleanVar(value=bool(app.cfg.get("open_when_done", False)))
        self.var_countdown = tk.BooleanVar(value=bool(app.cfg.get("delete_countdown", True)))
        for i, (text, var) in enumerate([
            ("把 cover.jpg 写入 MP4 封面（covr，多数播放器 / 媒体库可见）", self.var_embed),
            ("同时在输出目录保存同名 cover.jpg", self.var_sidecar),
            ("使用 faststart（网页边下边播更顺，大文件会多花时间）", self.var_fast),
            ("合并完成后自动打开输出目录", self.var_open),
            (f"删除原文件前等待 {DELETE_COUNTDOWN_SECONDS} 秒（防手滑，关掉后勾选即可删除）",
             self.var_countdown),
        ]):
            opt_row = tk.Frame(body, bg=PALETTE["card"])
            opt_row.grid(row=3 + i, column=0, sticky="w", pady=2)
            CanvasCheck(opt_row, var, bg=PALETTE["card"]).pack(side="left", padx=(0, 8))
            opt_label = tk.Label(opt_row, text=text, font=(font, 9),
                                 bg=PALETTE["card"], fg=PALETTE["text"])
            opt_label.pack(side="left")
            opt_label.bind("<Button-1>", lambda e, v=var: v.set(not v.get()))

        btns = tk.Frame(self, bg=PALETTE["card"])
        btns.pack(fill="x", padx=pad, pady=(16, pad))
        ttk.Button(btns, text="取消", style="Ghost.TButton", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="保存", style="Accent.TButton", command=self._save).pack(
            side="right", padx=(0, 8))
        self.btn_dl = ttk.Button(btns, text="下载 ffmpeg", style="Ghost.TButton",
                                 command=self._download)
        self.btn_dl.pack(side="left")

        self.update_idletasks()
        w = max(self.winfo_reqwidth(), 700)
        h = self.winfo_reqheight()
        x = master.winfo_rootx() + max(0, (master.winfo_width() - w) // 2)
        y = master.winfo_rooty() + max(0, (master.winfo_height() - h) // 4)
        self.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")

    def _refresh_hint(self):
        path = self.var_ff.get().strip()
        if path and os.path.isfile(path):
            self.hint.configure(text=f"已找到：{path}", fg=PALETTE["ok"])
        elif path:
            self.hint.configure(text="路径无效：文件不存在", fg=PALETTE["err"])
        else:
            self.hint.configure(text="未指定，将自动在程序目录 / PATH / 常见位置查找", fg=PALETTE["sub"])

    def _browse_ff(self):
        path = filedialog.askopenfilename(
            title="选择 ffmpeg.exe",
            filetypes=[("ffmpeg", "ffmpeg.exe"), ("可执行文件", "*.exe"), ("所有文件", "*.*")],
        )
        if path:
            self.var_ff.set(path)
            self._refresh_hint()

    def _auto_ff(self):
        found = find_ffmpeg("")
        if found:
            self.var_ff.set(found)
            self._refresh_hint()
        else:
            messagebox.showwarning("未找到", "没有自动找到 ffmpeg.exe，请手动指定或点“下载 ffmpeg”。",
                                   parent=self)

    def _download(self):
        dest = app_relative("bin")
        if not messagebox.askyesno(
            "下载 ffmpeg",
            f"将从网络下载静态版 ffmpeg 到：\n{dest}\n\n下载量约 30–100 MB，是否继续？",
            parent=self,
        ):
            return
        self.btn_dl.configure(state="disabled", text="下载中…")

        def work():
            try:
                path = download_ffmpeg(
                    dest, lambda msg: self.app.events.put(("log", msg))
                )
                self.app.events.put(("ffmpeg_ready", path, ""))
            except Exception as exc:  # noqa: BLE001
                self.app.events.put(("ffmpeg_ready", "", str(exc)))

        threading.Thread(target=work, daemon=True).start()
        self.after(500, self._poll_download)

    def _poll_download(self):
        if not self.winfo_exists():
            return
        if self.app.ffmpeg_path:
            self.var_ff.set(self.app.ffmpeg_path)
            self._refresh_hint()
            self.btn_dl.configure(state="normal", text="下载 ffmpeg")
            return
        self.after(800, self._poll_download)

    def _save(self):
        self.result = {
            "ffmpeg": self.var_ff.get().strip(),
            "embed_cover": self.var_embed.get(),
            "sidecar_cover": self.var_sidecar.get(),
            "faststart": self.var_fast.get(),
            "open_when_done": self.var_open.get(),
            "delete_countdown": self.var_countdown.get(),
        }
        self.destroy()


# ---------------------------------------------------------------- 列表行


class ItemRow:
    def __init__(self, parent: tk.Frame, app: "App", item: CacheItem):
        self.app = app
        self.item = item
        p = PALETTE
        self.frame = tk.Frame(parent, bg=p["card"], height=app.row_height)
        self.frame.pack(fill="x")
        self.frame.pack_propagate(False)

        self.inner = tk.Frame(self.frame, bg=p["card"])
        self.inner.pack(fill="both", expand=True, padx=10, pady=6)

        if item.selected is None:
            item.selected = tk.BooleanVar(value=True)
        self.check = CanvasCheck(
            self.inner, item.selected, bg=p["card"], scale=app.scale,
            command=self.app.on_selection_changed,
        )
        self.check.pack(side="left", padx=(2, 10))

        self.thumb_box = tk.Frame(
            self.inner, bg=p["thumb_bg"], width=app.thumb_w, height=app.thumb_h
        )
        self.thumb_box.pack(side="left", padx=(0, 12))
        self.thumb_box.pack_propagate(False)
        self.thumb = tk.Label(
            self.thumb_box, bg=p["thumb_bg"], text="封面", fg="#9aa0a6",
            font=(app.font, 8), bd=0,
        )
        self.thumb.pack(fill="both", expand=True)

        self.text_box = tk.Frame(self.inner, bg=p["card"])
        self.text_box.pack(side="left", fill="both", expand=True)

        self.full_name = item.output_name + VIDEO_EXT
        self.name_label = tk.Label(
            self.text_box, text=self.full_name, font=app.name_font,
            fg=p["text"], bg=p["card"], anchor="w", justify="left",
        )
        self.name_label.pack(fill="x")

        detail = " · ".join(x for x in [
            item.duration_text,
            item.resolution_text,
            item.quality_desc,
            item.size_text,
            f"P{item.page_no}" if item.page_no > 1 else "",
            item.date_text,
        ] if x)
        self.full_detail = detail
        self.sub_label = tk.Label(
            self.text_box, text=detail, font=app.sub_font, fg=p["sub"], bg=p["card"], anchor="w",
        )
        self.sub_label.pack(fill="x", pady=(3, 0))

        self.status_label = tk.Label(
            self.inner, text=item.status_text, font=(app.font, 9, "bold"),
            fg=app.status_color(item), bg=p["card"], anchor="e", width=6,
        )
        self.status_label.pack(side="right", padx=(8, 2))

        self.sep = tk.Frame(parent, bg=p["border"], height=1)
        self.sep.pack(fill="x", padx=10)

        for widget in (self.frame, self.inner, self.text_box, self.name_label,
                       self.sub_label, self.thumb, self.thumb_box):
            widget.bind("<Button-1>", self._toggle, add="+")
            widget.bind("<Enter>", self._hover_on, add="+")
            widget.bind("<Leave>", self._hover_off, add="+")
            widget.bind("<Double-Button-1>", self._double, add="+")

        self.refresh()

    def _toggle(self, _event=None):
        self.item.selected.set(not self.item.selected.get())
        self.app.on_selection_changed()

    def _double(self, _event=None):
        self.app.open_item(self.item)

    def _hover_on(self, _event=None):
        if not self.item.selected.get():
            for w in (self.frame, self.inner, self.text_box, self.name_label,
                      self.sub_label, self.status_label):
                w.configure(bg=PALETTE["hover"])
            self.check.set_bg(PALETTE["hover"])

    def _hover_off(self, _event=None):
        self.refresh()

    def refresh(self):
        self.status_label.configure(text=self.item.status_text, fg=self.app.status_color(self.item))
        bg = PALETTE["sel"] if self.item.selected.get() else PALETTE["card"]
        for w in (self.frame, self.inner, self.text_box, self.name_label,
                  self.sub_label, self.status_label):
            w.configure(bg=bg)
        self.check.set_bg(bg)

    def set_image(self, image: tk.PhotoImage):
        self.photo = image
        self.thumb.configure(image=image, text="")

    def set_placeholder(self):
        """取不到封面时用「标题首字 + 色块」占位，比纯文字好看也便于区分。"""
        title = self.item.title or self.item.output_name or "?"
        first = next((c for c in title if c not in "【】[]()（）《》<>·:： " and c.strip()),
                     None)
        colors = ("#ef9aa8", "#8fb7e8", "#8fcfa8", "#e6b17e", "#a99ae0", "#74bfc4")
        color = colors[sum(map(ord, self.item.output_name or "x")) % len(colors)]
        try:
            self.thumb.configure(
                image="", text=first or "?", bg=color, fg="#ffffff",
                font=(self.app.font, max(10, int(16 * self.app.scale)), "bold"),
            )
        except Exception:
            pass

    @staticmethod
    def _fit(font_obj: tkfont.Font, text: str, avail: int) -> str:
        if avail <= 20 or font_obj.measure(text) <= avail:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font_obj.measure(text[:mid] + "…") <= avail:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip() + "…"

    def apply_width(self, avail: int):
        """按可用宽度截断文字，避免整行被撑出列表可视区域。"""
        name = self._fit(self.app.name_font, self.full_name, avail)
        if self.name_label.cget("text") != name:
            self.name_label.configure(text=name)
        detail = self._fit(self.app.sub_font, self.full_detail, avail)
        if self.sub_label.cget("text") != detail:
            self.sub_label.configure(text=detail)

    def destroy(self):
        try:
            self.frame.destroy()
            self.sep.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------- 主界面


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.configure(bg=PALETTE["bg"])
        self.title(f"{APP_TITLE} v{APP_VERSION}")
        self.minsize(1020, 680)

        self.cfg = dict(DEFAULT_CONFIG)
        loaded = load_json(CONFIG_PATH)
        if isinstance(loaded, dict):
            self.cfg.update({k: v for k, v in loaded.items() if k in DEFAULT_CONFIG})
        self.state_file = load_json(STATE_PATH) or {}
        if not isinstance(self.state_file, dict):
            self.state_file = {}

        self.font = pick_font_family(self)
        self.name_font = tkfont.Font(family=self.font, size=10)
        self.sub_font = tkfont.Font(family=self.font, size=8)
        self.scale = self._detect_scale()
        self.sandboxed = limited_environment()
        self.thumb_w = int(150 * self.scale)
        self.thumb_h = int(84 * self.scale)
        self.row_height = self.thumb_h + int(24 * self.scale)

        self.items: List[CacheItem] = []
        self.rows: Dict[str, ItemRow] = {}
        self.images: List[tk.PhotoImage] = []
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.stop_flag = threading.Event()
        self.merging = False
        self.scanning = False
        self.ffmpeg_path = find_ffmpeg(str(self.cfg.get("ffmpeg") or ""))
        self.thumb_pool = ThreadPoolExecutor(max_workers=4)
        self._thumb_pending: set = set()
        self._resize_job = None

        self._build_styles()
        self._build_ui()
        self._apply_ffmpeg_state()
        self._center_window()
        self.after(80, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<MouseWheel>", self._on_wheel)
        self.bind_all("<Control-a>", lambda e: self.set_selection("all"))

        last_input = str(self.cfg.get("input") or "")
        self.var_input.set(last_input if os.path.isdir(last_input) else "")
        self.var_output.set(str(self.cfg.get("output") or ""))
        if not self.var_output.get().strip() and self.var_input.get():
            self.var_output.set(suggest_output_dir(self.var_input.get()))
        self.log(f"启动方式：{'受限环境（继承 DSH 沙箱，只能写程序目录）' if self.sandboxed else '正常'}；"
                 f"进程令牌：{token_summary()}")
        self._write_startup_diag()
        if self.var_input.get():
            self.after(250, self.scan)
        else:
            self.set_status("请选择缓存目录（例如 ...\\com.example\\piliplus\\download）")

    # ------------------------------------------------------------ 外观

    def _detect_scale(self) -> float:
        try:
            return max(1.0, min(2.5, self.winfo_fpixels("1i") / 96.0))
        except Exception:
            return 1.0

    def _center_window(self):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(int(1240 * self.scale), sw - 80)
        h = min(int(860 * self.scale), sh - 120)
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build_styles(self):
        p = PALETTE
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        f = self.font
        style.configure(".", font=(f, 9), background=p["bg"], foreground=p["text"])
        style.configure("TFrame", background=p["bg"])
        style.configure("TLabel", background=p["bg"], foreground=p["text"], font=(f, 9))
        style.configure("Card.TCheckbutton", background=p["card"], foreground=p["text"],
                        font=(f, 9), focuscolor=p["card"])
        style.map("Card.TCheckbutton", background=[("active", p["card"])],
                  indicatorcolor=[("selected", p["accent"]), ("!selected", "#ffffff")])

        style.configure(
            "TEntry", fieldbackground=p["card"], foreground=p["text"],
            bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
            insertcolor=p["text"], padding=(8, 6), relief="flat",
        )
        style.map("TEntry", bordercolor=[("focus", p["accent"])])

        style.configure(
            "TButton", background="#ffffff", foreground=p["text"], font=(f, 9),
            bordercolor=p["border"], focuscolor=p["border"], relief="flat",
            padding=(14, 6), lightcolor=p["card"], darkcolor=p["card"],
        )
        style.map("TButton", background=[("active", p["hover"]), ("disabled", "#f5f5f6")],
                  foreground=[("disabled", "#b5b9bf")])

        style.configure(
            "Accent.TButton", background=p["accent"], foreground="#ffffff",
            font=(f, 9, "bold"), bordercolor=p["accent"], padding=(16, 7),
            lightcolor=p["accent"], darkcolor=p["accent"],
        )
        style.map("Accent.TButton",
                  background=[("active", p["accent_dark"]), ("disabled", "#f7c9d6")],
                  foreground=[("disabled", "#ffffff")])

        style.configure(
            "Danger.TButton", background=p["err"], foreground="#ffffff",
            font=(f, 9, "bold"), bordercolor=p["err"], padding=(16, 7),
            lightcolor=p["err"], darkcolor=p["err"],
        )
        style.map("Danger.TButton", background=[("active", "#b8261d"), ("disabled", "#f0b3af")])

        style.configure("Ghost.TButton", background="#ffffff", foreground=p["sub"],
                        padding=(12, 6), bordercolor=p["border"],
                        lightcolor=p["card"], darkcolor=p["card"])
        style.map("Ghost.TButton", background=[("active", p["hover"])],
                  foreground=[("disabled", "#c3c7cc")])

        style.configure("TProgressbar", background=p["accent"], troughcolor="#e9ebee",
                        bordercolor="#e9ebee", lightcolor=p["accent"], darkcolor=p["accent"])
        style.configure("Vertical.TScrollbar", background="#d6d9de", troughcolor=p["bg"],
                        bordercolor=p["bg"], arrowcolor=p["sub"], width=10)
        style.map("Vertical.TScrollbar", background=[("active", "#c0c4ca")])
        style.configure("TCombobox", fieldbackground=p["card"], background=p["card"],
                        bordercolor=p["border"], arrowcolor=p["sub"], padding=(6, 4))
        style.map("TCombobox", fieldbackground=[("readonly", p["card"])])

    def _build_ui(self):
        p = PALETTE
        f = self.font

        # ---- 顶部标题条
        header = tk.Frame(self, bg=p["card"], height=int(62 * self.scale))
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Frame(self, bg=p["border"], height=1).pack(fill="x")

        if self.sandboxed:
            warn = tk.Frame(self, bg="#fff4e2")
            warn.pack(fill="x")
            tk.Label(
                warn,
                text=("⚠ 当前程序运行在受限环境（低完整性令牌）：只能写入程序所在的目录，"
                      "写其他盘 / C 盘会被系统拒绝。要正常写到别处，请关掉本窗口，"
                      "用资源管理器（Win+E）找到程序文件夹，自己双击 BiliCacheMerge.exe。"),
                font=(f, 8), bg="#fff4e2", fg="#8a5a00", anchor="w", justify="left",
                wraplength=int(1180 * self.scale),
            ).pack(fill="x", padx=14, pady=6)
            tk.Frame(self, bg="#f0dcb8", height=1).pack(fill="x")

        title_box = tk.Frame(header, bg=p["card"])
        title_box.pack(side="left", padx=18)
        tk.Label(title_box, text=APP_TITLE, font=(f, 14, "bold"),
                 bg=p["card"], fg=p["text"]).pack(side="left")
        tk.Label(title_box, text=f" v{APP_VERSION}", font=(f, 8),
                 bg=p["card"], fg=p["sub"]).pack(side="left", pady=(4, 0))

        right = tk.Frame(header, bg=p["card"])
        right.pack(side="right", padx=18)
        self.ff_badge = tk.Label(right, text="ffmpeg", font=(f, 8), bg="#eef0f2",
                                 fg=p["sub"], padx=8, pady=3, cursor="hand2")
        self.ff_badge.pack(side="right", padx=(8, 0))
        self.ff_badge.bind("<Button-1>", lambda e: self.open_settings())
        ttk.Button(right, text="设置", style="Ghost.TButton",
                   command=self.open_settings).pack(side="right")

        # ---- 路径区
        path_card = tk.Frame(self, bg=p["card"], highlightbackground=p["border"],
                             highlightthickness=1)
        path_card.pack(fill="x", padx=14, pady=(12, 8))

        self.var_input = tk.StringVar(value="")
        self.var_output = tk.StringVar(value="")

        row1 = tk.Frame(path_card, bg=p["card"])
        row1.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(row1, text="输入地址", font=(f, 9, "bold"), bg=p["card"],
                 fg=p["text"], width=8, anchor="w").pack(side="left")
        self.entry_input = HintEntry(
            row1, self.var_input,
            "点「浏览…」选择 PiliPlus 缓存目录，例如 …\\com.example\\piliplus\\download",
            hint_font=f,
        )
        self.entry_input.pack(side="left", fill="x", expand=True)
        ttk.Button(row1, text="浏览…", style="Ghost.TButton",
                   command=self.choose_input).pack(side="left", padx=(8, 0))
        self.btn_scan = ttk.Button(row1, text="扫描", style="Accent.TButton", command=self.scan)
        self.btn_scan.pack(side="left", padx=(8, 0))

        row2 = tk.Frame(path_card, bg=p["card"])
        row2.pack(fill="x", padx=14, pady=(0, 12))
        tk.Label(row2, text="输出地址", font=(f, 9, "bold"), bg=p["card"],
                 fg=p["text"], width=8, anchor="w").pack(side="left")
        self.entry_output = HintEntry(
            row2, self.var_output,
            "点「浏览…」选择输出目录，例如 D:\\B站视频",
            hint_font=f,
        )
        self.entry_output.pack(side="left", fill="x", expand=True)
        ttk.Button(row2, text="浏览…", style="Ghost.TButton",
                   command=self.choose_output).pack(side="left", padx=(8, 0))
        ttk.Button(row2, text="打开目录", style="Ghost.TButton",
                   command=self.open_output_dir).pack(side="left", padx=(8, 0))

        # ---- 工具栏
        toolbar = tk.Frame(self, bg=p["bg"])
        toolbar.pack(fill="x", padx=14, pady=(0, 6))
        for text, cmd in (
            ("全选", lambda: self.set_selection("all")),
            ("全不选", lambda: self.set_selection("none")),
            ("反选", lambda: self.set_selection("invert")),
            ("只选未合并", lambda: self.set_selection("unmerged")),
        ):
            ttk.Button(toolbar, text=text, style="Ghost.TButton", command=cmd).pack(
                side="left", padx=(0, 6))

        self.filter_var = tk.StringVar(value=str(self.cfg.get("filter") or "全部"))
        self.sort_var = tk.StringVar(value=str(self.cfg.get("sort") or "名称"))
        tk.Label(toolbar, text="筛选", font=(f, 8), bg=p["bg"], fg=p["sub"]).pack(
            side="left", padx=(10, 4))
        ttk.Combobox(toolbar, textvariable=self.filter_var, values=["全部", "未合并", "已合并"],
                     state="readonly", width=7).pack(side="left")
        tk.Label(toolbar, text="排序", font=(f, 8), bg=p["bg"], fg=p["sub"]).pack(
            side="left", padx=(10, 4))
        ttk.Combobox(toolbar, textvariable=self.sort_var,
                     values=["名称", "时间", "大小", "时长", "状态"],
                     state="readonly", width=7).pack(side="left")

        # 下拉框一改就重建列表（筛选 / 排序立即生效）
        self.filter_var.trace_add("write", lambda *a: self.rebuild_list())
        self.sort_var.trace_add("write", lambda *a: self.rebuild_list())

        self.count_label = tk.Label(toolbar, text="共 0 项 · 已选 0 项", font=(f, 9),
                                    bg=p["bg"], fg=p["sub"])
        self.count_label.pack(side="right")

        # ---- 列表
        list_wrap = tk.Frame(self, bg=p["card"], highlightbackground=p["border"],
                             highlightthickness=1)
        list_wrap.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        self.canvas = tk.Canvas(list_wrap, bg=p["card"], highlightthickness=0, bd=0)
        vbar = ttk.Scrollbar(list_wrap, orient="vertical", command=self.canvas.yview,
                             style="Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.list_inner = tk.Frame(self.canvas, bg=p["card"])
        self.list_window = self.canvas.create_window((0, 0), window=self.list_inner, anchor="nw")
        self.list_inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.empty_label = tk.Label(
            self.list_inner, text="还没有扫描到缓存\n请选择输入地址后点“扫描”",
            font=(f, 10), bg=p["card"], fg=p["sub"], justify="center",
        )

        # ---- 底部操作区
        bottom = tk.Frame(self, bg=p["card"], highlightbackground=p["border"],
                          highlightthickness=1)
        bottom.pack(fill="x", side="bottom")

        prog_row = tk.Frame(bottom, bg=p["card"])
        prog_row.pack(fill="x", padx=14, pady=(10, 4))
        self.progress = ttk.Progressbar(prog_row, mode="determinate", maximum=1000)
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_label = tk.Label(prog_row, text="就绪", font=(f, 8), bg=p["card"],
                                       fg=p["sub"], width=10, anchor="e")
        self.progress_label.pack(side="left", padx=(10, 0))

        action_row = tk.Frame(bottom, bg=p["card"])
        action_row.pack(fill="x", padx=14, pady=(2, 10))
        self.btn_merge = ttk.Button(action_row, text="合并选中", style="Accent.TButton",
                                    command=self.start_merge)
        self.btn_merge.pack(side="left")
        self.btn_stop = ttk.Button(action_row, text="停止", style="Ghost.TButton",
                                   command=self.stop_merge, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_delete = ttk.Button(action_row, text="删除已合并的原文件", style="Ghost.TButton",
                                     command=self.delete_merged)
        self.btn_delete.pack(side="left", padx=(18, 0))
        self.status_label = tk.Label(action_row, text="", font=(f, 9), bg=p["card"],
                                     fg=p["sub"], anchor="w")
        self.status_label.pack(side="left", padx=(18, 0))

        self.btn_log = ttk.Button(action_row, text="显示日志", style="Ghost.TButton",
                                  command=self.toggle_log)
        self.btn_log.pack(side="right")

        # ---- 日志面板（默认隐藏，位于操作区上方）
        self.log_frame = tk.Frame(self, bg="#1e2024", height=int(150 * self.scale))
        self.log_text = tk.Text(
            self.log_frame, height=7, bg="#1e2024", fg="#d7dae0", insertbackground="#fff",
            font=("Consolas", 9), bd=0, relief="flat", padx=10, pady=8, wrap="none",
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        log_sb = ttk.Scrollbar(self.log_frame, orient="vertical", command=self.log_text.yview,
                               style="Vertical.TScrollbar")
        log_sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_sb.set, state="disabled")

    def status_color(self, item: CacheItem) -> str:
        return {
            "new": PALETTE["sub"],
            "merged": PALETTE["ok"],
            "running": PALETTE["warn"],
            "ok": PALETTE["ok"],
            "error": PALETTE["err"],
        }.get(item.status, PALETTE["sub"])

    # ------------------------------------------------------------ 日志 / 状态

    def log(self, message: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def toggle_log(self):
        if self.log_frame.winfo_ismapped():
            self.log_frame.pack_forget()
            self.btn_log.configure(text="显示日志")
        else:
            self.log_frame.pack(fill="x", side="bottom")
            self.btn_log.configure(text="隐藏日志")

    def set_status(self, text: str):
        self.status_label.configure(text=text)

    def _on_wheel(self, event):
        widget = self.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if widget is self.canvas:
                self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
                return
            widget = getattr(widget, "master", None)

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self.list_window, width=event.width)
        if self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.after(120, self._apply_row_widths)

    def _apply_row_widths(self):
        self._resize_job = None
        width = self.canvas.winfo_width()
        if width <= 1:
            return
        avail = width - self.thumb_w - 142
        for row in self.rows.values():
            row.apply_width(avail)

    # ------------------------------------------------------------ 路径选择

    def choose_input(self):
        initial = self.var_input.get() or os.path.expanduser("~")
        path = filedialog.askdirectory(
            title="选择缓存根目录（例如 PiliPlus 的 download 文件夹）", initialdir=initial)
        if not path:
            return
        path = os.path.normpath(path)
        self.var_input.set(path)
        if not self.var_output.get().strip():
            self.var_output.set(suggest_output_dir(path))
        self.scan()

    def choose_output(self):
        initial = self.var_output.get() or os.path.expanduser("~")
        path = filedialog.askdirectory(title="选择输出目录", initialdir=initial)
        if path:
            self.var_output.set(os.path.normpath(path))
            self._update_buttons()

    # ------------------------------------------------------------ 输出目录校验

    def _check_writable(self, path: str) -> str:
        """真的写一个文件试试；返回错误描述，空字符串表示可写。"""
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return str(exc)
        probe = os.path.join(path, ".bilicache_writetest.tmp")
        try:
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
        except Exception as exc:  # noqa: BLE001
            return str(exc)
        return ""

    def _write_startup_diag(self) -> None:
        """每次启动往 diag.log 追加一行，方便判断是哪次启动、什么令牌。"""
        try:
            with open(os.path.join(APP_DIR, "diag.log"), "a", encoding="utf-8") as fh:
                fh.write(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 启动 "
                    f"python={sys.executable} "
                    f"token={token_summary()} "
                    f"limited={self.sandboxed} "
                    f"DSH_env={bool(os.environ.get('DSH_SESSION_ID'))}\n"
                )
        except Exception:
            pass

    def _write_diag(self, path: str, error: str) -> None:
        """把写权限诊断写到程序目录的 diag.log，方便排查。"""
        try:
            text = "\n".join([
                f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"python：{sys.executable}",
                f"打包：{getattr(sys, 'frozen', False)}",
                f"受限环境：{'是' if self.sandboxed else '否'}",
                f"进程令牌：{token_summary()}",
                f"目标目录：{path}",
                f"首次错误：{error}",
                "写入测试：",
                write_diagnostics(path),
                f"程序目录：{APP_DIR} -> {quick_writable(APP_DIR) or '可写'}",
                f"临时目录：{tempfile.gettempdir()} -> "
                f"{quick_writable(tempfile.gettempdir()) or '可写'}",
            ])
            with open(os.path.join(APP_DIR, "diag.log"), "a", encoding="utf-8") as fh:
                fh.write("\n" + text + "\n")
            self.log("已把诊断信息写入 diag.log")
        except Exception:
            pass

    def _prepare_output_dir(self) -> str:
        """确认输出目录存在且可写；不可写时给出原因并允许重选。"""
        output = self.var_output.get().strip()
        if not output:
            output = suggest_output_dir(self.var_input.get().strip())
            self.var_output.set(output)
        if not output:
            messagebox.showinfo("缺少输出地址", "请先填写输出地址。", parent=self)
            return ""

        output = os.path.normpath(output)
        self.var_output.set(output)
        error = self._check_writable(output)
        if error:
            self._write_diag(output, error)

        if error and self.sandboxed:
            alt = os.path.join(APP_DIR, "output")
            alt_error = self._check_writable(alt)
            message = (
                f"{output}\n\n{error}\n\n"
                "当前程序运行在「受限环境」里（低完整性令牌）：\n"
                "除了程序所在的目录，其他盘符 / C 盘都会被系统拒绝写入。\n"
                "这不是文件夹权限问题，也不是安全软件拦截。\n\n"
                "最彻底的办法：关掉本窗口，用资源管理器（Win+E）打开程序文件夹，\n"
                "自己双击 BiliCacheMerge.exe 重新启动（从聊天里的文件卡片打开会继承限制）。\n\n"
                f"诊断：{token_summary()}\n"
                f"{write_diagnostics(output)}\n\n"
            )
            if not alt_error:
                message += f"要临时先把输出改到程序目录吗？\n{alt}"
                if messagebox.askyesno("受限环境：输出目录不可写", message, parent=self):
                    self.var_output.set(alt)
                    self.log(f"输出目录已改为 {alt}（受限环境）")
                    return alt
                return ""
            messagebox.showerror("受限环境：输出目录不可写", message, parent=self)
            return ""

        attempts = 0
        while error and attempts < 3:
            attempts += 1
            if not messagebox.askyesno(
                "输出目录不可写",
                f"{output}\n\n{error}\n\n"
                f"诊断：{token_summary()}\n"
                f"{write_diagnostics(output)}\n\n"
                "常见原因：\n"
                "1. 安全软件（火绒 / 360 / Windows 受控文件夹访问）保护了该目录，\n"
                "   视频 / 文档 / 桌面 / 图片 这些文件夹默认受保护；\n"
                "2. 目录只读或当前用户没有写权限；\n"
                "3. 文件夹被别的程序占用。\n\n"
                "建议换一个普通目录（例如 D:\\B站视频）。要现在重新选择吗？",
                parent=self,
            ):
                return ""
            picked = filedialog.askdirectory(
                title="选择可写入的输出目录", initialdir=os.path.expanduser("~"))
            if not picked:
                return ""
            output = os.path.normpath(picked)
            self.var_output.set(output)
            error = self._check_writable(output)
        if error:
            messagebox.showerror("输出目录不可写", f"{output}\n\n{error}", parent=self)
            return ""
        return output

    def open_output_dir(self):
        path = self.var_output.get().strip()
        if not path:
            messagebox.showinfo("提示", "请先填写输出地址。", parent=self)
            return
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("无法创建输出目录", f"{path}\n{exc}", parent=self)
            return
        self._open_path(path)

    def _open_path(self, path: str, quiet: bool = False):
        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            if quiet:
                self.log(f"打开失败（已忽略）：{path} — {exc}")
            else:
                messagebox.showerror("打开失败", str(exc), parent=self)

    def open_item(self, item: CacheItem):
        if item.status in ("merged", "ok") and os.path.isfile(item.output_path):
            self._open_path(item.output_path)
        elif os.path.isdir(item.page_dir):
            self._open_path(item.page_dir)

    # ------------------------------------------------------------ 列表 / 选择

    def visible_items(self) -> List[CacheItem]:
        mode = self.filter_var.get()
        items = [i for i in self.items
                 if mode == "全部"
                 or (mode == "未合并" and i.status not in ("merged", "ok"))
                 or (mode == "已合并" and i.status in ("merged", "ok"))]
        key = self.sort_var.get()
        if key == "时间":
            items.sort(key=lambda i: i.cache_time, reverse=True)
        elif key == "大小":
            items.sort(key=lambda i: i.size_bytes, reverse=True)
        elif key == "时长":
            items.sort(key=lambda i: i.duration_ms, reverse=True)
        elif key == "状态":
            items.sort(key=lambda i: i.status)
        else:
            items.sort(key=lambda i: (i.season_index, i.owner, i.output_name))
        return items

    def set_selection(self, mode: str):
        for item in self.visible_items():
            if mode == "all":
                item.selected.set(True)
            elif mode == "none":
                item.selected.set(False)
            elif mode == "invert":
                item.selected.set(not item.selected.get())
            elif mode == "unmerged":
                item.selected.set(item.status not in ("merged", "ok"))
        self.on_selection_changed()

    def on_selection_changed(self):
        for row in self.rows.values():
            row.refresh()
        selected = [i for i in self.items if i.selected_state]
        total = sum(i.size_bytes for i in selected)
        self.count_label.configure(
            text=f"共 {len(self.items)} 项 · 已选 {len(selected)} 项 · {format_size(total)}")
        self._update_buttons()

    def _update_buttons(self):
        self.btn_scan.configure(
            state="disabled" if (self.merging or self.scanning) else "normal")
        if self.merging:
            self.btn_merge.configure(state="disabled")
            self.btn_delete.configure(state="disabled")
            return
        selected = [i for i in self.items if i.selected_state]
        self.btn_merge.configure(
            state="normal" if selected and self.ffmpeg_path else "disabled")
        merged = [i for i in self.items if i.status in ("merged", "ok")]
        self.btn_delete.configure(state="normal" if merged else "disabled")

    def rebuild_list(self):
        for row in list(self.rows.values()):
            row.destroy()
        self.rows.clear()
        for child in self.list_inner.winfo_children():
            if child is not self.empty_label:
                child.destroy()
        self.images.clear()

        items = self.visible_items()
        if not items:
            self.empty_label.configure(
                text="没有符合条件的缓存" if self.items
                else "还没有扫描到缓存\n请选择输入地址后点“扫描”")
            self.empty_label.pack(pady=60)
        else:
            self.empty_label.pack_forget()
            for item in items:
                self.rows[item.page_dir] = ItemRow(self.list_inner, self, item)
                self._request_thumb(item)
        self.canvas.yview_moveto(0)
        self.after_idle(self._apply_row_widths)
        self.on_selection_changed()

    # ------------------------------------------------------------ 扫描

    def scan(self):
        if self.worker and self.worker.is_alive():
            return
        if self.merging:
            messagebox.showinfo("正在合并", "请等待当前合并任务结束后再重新扫描。", parent=self)
            return
        root = self.var_input.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showwarning("路径无效", "请输入有效的缓存根目录。", parent=self)
            return
        output = self.var_output.get().strip()
        self.btn_scan.configure(state="disabled", text="扫描中…")
        self.scanning = True
        self.set_status("正在扫描缓存…")
        self.cfg["input"] = root
        if output:
            self.cfg["output"] = output
        save_json(CONFIG_PATH, self.cfg)

        previous = {i.page_dir: i.selected_state for i in self.items}
        state = self.state_file

        def work():
            try:
                items = scan_items(root, output, state)
                for it in items:
                    it._pre_selected = previous.get(  # type: ignore[attr-defined]
                        it.page_dir, it.status != "merged")
                self.events.put(("scan_done", items))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("scan_error", str(exc)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    # ------------------------------------------------------------ 缩略图

    def _request_thumb(self, item: CacheItem):
        if not item.cover_path or not self.ffmpeg_path:
            row = self.rows.get(item.page_dir)
            if row is not None:
                row.set_placeholder()
            return
        if item.page_dir in self._thumb_pending:
            return
        self._thumb_pending.add(item.page_dir)
        cover = item.cover_path
        w, h = self.thumb_w, self.thumb_h

        def work():
            path, error = "", ""
            try:
                path, error = self._make_thumb_with_fallback(cover, w, h)
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
            try:
                if error:
                    self.events.put(("log", f"封面生成失败：{error}"))
                self.events.put(("thumb", item.page_dir, path))
            except Exception:
                pass

        self.thumb_pool.submit(work)

    def _make_thumb_with_fallback(self, cover: str, w: int, h: int):
        """依次尝试几个可写的缓存目录生成缩略图，返回 (路径, 错误信息)。"""
        if os.path.splitext(cover)[1].lower() == ".png":
            return cover, ""
        bases = [os.path.join(APP_DIR, "thumbs"),
                 THUMB_DIR,
                 os.path.join(tempfile.gettempdir(), "BiliCacheMerge", "thumbs")]
        seen = set()
        last_error = "未知错误"
        for base in bases:
            if not base or base in seen:
                continue
            seen.add(base)
            try:
                os.makedirs(base, exist_ok=True)
            except Exception as exc:  # noqa: BLE001
                last_error = f"{base}：{exc}"
                continue
            out = thumb_cache_path(base, cover, w, h)
            if os.path.isfile(out):
                return out, ""
            ok, error = make_thumbnail(self.ffmpeg_path, cover, out, w, h)
            if ok:
                return out, ""
            last_error = f"{base}：{error}"
        return "", last_error

    # ------------------------------------------------------------ 事件循环

    def _poll_events(self):
        try:
            while True:
                kind, *payload = self.events.get_nowait()
                if kind == "scan_done":
                    self._on_scan_done(payload[0])
                elif kind == "scan_error":
                    self.scanning = False
                    self.btn_scan.configure(state="normal", text="扫描")
                    self.set_status("扫描失败")
                    messagebox.showerror("扫描失败", payload[0], parent=self)
                elif kind == "thumb":
                    self._on_thumb(payload[0], payload[1])
                elif kind == "log":
                    self.log(payload[0])
                elif kind == "progress":
                    self._on_progress(*payload)
                elif kind == "item_status":
                    self._on_item_status(*payload)
                elif kind == "merge_done":
                    self._on_merge_done(*payload)
                elif kind == "ffmpeg_ready":
                    self._on_ffmpeg_ready(payload[0], payload[1])
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _on_scan_done(self, items: List[CacheItem]):
        self.items = items
        self.scanning = False
        self.btn_scan.configure(state="normal", text="扫描")
        for it in items:
            it.selected = tk.BooleanVar(value=getattr(it, "_pre_selected", True))
        self.rebuild_list()
        merged = sum(1 for i in items if i.status in ("merged", "ok"))
        self.set_status(f"扫描完成：{len(items)} 个缓存，{merged} 个已合并")
        self.log(f"扫描完成：共 {len(items)} 个缓存，{merged} 个已合并")
        if items:
            self.log(f"ffmpeg：{self.ffmpeg_path or '未找到'}；缩略图目录：{THUMB_DIR}")
        self._update_buttons()

    def _on_thumb(self, page_dir: str, path: str):
        self._thumb_pending.discard(page_dir)
        row = self.rows.get(page_dir)
        if not row:
            return
        if not path:
            row.set_placeholder()
            return
        try:
            image = tk.PhotoImage(file=path)
            row.set_image(image)
            self.images.append(image)
        except Exception as exc:  # noqa: BLE001
            self.log(f"封面加载失败：{exc}")
            row.set_placeholder()

    # ------------------------------------------------------------ 合并

    def start_merge(self):
        if self.merging:
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("正在扫描", "扫描还没结束，请稍候再合并。", parent=self)
            return
        selected = [i for i in self.items if i.selected_state]
        if not selected:
            messagebox.showinfo("提示", "请先勾选要合并的视频。", parent=self)
            return
        if not self.ffmpeg_path:
            messagebox.showerror("缺少 ffmpeg",
                                 "没有找到 ffmpeg，请到“设置”里指定，或点“下载 ffmpeg”。",
                                 parent=self)
            return

        output = self._prepare_output_dir()
        if not output:
            return
        self.cfg["output"] = output
        save_json(CONFIG_PATH, self.cfg)

        used = set()
        for item in selected:
            name = item.output_name
            n = 2
            while name.lower() in used:
                name = f"{item.output_name} ({n})"
                n += 1
            used.add(name.lower())
            item.output_path = os.path.join(output, name + VIDEO_EXT)

        need = sum(i.size_bytes for i in selected)
        try:
            free = shutil.disk_usage(output).free
            if need > free * 0.98 and not messagebox.askyesno(
                "磁盘空间可能不足",
                f"预计需要 {format_size(need)}，输出盘剩余 {format_size(free)}。\n是否继续？",
                parent=self,
            ):
                return
        except OSError:
            pass

        todo = []
        skipped = 0
        for item in selected:
            if os.path.isfile(item.output_path) and os.path.getsize(item.output_path) > 0:
                item.status = "merged"
                self.state_file[item.page_dir] = {
                    "output": item.output_path,
                    "size": os.path.getsize(item.output_path),
                    "time": time.time(),
                }
                skipped += 1
            else:
                todo.append(item)
        if skipped:
            save_json(STATE_PATH, self.state_file)
            self.log(f"{skipped} 个已存在同名输出，直接标记为已合并")

        self.merging = True
        self.stop_flag.clear()
        self.btn_stop.configure(state="normal")
        self._update_buttons()
        self.set_status("开始合并…")
        self.progress.configure(value=0)
        self.progress_label.configure(text="0%")

        cfg = dict(self.cfg)
        ffmpeg = self.ffmpeg_path

        def work():
            succeeded, failed = [], []
            total = len(todo)
            for index, item in enumerate(todo):
                if self.stop_flag.is_set():
                    break
                self.events.put(("item_status", item.page_dir, "running", "合并中"))
                self.events.put(("progress", index / max(1, total),
                                 f"{index}/{total}  {item.output_name}"))
                ok, message = self._merge_one(ffmpeg, item, cfg, index, total)
                if ok:
                    succeeded.append(item)
                    self.events.put(("item_status", item.page_dir, "merged", "已完成"))
                else:
                    failed.append((item, message))
                    self.events.put(("item_status", item.page_dir, "error", message or "失败"))
            self.events.put(("merge_done", succeeded, failed, skipped))

        threading.Thread(target=work, daemon=True).start()

    def _merge_one(self, ffmpeg: str, item: CacheItem, cfg: dict, index: int, total: int):
        out_path = item.output_path
        tmp_path = out_path + ".part.mp4"
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        cmd = build_merge_cmd(ffmpeg, item, cfg, tmp_path)
        self.events.put(("log", "运行：" + subprocess.list2cmdline(cmd)))
        expected = item.duration_ms / 1000.0
        last_emit = 0.0
        try:
            proc = subprocess.Popen(
                cmd, text=True, encoding="utf-8", errors="replace", **popen_kwargs())
            err_lines: List[str] = []

            def drain():
                try:
                    for line in proc.stderr:  # type: ignore[union-attr]
                        err_lines.append(line.rstrip())
                except Exception:
                    pass

            threading.Thread(target=drain, daemon=True).start()
            for line in proc.stdout:  # type: ignore[union-attr]
                if self.stop_flag.is_set():
                    proc.kill()
                    break
                line = line.strip()
                if line.startswith("out_time=") and expected > 0:
                    seconds = parse_hhmmss(line.split("=", 1)[1])
                    now = time.time()
                    if now - last_emit > 0.25:
                        last_emit = now
                        frac = min(1.0, seconds / expected)
                        self.events.put((
                            "progress", (index + frac) / max(1, total),
                            f"{index}/{total}  {item.output_name}  {int(frac * 100)}%"))
            code = proc.wait()
            if self.stop_flag.is_set():
                raise RuntimeError("已停止")
            if code != 0:
                raise RuntimeError("\n".join(err_lines[-6:]) or f"ffmpeg 退出码 {code}")
            if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) == 0:
                raise RuntimeError("没有生成输出文件")
            os.replace(tmp_path, out_path)

            if cfg.get("sidecar_cover") and item.cover_path:
                try:
                    shutil.copyfile(item.cover_path, os.path.splitext(out_path)[0] + ".jpg")
                except Exception:
                    pass

            duration = probe_duration(ffmpeg, out_path)
            if expected > 2 and duration and abs(duration - expected) > max(3.0, expected * 0.03):
                self.events.put(("log",
                                 f"警告：{os.path.basename(out_path)} 时长 {duration:.1f}s，"
                                 f"缓存标记 {expected:.1f}s"))

            self.state_file[item.page_dir] = {
                "output": out_path,
                "size": os.path.getsize(out_path),
                "time": time.time(),
            }
            save_json(STATE_PATH, self.state_file)
            self.events.put(("log",
                             f"完成：{out_path}（{format_size(os.path.getsize(out_path))}）"))
            return True, ""
        except Exception as exc:  # noqa: BLE001
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            self.events.put(("log", f"失败：{item.output_name} — {exc}"))
            return False, str(exc)

    def _on_progress(self, fraction: float, text: str):
        self.progress.configure(value=max(0, min(1000, int(fraction * 1000))))
        self.progress_label.configure(text=f"{int(fraction * 100)}%")
        self.set_status(text)

    def _on_item_status(self, page_dir: str, status: str, message: str):
        for item in self.items:
            if item.page_dir == page_dir:
                item.status = status
                item.message = message
                break
        row = self.rows.get(page_dir)
        if row:
            row.refresh()
        self._update_buttons()

    def _on_merge_done(self, succeeded: List[CacheItem], failed, skipped: int = 0):
        self.merging = False
        self.btn_stop.configure(state="disabled")
        save_json(STATE_PATH, self.state_file)
        for item in succeeded:
            item.status = "merged"
        self.rebuild_list()
        self.progress.configure(value=1000 if succeeded else 0)
        self.progress_label.configure(text="100%" if succeeded and not failed else "完成")

        if not succeeded and not failed and skipped:
            summary = f"没有需要合并的视频（{skipped} 个输出已存在）"
        else:
            summary = f"合并完成：成功 {len(succeeded)} 个"
            if failed:
                summary += f"，失败 {len(failed)} 个"
        self.set_status(summary)
        self.log(summary)
        self._update_buttons()

        if failed:
            detail = "\n".join(f"· {it.output_name}：{msg}" for it, msg in failed[:8])
            hint = ""
            joined = " ".join(msg for _it, msg in failed)
            if "Permission denied" in joined or "Error opening output" in joined:
                hint = (
                    "\n\n提示：ffmpeg 无法写入输出目录。\n"
                    "常见原因是安全软件拦了写入：火绒 / 360 的「重要文件保护」，\n"
                    "或 Windows 的「受控文件夹访问」——视频 / 文档 / 桌面 等文件夹默认受保护。\n"
                    "解决办法：换一个普通目录（例如 D:\\B站视频），\n"
                    "或者把 bin\\ffmpeg.exe 加进安全软件的信任区 / 允许列表。"
                )
            messagebox.showwarning("部分失败", f"{summary}\n\n{detail}{hint}", parent=self)
        elif succeeded and self.cfg.get("open_when_done", False):
            # 自动打开只是顺带行为，失败了就写日志，不弹窗打扰
            path = self.var_output.get().strip()
            if path:
                self._open_path(path, quiet=True)

    def stop_merge(self):
        self.stop_flag.set()
        self.set_status("正在停止…")
        self.log("用户请求停止")

    # ------------------------------------------------------------ 删除原文件

    def delete_merged(self) -> None:
        if self.merging:
            return
        merged = [i for i in self.items
                  if i.status in ("merged", "ok") and os.path.isdir(i.page_dir)]
        if not merged:
            messagebox.showinfo("没有可删除的项", "还没有已合并完成的缓存。", parent=self)
            return

        total_bytes = sum(dir_size(i.page_dir) for i in merged)
        countdown = DELETE_COUNTDOWN_SECONDS if self.cfg.get("delete_countdown", True) else 0
        dlg = ConfirmDeleteDialog(self, merged, total_bytes, self.font, countdown=countdown)
        self.wait_window(dlg)
        if not dlg.result:
            return

        root = self.var_input.get().strip()
        root_norm = os.path.normcase(os.path.abspath(root)) if root else ""
        deleted = skipped = 0
        for item in merged:
            page_dir = os.path.abspath(item.page_dir)
            safe = True
            if root_norm:
                try:
                    safe = os.path.commonpath(
                        [os.path.normcase(page_dir), root_norm]) == root_norm
                except ValueError:
                    safe = False
            if not safe or not os.path.basename(page_dir).startswith("c_") or not os.path.isfile(
                os.path.join(page_dir, "entry.json")
            ):
                skipped += 1
                continue
            try:
                shutil.rmtree(page_dir)
                deleted += 1
                self.state_file.pop(item.page_dir, None)
                parent = os.path.dirname(page_dir)
                try:
                    if os.path.isdir(parent) and not os.listdir(parent):
                        os.rmdir(parent)
                except OSError:
                    pass
            except Exception as exc:  # noqa: BLE001
                self.log(f"删除失败：{page_dir} — {exc}")
                skipped += 1
        save_json(STATE_PATH, self.state_file)
        self.log(f"已删除 {deleted} 个缓存目录，跳过 {skipped} 个")
        self.scan()
        messagebox.showinfo("删除完成", f"已删除 {deleted} 个缓存目录。", parent=self)

    # ------------------------------------------------------------ 设置 / ffmpeg

    def open_settings(self):
        dlg = SettingsDialog(self, self)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        self.cfg.update(dlg.result)
        save_json(CONFIG_PATH, self.cfg)
        self.ffmpeg_path = find_ffmpeg(str(self.cfg.get("ffmpeg") or ""))
        self._apply_ffmpeg_state()
        self.log(f"设置已保存，ffmpeg：{self.ffmpeg_path or '未找到'}")

    def _on_ffmpeg_ready(self, path: str, error: str):
        if path and os.path.isfile(path):
            self.cfg["ffmpeg"] = path
            save_json(CONFIG_PATH, self.cfg)
            self.ffmpeg_path = path
            self._apply_ffmpeg_state()
            self.log(f"ffmpeg 已就绪：{path}")
            self.rebuild_list()  # 重新尝试生成缩略图
        else:
            self.log(f"ffmpeg 下载失败：{error}")
            messagebox.showerror("下载失败", error or "未知错误", parent=self)

    def _apply_ffmpeg_state(self):
        p = PALETTE
        if self.ffmpeg_path:
            self.ff_badge.configure(text="ffmpeg 就绪", bg="#eaf7f0", fg=p["ok"])
        else:
            self.ff_badge.configure(text="缺少 ffmpeg（点此设置）", bg="#fdecea", fg=p["err"])
        self._update_buttons()

    # ------------------------------------------------------------ 关闭

    def _on_close(self):
        if self.merging:
            if not messagebox.askyesno(
                "正在合并",
                "还有视频正在合并，现在退出会中断合并（未完成的临时文件会被丢弃）。\n确定退出吗？",
                parent=self,
            ):
                return
            self.stop_flag.set()
        try:
            self.cfg["input"] = self.var_input.get().strip()
            self.cfg["output"] = self.var_output.get().strip()
            self.cfg["sort"] = self.sort_var.get()
            self.cfg["filter"] = self.filter_var.get()
            save_json(CONFIG_PATH, self.cfg)
            save_json(STATE_PATH, self.state_file)
        except Exception:
            pass
        try:
            self.thumb_pool.shutdown(wait=False)
        except Exception:
            pass
        self.destroy()


def main():
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # 用 pythonw 启动时没有控制台，至少给个对话框
        import traceback

        detail = traceback.format_exc()
        try:
            with open(os.path.join(APP_DIR, "crash.log"), "w", encoding="utf-8") as fh:
                fh.write(detail)
        except Exception:
            pass
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb

            _root = _tk.Tk()
            _root.withdraw()
            _mb.showerror("启动失败", detail[-1800:])
            _root.destroy()
        except Exception:
            pass
        raise
