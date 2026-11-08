#!/usr/bin/env python3
"""
Living Room - a lightweight, self-hosted, Netflix-style media server.

What it does
------------
1. Scans a media folder (recursively) for .mkv files and remuxes each one to a
   web-friendly .mp4 with FFmpeg (video stream copied, audio -> AAC). Files that
   already have a matching .mp4 are skipped. Runs in a background thread, so the
   web UI is available immediately and shows conversion progress.
2. Serves a dark, responsive library UI with live search and an in-page player.
3. Streams video with proper HTTP Range support (206 Partial Content), so
   seeking works instantly on desktop, iPhone, Android and tablets.

Run
---
    pip install flask waitress
    python media_server.py --media-dir ~/Videos --port 8000

Then open  http://<server-ip>:8000  from any device on the same Wi-Fi.

Requires FFmpeg on PATH (https://ffmpeg.org/download.html).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file

# --------------------------------------------------------------------------- #
# Configuration / shared state
# --------------------------------------------------------------------------- #

CHUNK_SIZE = 1024 * 512          # bytes read per iteration while streaming
RESCAN_SECONDS = 20              # how often the worker looks for new files
POSTER_DIRNAME = ".posters"      # cached thumbnails live inside the media dir
AUDIO_DIRNAME = ".audio"         # cached single-language copies live here

app = Flask(__name__)

MEDIA_DIR = Path("media").resolve()
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

_lock = threading.Lock()
library = {}                     # video id -> absolute Path (only .mp4 files)
state = {
    "version": 0,                # bumps whenever the library contents change
    "converting": None,          # title currently being remuxed
    "queue": 0,                  # files still waiting
    "failed": [],                # names of files FFmpeg could not convert
}
_poster_slots = threading.Semaphore(2)   # limit parallel thumbnail jobs
_variant_lock = threading.Lock()         # one audio-copy job at a time


# --------------------------------------------------------------------------- #
# Library scanning
# --------------------------------------------------------------------------- #

def _visible(path: Path) -> bool:
    """Skip hidden folders/files (e.g. the poster cache) and in-progress temp files."""
    rel_parts = path.relative_to(MEDIA_DIR).parts
    return not any(p.startswith(".") for p in rel_parts) and ".part." not in path.name


def _walk(suffix: str):
    for p in MEDIA_DIR.rglob("*"):
        if p.is_file() and p.suffix.lower() == suffix and _visible(p):
            yield p


def _video_id(path: Path) -> str:
    rel = path.relative_to(MEDIA_DIR).as_posix()
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]


def _title(path: Path) -> str:
    stem = path.stem
    if " " not in stem:                       # "My.Movie_2020" -> "My Movie 2020"
        stem = re.sub(r"[._]+", " ", stem).strip()
    return stem or path.name


def scan_library() -> None:
    """Rebuild the id -> file map from the .mp4 files on disk."""
    found = {_video_id(p): p for p in _walk(".mp4")}
    with _lock:
        if set(found) != set(library):
            state["version"] += 1
        library.clear()
        library.update(found)


# --------------------------------------------------------------------------- #
# FFmpeg remuxing (MKV -> MP4)
# --------------------------------------------------------------------------- #

def output_dir(mkv: Path) -> Path:
    """Folder that holds everything generated from this MKV."""
    if mkv.parent.name == mkv.stem:        # MKV already sits in its own folder
        return mkv.parent
    return mkv.parent / mkv.stem


def output_mp4(mkv: Path) -> Path:
    return output_dir(mkv) / (mkv.stem + ".mp4")


def pending_conversions():
    """MKV files with no MP4 yet, either in the new folder or the old spot beside the MKV."""
    return [m for m in _walk(".mkv")
            if not output_mp4(m).exists() and not m.with_suffix(".mp4").exists()]

def needs_video_reencode(src: Path) -> bool:
    """True if the video is not 8-bit H.264 (phones often can't play it)."""
    if not FFPROBE:
        return False                      # can't check, so keep the old behaviour
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,pix_fmt",
             "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, timeout=60,
        )
        codec, _, pix = r.stdout.strip().partition(",")
        return not (codec == "h264" and pix in ("yuv420p", "yuvj420p"))
    except Exception:
        return False

