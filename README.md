# Living Room (Media Server)

A lightweight, self-hosted, Netflix-style video server written in Python. Open it from any device on your Wi-Fi and stream videos in the browser, with no client software.

## Features

- Automatically converts `.mkv` files to browser-friendly `.mp4` using FFmpeg (video is copied, audio is converted to AAC)
- Background worker that detects new files without a restart
- HTTP Range support (206 Partial Content), so seeking is instant on desktop and mobile
- Responsive dark UI with live search, thumbnails and an in-page player
- Resume position and progress bar, saved per browser
- Keyboard shortcuts (space, arrows, `m`, `f`, Esc)
- Single file: Flask backend with the HTML, CSS and JS embedded, no external CDNs

## Requirements

- Python 3.9 or newer
- [FFmpeg](https://ffmpeg.org/download.html) on your PATH

## Install and run

```bash
pip install -r requirements.txt
python media_server.py --media-dir "/path/to/videos" --port 8000
```

Then open `http://<server-ip>:8000` from any device on the same network. The terminal prints the address.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--media-dir` | `./media` | Folder with your video files |
| `--host` | `0.0.0.0` | Bind address |
| `--port` | `8000` | Port |

## Security note

There is no authentication. Anyone on your network can watch, so do not expose it to the public internet.

## Known limitations

- No subtitle support yet (planned)
- Only the first audio track is kept
- HEVC video plays in Safari but not in every browser
