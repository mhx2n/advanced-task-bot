"""Async video downloader using yt-dlp.
Supports YouTube, Facebook, Instagram, TikTok and 1000+ sites.
Concurrency-safe via per-task temp dirs. Capped to keep bot lightweight.
"""
import asyncio
import os
import re
import shutil
import tempfile
import time
from typing import Optional

import yt_dlp

# Telegram bot upload cap (~50 MB for regular bots)
MAX_BYTES = 49 * 1024 * 1024

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
SUPPORTED_HOSTS = (
    "youtube.com", "youtu.be", "facebook.com", "fb.watch",
    "instagram.com", "tiktok.com", "vt.tiktok.com", "twitter.com", "x.com",
)


def detect_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(").,]>")
    if any(h in url.lower() for h in SUPPORTED_HOSTS):
        return url
    return None


def _ydl_opts(outtmpl: str) -> dict:
    opts: dict = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Prefer compact mp4 under cap. Falls back gracefully.
        "format": (
            f"best[filesize<{MAX_BYTES}][ext=mp4]/"
            f"best[filesize<{MAX_BYTES}]/"
            "best[height<=720][ext=mp4]/best[height<=720]/best"
        ),
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 4,
        "retries": 3,
        "socket_timeout": 30,
        "nocheckcertificate": True,
        "geo_bypass": True,
        # 2024+: YouTube blocks the default "android" client from server IPs.
        # tv_embedded + ios + mweb still serve streams without PO-token in most regions.
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embedded", "ios", "mweb", "web_safari"],
                "player_skip": ["configs"],
            },
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.5 Mobile/15E148 Safari/604.1"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    # Optional: owner can drop a cookies.txt path via env to bypass bot-checks.
    cookies = os.getenv("YT_COOKIES_FILE", "").strip()
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    return opts


def _sync_download(url: str, workdir: str) -> dict:
    outtmpl = os.path.join(workdir, "%(id).40s.%(ext)s")
    with yt_dlp.YoutubeDL(_ydl_opts(outtmpl)) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info:  # playlist — take first
            info = info["entries"][0]
        path = ydl.prepare_filename(info)
        # yt-dlp may have remuxed -> swap extension if needed
        if not os.path.exists(path):
            base, _ = os.path.splitext(path)
            for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3"):
                if os.path.exists(base + ext):
                    path = base + ext
                    break
    size = os.path.getsize(path) if os.path.exists(path) else 0
    return {
        "path": path,
        "size": size,
        "title": (info.get("title") or "")[:200],
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration") or 0,
        "ext": os.path.splitext(path)[1].lstrip("."),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or url,
    }


async def download(url: str) -> dict:
    """Download a video. Returns dict with path/size/title or raises."""
    workdir = tempfile.mkdtemp(prefix="dl_")
    try:
        info = await asyncio.to_thread(_sync_download, url, workdir)
        if info["size"] == 0:
            raise RuntimeError("Downloaded file is empty.")
        if info["size"] > MAX_BYTES:
            try: os.remove(info["path"])
            except Exception: pass
            raise RuntimeError(
                f"File too large for Telegram ({info['size']/1024/1024:.1f} MB). "
                f"Max {MAX_BYTES/1024/1024:.0f} MB."
            )
        # caller is responsible for cleanup via cleanup()
        info["_workdir"] = workdir
        return info
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def user_error_text(err: Exception) -> str:
    msg = str(err or "Download failed").strip()
    low = msg.lower()
    if "sign in to confirm you're not a bot" in low:
        return "YouTube blocked this request from the server IP. Try another link or retry later."
    if "unable to extract video url" in low or "empty media response" in low:
        return "This platform did not expose a downloadable video stream for that link. Try another public post/reel."
    if "timed out" in low:
        return "The remote site took too long to respond. Please try again."
    return msg[:500]


def cleanup(info: dict):
    wd = info.get("_workdir")
    if wd:
        shutil.rmtree(wd, ignore_errors=True)