def probe_tracks(path: Path, kind: str):
    """List audio ('a') or subtitle ('s') streams of a file.
    Returns e.g. [{'n': 0, 'codec': 'aac', 'lang': 'eng', 'title': ''}, ...]
    where n is the position among streams of that kind (0 = first)."""
    if not FFPROBE:
        return []
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", kind,
             "-show_entries", "stream=codec_name:stream_tags=language,title",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except Exception:
        return []
    out = []
    for n, s in enumerate(streams):
        tags = s.get("tags", {})
        out.append({"n": n, "codec": s.get("codec_name", ""),
                    "lang": tags.get("language", "und"),
                    "title": tags.get("title", "")})
    return out
TEXT_SUB_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}


def extract_subtitles(mkv: Path) -> None:
    """Write each text subtitle track of the MKV beside the MP4 as Name.<lang>.srt.
    Picture-based tracks (PGS, VobSub) are skipped because they can't become text."""
    used = set()
    for s in probe_tracks(mkv, "s"):
        if s["codec"] not in TEXT_SUB_CODECS:
            continue
        lang = re.sub(r"[^A-Za-z0-9-]", "", s["lang"]) or "und"
        label, k = lang, 1
        while label in used:                   # two tracks, same language
            k += 1
            label = f"{lang}{k}"
        used.add(label)
        out = output_dir(mkv) / f"{mkv.stem}.{label}.srt"
        if out.exists():                       # never overwrite a file you may have edited
            continue
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(mkv),
             "-map", f"0:s:{s['n']}", "-c:s", "srt", str(out)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            out.unlink(missing_ok=True)
            print(f"[subs] could not extract track {s['n']} of {mkv.name}")
        else:
            print(f"[subs] wrote {out.name}")

def remux(mkv: Path) -> bool:
    """
    Equivalent to:  ffmpeg -i input.mkv -c:v copy -c:a aac output.mp4
    plus a few safety flags. Arguments are passed as a list (no shell), so
    spaces and special characters in file names are handled safely.
    Output goes to a temp file first, so a crash never leaves a broken .mp4.
    """
    mp4 = output_mp4(mkv)
    mp4.parent.mkdir(parents=True, exist_ok=True)
    tmp = mp4.with_name(mp4.stem + ".part.mp4")
    if needs_video_reencode(mkv):
        print(f"[remux] re-encoding video to H.264: {mkv.name}")
        video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                      "-pix_fmt", "yuv420p"]
    else:
        video_args = ["-c:v", "copy"]        # already phone-friendly: near-instant
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(mkv),
        "-map", "0:v:0", "-map", "0:a?",     # first video + ALL audio tracks
        *video_args,
        "-c:a", "aac", "-b:a", "192k",       # browsers need AAC
        "-ac", "2",                          # stereo: keeps dialogue audible everywhere
        "-sn", "-dn",                        # drop subtitles/data (MP4 can't hold most)
        "-movflags", "+faststart",           # put the index up front for instant start
        str(tmp),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not tmp.exists():
            print(f"[remux] FAILED {mkv.name}\n{result.stderr.strip()}")
            return False
        os.replace(tmp, mp4)
        print(f"[remux] done   {mkv.name}")
        try:
            extract_subtitles(mkv)             # a subtitle problem must not fail the movie
        except Exception as exc:
            print(f"[subs] error: {exc}")
        return True
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def worker() -> None:
    """Background loop: convert any new MKVs, then refresh the library."""
    while True:
        try:
            scan_library()
            todo = pending_conversions() if FFMPEG else []
            with _lock:
                state["queue"] = len(todo)
            for i, mkv in enumerate(todo):
                with _lock:
                    state["converting"] = _title(mkv)
                    state["queue"] = len(todo) - i - 1
                ok = remux(mkv)
                with _lock:
                    if not ok and mkv.name not in state["failed"]:
                        state["failed"].append(mkv.name)
                scan_library()
        except Exception as exc:                       # keep the worker alive
            print(f"[worker] error: {exc}")
        finally:
            with _lock:
                state["converting"] = None
                state["queue"] = 0
        time.sleep(RESCAN_SECONDS)


