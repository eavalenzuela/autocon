#!/usr/bin/env python3
"""Autocon - automatic video converter.

Watches a directory for new video files and converts them using ffmpeg.
"""

import logging
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

log = logging.getLogger("autocon")

DEFAULT_CONFIG = Path(__file__).parent / "config.toml"

DEFAULT_EXTENSIONS = {
    ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".ts", ".3gp", ".mp4",
}


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def get_video_extensions(cfg: dict) -> set[str]:
    exts = cfg.get("conversion", {}).get("extensions", {}).get("video")
    if exts is None:
        return DEFAULT_EXTENSIONS
    return {f".{e.lower().lstrip('.')}" for e in exts}


def is_video(path: Path, extensions: set[str]) -> bool:
    return path.suffix.lower() in extensions


def wait_for_stable(path: Path, interval: float = 2.0) -> bool:
    """Wait until the file size stops changing."""
    prev_size = -1
    while True:
        try:
            curr_size = path.stat().st_size
        except OSError:
            return False
        if curr_size == prev_size and curr_size > 0:
            return True
        prev_size = curr_size
        time.sleep(interval)


def build_ffmpeg_cmd(input_path: Path, output_path: Path, settings: dict) -> list[str]:
    codec = settings.get("video_codec", "libx264")
    crf = settings.get("crf", 23)
    preset = settings.get("preset", "medium")
    max_w = settings.get("max_width", 1920)
    max_h = settings.get("max_height", 1080)
    audio_codec = settings.get("audio_codec", "aac")
    audio_bitrate = settings.get("audio_bitrate", "128k")
    extra_flags = settings.get("extra_output_flags", ["-movflags", "+faststart"])

    scale_filter = (
        f"scale='min({max_w},iw)':'min({max_h},ih)'"
        f":force_original_aspect_ratio=decrease,"
        f"scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )

    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-y",
        "-i", str(input_path),
        "-c:v", codec, "-crf", str(crf), "-preset", preset,
        "-vf", scale_filter,
        "-c:a", audio_codec, "-b:a", audio_bitrate,
        *extra_flags,
        str(output_path),
    ]
    return cmd


def convert_video(
    input_path: Path,
    output_dir: Path,
    originals_dir: Path,
    settings: dict,
    stable_interval: float,
):
    name = input_path.stem
    output_path = output_dir / f"{name}.mp4"
    lock_path = input_path.with_name(input_path.name + ".lock")

    if not wait_for_stable(input_path, stable_interval):
        log.warning("File disappeared: %s", input_path.name)
        return

    lock_path.touch()
    log.info("CONVERTING: %s -> converted/%s.mp4", input_path.name, name)

    try:
        cmd = build_ffmpeg_cmd(input_path, output_path, settings)
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            size_mb = output_path.stat().st_size / (1024 * 1024)
            input_path.rename(originals_dir / input_path.name)
            log.info(
                "DONE: converted/%s.mp4 (%.1fM) — original moved to originals/",
                name, size_mb,
            )
        else:
            log.error("FAILED: %s\n%s", input_path.name, result.stderr[-2000:])
            output_path.unlink(missing_ok=True)
    finally:
        lock_path.unlink(missing_ok=True)


class VideoHandler(FileSystemEventHandler):
    def __init__(self, watch_dir, output_dir, originals_dir, settings, stable_interval, extensions, executor):
        self.watch_dir = watch_dir
        self.output_dir = output_dir
        self.originals_dir = originals_dir
        self.settings = settings
        self.stable_interval = stable_interval
        self.extensions = extensions
        self.executor = executor

    def _handle(self, path: Path):
        if path.parent.resolve() != self.watch_dir:
            return
        if is_video(path, self.extensions):
            self.executor.submit(
                convert_video, path, self.output_dir, self.originals_dir,
                self.settings, self.stable_interval,
            )

    def on_closed(self, event):
        """Triggered on IN_CLOSE_WRITE (Linux inotify) — file finished writing."""
        if not event.is_directory:
            self._handle(Path(event.src_path))

    def on_moved(self, event):
        """Triggered on IN_MOVED_TO — file moved into the watch directory."""
        if not event.is_directory:
            self._handle(Path(event.dest_path))


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG
    if not config_path.exists():
        log.error("Config not found: %s", config_path)
        sys.exit(1)

    cfg = load_config(config_path)

    # Directories
    dirs = cfg.get("directories", {})
    watch_dir = Path(dirs.get("watch_dir", ".")).expanduser().resolve()
    output_dir = Path(dirs.get("output_dir", "converted"))
    originals_dir = Path(dirs.get("originals_dir", "originals"))

    if not output_dir.is_absolute():
        output_dir = watch_dir / output_dir
    if not originals_dir.is_absolute():
        originals_dir = watch_dir / originals_dir

    watch_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    originals_dir.mkdir(parents=True, exist_ok=True)

    # Settings
    settings = cfg.get("conversion", {})
    extensions = get_video_extensions(cfg)
    general = cfg.get("general", {})
    max_workers = general.get("max_concurrent", 2)
    stable_interval = general.get("file_stable_interval", 2.0)

    executor = ThreadPoolExecutor(max_workers=max_workers)

    # Clean up stale lock files from previous runs
    for lock in watch_dir.glob("*.lock"):
        lock.unlink(missing_ok=True)
        log.info("Removed stale lock file: %s", lock.name)

    # Process existing videos
    log.info("Scanning for existing videos in %s ...", watch_dir)
    for f in sorted(watch_dir.iterdir()):
        if f.is_file() and is_video(f, extensions):
            executor.submit(convert_video, f, output_dir, originals_dir, settings, stable_interval)

    # Watch for new videos
    log.info("Watching %s for new videos ...", watch_dir)
    handler = VideoHandler(
        watch_dir, output_dir, originals_dir, settings,
        stable_interval, extensions, executor,
    )
    observer = Observer()
    observer.schedule(handler, str(watch_dir), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down ...")
    finally:
        observer.stop()
        executor.shutdown(wait=True)
        observer.join()


if __name__ == "__main__":
    main()
