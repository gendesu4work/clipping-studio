"""Make sure ffmpeg and ffprobe are available, even on PCs that never installed them.

If they're not on PATH, the static-ffmpeg package downloads a copy once (about 80 MB, kept
inside the Python environment) and adds it to this process's PATH, so every later
subprocess call to "ffmpeg"/"ffprobe" (ours and yt-dlp's) finds it.
"""
import shutil
import threading

_lock = threading.Lock()


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def ensure_ffmpeg() -> bool:
    """Blocks until ffmpeg/ffprobe are usable (downloading them if needed). Returns success."""
    if have_ffmpeg():
        return True
    with _lock:  # one download at a time; later callers wait for it
        if have_ffmpeg():
            return True
        try:
            import static_ffmpeg
            static_ffmpeg.add_paths(weak=True)
        except Exception as e:
            print(f"[ffmpeg] couldn't get ffmpeg: {e}", flush=True)
        return have_ffmpeg()


def ensure_ffmpeg_in_background() -> None:
    """Start fetching ffmpeg at app start so it's usually ready before anyone needs it."""
    if not have_ffmpeg():
        threading.Thread(target=ensure_ffmpeg, daemon=True).start()