# --------------------------------------------------------------------------- #
# HTTP: API + streaming with Range support
# --------------------------------------------------------------------------- #

def _lookup(vid: str) -> Path:
    with _lock:
        path = library.get(vid)
    if path is None or not path.exists():
        abort(404)
    return path


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024

def audio_variant(vid: str, src: Path, n: int) -> Path:
    """MP4 copy of `src` that contains only audio track n. Built once, then cached."""
    if not FFMPEG or n < 0 or n >= len(probe_tracks(src, "a")):
        abort(404)
    cache = src.parent / AUDIO_DIRNAME      # src is the movie's MP4, so this is its own folder
    cache.mkdir(exist_ok=True)
    out = cache / f"{vid}_a{n}.mp4"

    def fresh():
        return (out.exists() and out.stat().st_size > 0
                and out.stat().st_mtime >= src.stat().st_mtime)

    with _variant_lock:                       # other requests wait, then find it ready
        if not fresh():
            tmp = cache / f"{vid}_a{n}.part.mp4"
            print(f"[audio] building track {n + 1} for {src.name}")
            r = subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                 "-map", "0:v:0", "-map", f"0:a:{n}", "-c", "copy",
                 "-movflags", "+faststart", str(tmp)],
                capture_output=True, text=True,
            )
            if r.returncode != 0 or not tmp.exists():
                tmp.unlink(missing_ok=True)
                print(f"[audio] FAILED {src.name}\n{r.stderr.strip()}")
                abort(500)
            os.replace(tmp, out)
    return out


@app.get("/api/tracks/<vid>")
def api_tracks(vid):
    path = _lookup(vid)
    return jsonify({
        "audio": probe_tracks(path, "a"),
        "subs": [{"n": i, "label": s["label"]}
                 for i, s in enumerate(find_subtitles(path))],
    })
def find_subtitles(video: Path):
    """Sidecar subtitles: 'Movie.srt' or 'Movie.en.srt' next to 'Movie.mp4'."""
    found = []
    for f in sorted(video.parent.glob("*.srt")):
        if f.stem == video.stem:
            label = "Subtitles"
        elif f.stem.startswith(video.stem + "."):
            label = f.stem[len(video.stem) + 1:]
        else:
            continue
        found.append({"file": f, "label": label})
    return found


@app.get("/subs/<vid>/<int:n>.vtt")
def subtitle(vid, n):
    """Browsers only understand WebVTT, so convert the .srt on the fly."""
    subs = find_subtitles(_lookup(vid))
    if n >= len(subs):
        abort(404)
    raw = subs[n]["file"].read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(                              # 00:01:02,500 -> 00:01:02.500 (timing lines only)
        r"^(\d{2}:\d{2}:\d{2}),(\d{3}) --> (\d{2}:\d{2}:\d{2}),(\d{3})",
        r"\1.\2 --> \3.\4 line:85%", text, flags=re.M)
    return Response("WEBVTT\n\n" + text, mimetype="text/vtt")

@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.get("/api/library")
def api_library():
    with _lock:
        items = [
            {"id": vid, "title": _title(p), "size": _human_size(p.stat().st_size)}
            for vid, p in library.items() if p.exists()
        ]
    items.sort(key=lambda i: i["title"].lower())
    return jsonify(items)


@app.get("/api/status")
def api_status():
    with _lock:
        return jsonify({**state, "media_dir": str(MEDIA_DIR), "ffmpeg": bool(FFMPEG)})


@app.get("/stream/<vid>")
def stream(vid):
    """Serve an MP4 honouring HTTP Range headers (206 Partial Content)."""
    path = _lookup(vid)
    track = request.args.get("a", default=0, type=int)
    if track > 0:                              # an alternate language was requested
        path = audio_variant(vid, path, track)
    size = path.stat().st_size
    start, end, status = 0, size - 1, 200
    range_header = request.headers.get("Range")
    if range_header:
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not m or (m.group(1) == "" and m.group(2) == ""):
            return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        first, last = m.groups()
        if first == "":                                  # "bytes=-500" -> last 500 bytes
            start = max(size - int(last), 0)
        else:
            start = int(first)
            if last:
                end = min(int(last), size - 1)
        if start >= size or start > end:
            return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206

    length = end - start + 1

    def generate():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Type": "video/mp4",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "no-cache",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Response(generate(), status=status, headers=headers, direct_passthrough=True)


@app.get("/poster/<vid>")
def poster(vid):
    """Lazily grab a frame as a thumbnail; cached on disk. 404 if impossible."""
    path = _lookup(vid)
    if not FFMPEG:
        abort(404)
    out = path.with_name(path.stem + ".poster.jpg")
    if not out.exists() or out.stat().st_size == 0:
        with _poster_slots:
            for ss in ("30", "5", "0"):                  # fall back for short videos
                subprocess.run(
                    [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", ss,
                     "-i", str(path), "-frames:v", "1", "-vf", "scale=480:-2",
                     "-q:v", "4", str(out)],
                    capture_output=True, timeout=60,
                )
                if out.exists() and out.stat().st_size > 0:
                    break
    if not out.exists() or out.stat().st_size == 0:
        abort(404)
    return send_file(out, mimetype="image/jpeg", max_age=86400)


# --------------------------------------------------------------------------- #
# Front end (served as one self-contained page; no external CDN needed, so it
# works even when the server has no internet access)
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#141414">
<title>Living Room</title>
<style>
:root{--bg:#141414;--panel:#222;--line:#333;--text:#ececec;--dim:#9a9a9a;--accent:#5eead4}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0;background:var(--bg);color:var(--text);
  font:16px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
body.lock{overflow:hidden}

header{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:.75rem 1.5rem;
  align-items:center;justify-content:space-between;
  padding:calc(.9rem + env(safe-area-inset-top)) max(1.25rem,env(safe-area-inset-right)) 1rem max(1.25rem,env(safe-area-inset-left));
  background:linear-gradient(var(--bg) 75%,rgba(20,20,20,0))}
.brand{margin:0;font-size:1.6rem;font-weight:800;letter-spacing:-.035em}
#search{width:min(380px,100%);padding:.65rem 1rem;border-radius:999px;border:1px solid var(--line);
  background:var(--panel);color:var(--text);font:inherit;outline:none}
#search:focus{border-color:var(--accent)}

#banner{margin:0 max(1.25rem,env(safe-area-inset-left)) 1rem;padding:.6rem .9rem;border-radius:6px;
  background:#1b2a28;border:1px solid #28504a;color:#b8f3ea;font-size:.9rem}
#banner.warn{background:#2b2218;border-color:#5a4524;color:#f3d9aa}

main{padding:0 max(1.25rem,env(safe-area-inset-right)) 3rem max(1.25rem,env(safe-area-inset-left))}
#grid{display:grid;gap:1.5rem 1rem;grid-template-columns:repeat(auto-fill,minmax(clamp(150px,22vw,260px),1fr))}
#empty{color:var(--dim);max-width:46ch;padding:3rem 0}
#empty code{color:var(--text);word-break:break-all}

.card{display:flex;flex-direction:column;gap:.45rem;padding:0;border:0;background:none;
  color:inherit;font:inherit;text-align:left;cursor:pointer;-webkit-tap-highlight-color:transparent}
.art{position:relative;aspect-ratio:16/9;border-radius:6px;overflow:hidden;display:grid;place-items:center;
  background:linear-gradient(135deg,hsl(var(--h) 45% 26%),hsl(calc(var(--h) + 40) 40% 12%));
  transition:transform .18s ease,box-shadow .18s ease}
.art span{font-size:2.6rem;font-weight:800;opacity:.5}
.art img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.card:hover .art{transform:scale(1.04);box-shadow:0 10px 26px rgba(0,0,0,.65)}
.card:focus-visible{outline:none}
.card:focus-visible .art{outline:2px solid var(--accent);outline-offset:3px}
.bar{position:absolute;left:0;right:0;bottom:0;height:4px;background:rgba(0,0,0,.55)}
.bar i{display:block;height:100%;background:var(--accent)}
.title{font-weight:600;font-size:.95rem;overflow:hidden;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.meta{color:var(--dim);font-size:.8rem}

#player{position:fixed;inset:0;z-index:20;display:flex;flex-direction:column;background:#000}
.ptop{display:flex;align-items:center;gap:1rem;
  padding:calc(.6rem + env(safe-area-inset-top)) max(1rem,env(safe-area-inset-right)) .6rem max(1rem,env(safe-area-inset-left))}
.ptop button{padding:.5rem .9rem;border-radius:6px;border:1px solid #444;background:#1a1a1a;
  color:var(--text);font:inherit;cursor:pointer}
.ptop button:hover{background:#2a2a2a}
.ptop button:focus-visible{outline:2px solid var(--accent)}
#ptitle{margin:0;font-size:1.05rem;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ptop select{margin-left:auto;padding:.5rem .9rem;border-radius:6px;border:1px solid #444;
  background:#1a1a1a;color:var(--text);font:inherit;max-width:50vw}
#video{flex:1;min-height:0;width:100%;background:#000}
#perr{margin:0;padding:.8rem 1rem;background:#2b1818;color:#f3b6b6;font-size:.95rem}
#video::cue{
  font-size:120%;
  color:#fff;
  background:rgba(0,0,0,.6);
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
@media (prefers-reduced-motion:reduce){.art{transition:none}.card:hover .art{transform:none}}
</style>
</head>
<body>
<header>
  <h1 class="brand">Living Room</h1>
  <input id="search" type="search" placeholder="Search your library" autocomplete="off" aria-label="Search your library">
</header>

<div id="banner" role="status" hidden></div>
<main>
  <div id="grid"></div>
  <p id="empty" hidden></p>
</main>

<section id="player" hidden aria-label="Video player">
  <div class="ptop">
    <button id="close" type="button">Back to library</button>
    <h2 id="ptitle"></h2>
    <select id="atrack" aria-label="Audio track" hidden></select>
  </div>
  <video id="video" controls playsinline preload="metadata"></video>
  <p id="perr" hidden>This file can't be played in this browser. It may use a video codec (such as HEVC) the browser doesn't support. Try Safari, or another device.</p>
</section>

<script>
const $ = s => document.querySelector(s);
const grid = $('#grid'), empty = $('#empty'), search = $('#search'), banner = $('#banner');
const modal = $('#player'), video = $('#video'), ptitle = $('#ptitle'), perr = $('#perr');
const atrack = $('#atrack');
let items = [], current = null, lastSave = 0, version = -1, mediaDir = '';

/* ---- resume positions (per browser) ---- */
const getPos = id => { try { return JSON.parse(localStorage.getItem('pos:' + id)); } catch { return null; } };
const setPos = (id, v) => { try { v ? localStorage.setItem('pos:' + id, JSON.stringify(v)) : localStorage.removeItem('pos:' + id); } catch {} };
const hue = s => { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };

/* ---- library grid ---- */
function render() {
  const term = search.value.trim().toLowerCase();
  const list = items.filter(i => i.title.toLowerCase().includes(term));
  grid.replaceChildren();
  for (const it of list) {
    const card = document.createElement('button');
    card.type = 'button'; card.className = 'card';

    const art = document.createElement('div');
    art.className = 'art'; art.style.setProperty('--h', hue(it.title));
    const letter = document.createElement('span');
    letter.textContent = ([...it.title][0] || '?').toUpperCase();
    const img = new Image();
    img.loading = 'lazy'; img.alt = ''; img.src = '/poster/' + it.id;
    img.onerror = () => img.remove();
    art.append(letter, img);

    const pos = getPos(it.id);
    if (pos && pos.d) {
      const bar = document.createElement('div'), fill = document.createElement('i');
      bar.className = 'bar'; fill.style.width = Math.min(100, pos.t / pos.d * 100) + '%';
      bar.append(fill); art.append(bar);
    }

    const title = document.createElement('span');
    title.className = 'title'; title.textContent = it.title;
    const meta = document.createElement('span');
    meta.className = 'meta'; meta.textContent = it.size;

    card.append(art, title, meta);
    card.addEventListener('click', () => openPlayer(it));
    grid.append(card);
  }
  if (!items.length) {
    empty.hidden = false;
    empty.replaceChildren('No videos yet. Add .mkv or .mp4 files to ');
    const code = document.createElement('code'); code.textContent = mediaDir || 'the media folder';
    empty.append(code, ' and they will appear here.');
  } else if (!list.length) {
    empty.hidden = false; empty.textContent = 'Nothing matches "' + search.value.trim() + '".';
  } else empty.hidden = true;
}
search.addEventListener('input', render);

/* ---- player ---- */
const langName = c => {
  try { return new Intl.DisplayNames(['en'], { type: 'language' }).of(c) || c; }
  catch { return c; }
};

async function loadTracks(it) {
  atrack.hidden = true; atrack.replaceChildren();
  video.querySelectorAll('track').forEach(el => el.remove());
  try {
    const t = await (await fetch('/api/tracks/' + it.id)).json();
    if (current !== it) return;              // player was closed or changed meanwhile
    for (const s of t.subs) {
      const tr = document.createElement('track');
      const isCode = /^[a-z]{2,3}$/i.test(s.label);
      tr.kind = 'subtitles';
      tr.label = isCode ? langName(s.label) : s.label;
      if (isCode) tr.srclang = s.label;
      tr.src = '/subs/' + it.id + '/' + s.n + '.vtt';
      video.append(tr);
    }
    if (t.audio.length < 2) return;          // nothing to choose from
    for (const a of t.audio) {
      const o = document.createElement('option');
      o.value = a.n;
      o.textContent = langName(a.lang) + (a.title ? ' - ' + a.title : '') + '  (track ' + (a.n + 1) + ')';
      atrack.append(o);
    }
    atrack.value = '0'; atrack.hidden = false;
  } catch {}
}

atrack.addEventListener('change', () => {
  const t = video.currentTime, wasPlaying = !video.paused;
  video.onloadedmetadata = () => {
    video.currentTime = t;
    if (wasPlaying) video.play().catch(() => {});
  };
  video.src = '/stream/' + current.id + (atrack.value !== '0' ? '?a=' + atrack.value : '');
});

function openPlayer(it) {
  current = it; perr.hidden = true; ptitle.textContent = it.title;
  modal.hidden = false; document.body.classList.add('lock');
  const pos = getPos(it.id);
  video.onloadedmetadata = () => {
    if (pos && pos.t > 5 && pos.t < video.duration - 30) video.currentTime = pos.t;
  };
  video.src = '/stream/' + it.id;
  loadTracks(it);
  video.play().catch(() => {});          // some browsers require a tap; controls are visible
  history.pushState({ p: 1 }, '');       // so the phone's Back button closes the player
  $('#close').focus();
}
function save() {
  if (!current || !video.duration) return;
  if (video.currentTime > video.duration - 30) setPos(current.id, null);
  else setPos(current.id, { t: video.currentTime, d: video.duration });
}
function hidePlayer() {
  if (modal.hidden) return;
  video.pause(); save();
  current = null;
  video.removeAttribute('src'); video.load();   // release the connection
  atrack.hidden = true;
  modal.hidden = true; document.body.classList.remove('lock');
  render();
}
video.addEventListener('timeupdate', () => {
  const now = Date.now();
  if (now - lastSave > 5000) { lastSave = now; save(); }
});
video.addEventListener('error', () => { if (current) perr.hidden = false; });
$('#close').addEventListener('click', () => history.back());
window.addEventListener('popstate', hidePlayer);

document.addEventListener('keydown', e => {
  if (modal.hidden) return;
  const k = e.key;
  if (k === 'Escape' && !document.fullscreenElement) { history.back(); return; }
  if (e.target === $('#close') && (k === ' ' || k === 'Enter')) return;
  if (k === ' ' || k === 'k') { e.preventDefault(); video.paused ? video.play() : video.pause(); }
  else if (k === 'ArrowRight') { e.preventDefault(); video.currentTime += 10; }
  else if (k === 'ArrowLeft') { e.preventDefault(); video.currentTime -= 10; }
  else if (k === 'ArrowUp') { e.preventDefault(); video.volume = Math.min(1, video.volume + .1); }
  else if (k === 'ArrowDown') { e.preventDefault(); video.volume = Math.max(0, video.volume - .1); }
  else if (k === 'm') video.muted = !video.muted;
  else if (k === 'f') document.fullscreenElement ? document.exitFullscreen() : (video.requestFullscreen || video.webkitEnterFullscreen).call(video);
});

/* ---- server status (conversion progress + auto-refresh) ---- */
async function load() {
  items = await (await fetch('/api/library')).json();
  render();
}
async function poll() {
  try {
    const s = await (await fetch('/api/status')).json();
    mediaDir = s.media_dir;
    if (s.converting) {
      banner.className = ''; banner.hidden = false;
      banner.textContent = 'Preparing "' + s.converting + '" for playback' + (s.queue ? ' (' + s.queue + ' more waiting)' : '') + '...';
    } else if (!s.ffmpeg) {
      banner.className = 'warn'; banner.hidden = false;
      banner.textContent = 'FFmpeg was not found on the server, so MKV files are not being converted.';
    } else if (s.failed.length) {
      banner.className = 'warn'; banner.hidden = false;
      banner.textContent = s.failed.length + ' file(s) could not be converted: ' + s.failed.join(', ') + '. Check the server terminal for details.';
    } else banner.hidden = true;
    if (s.version !== version && modal.hidden) { version = s.version; await load(); }
  } catch {}
}
poll(); setInterval(poll, 3000);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def lan_ip() -> str:
    """Best-effort local IP (no packets are actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main() -> None:
    global MEDIA_DIR
    ap = argparse.ArgumentParser(description="Self-hosted Netflix-style media server")
    ap.add_argument("--media-dir", default=os.environ.get("MEDIA_DIR", "media"),
                    help="folder containing your .mkv / .mp4 files (default: ./media)")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    args = ap.parse_args()

    MEDIA_DIR = Path(args.media_dir).expanduser().resolve()
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    if not FFMPEG:
        print("WARNING: ffmpeg not found on PATH. MKV conversion and thumbnails are disabled.")

    scan_library()                                        # existing MP4s show up instantly
    threading.Thread(target=worker, daemon=True).start()  # remux MKVs in the background

    print(f"\n  Media folder : {MEDIA_DIR}")
    print(f"  This machine : http://localhost:{args.port}")
    print(f"  Wi-Fi/LAN    : http://{lan_ip()}:{args.port}\n")

    try:
        from waitress import serve                        # production-grade, handles streaming well
        serve(app, host=args.host, port=args.port, threads=16)
    except ImportError:
        print("(waitress not installed - using Flask's built-in server)")
        app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
