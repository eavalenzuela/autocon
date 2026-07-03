#!/usr/bin/env python3
"""Autocon - automatic video converter.

Watches a directory for new video files and converts them using ffmpeg.
"""

import argparse
import json
import logging
import shutil
import signal
import subprocess
import sys
import threading
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

__version__ = "0.2.0"

DEFAULT_CONFIG = Path(__file__).parent / "config.toml"

DEFAULT_EXTENSIONS = {
    ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".ts", ".3gp", ".mp4",
}

# ffmpeg encoder name -> codec name as reported by ffprobe. Used to decide
# whether an input can be remuxed (stream-copied) instead of re-encoded.
CODEC_NAMES = {
    "libx264": "h264",
    "h264": "h264",
    "h264_nvenc": "h264",
    "h264_vaapi": "h264",
    "h264_qsv": "h264",
    "libx265": "hevc",
    "hevc": "hevc",
    "hevc_nvenc": "hevc",
    "hevc_vaapi": "hevc",
    "hevc_qsv": "hevc",
    "libaom-av1": "av1",
    "libsvtav1": "av1",
    "aac": "aac",
    "libfdk_aac": "aac",
    "libopus": "opus",
    "opus": "opus",
    "libmp3lame": "mp3",
    "mp3": "mp3",
    "libvorbis": "vorbis",
}

ORIGINALS_MODES = {"keep", "delete"}


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


def should_process(path: Path, extensions: set[str]) -> bool:
    """True for visible files with a configured video extension.

    Hidden files (e.g. macOS `._foo.mkv` AppleDouble sidecars) are skipped.
    """
    if path.name.startswith("."):
        return False
    return is_video(path, extensions)


def resolve_dirs(cfg: dict) -> dict[str, Path]:
    """Resolve the watch/output/originals/failed directories from config.

    Relative output/originals/failed paths are anchored at the watch dir.
    """
    dirs = cfg.get("directories", {})
    watch_dir = Path(dirs.get("watch_dir", ".")).expanduser().resolve()

    def resolve(key: str, default: str) -> Path:
        p = Path(dirs.get(key, default)).expanduser()
        return p if p.is_absolute() else watch_dir / p

    return {
        "watch": watch_dir,
        "output": resolve("output_dir", "converted"),
        "originals": resolve("originals_dir", "originals"),
        "failed": resolve("failed_dir", "failed"),
    }


def validate_config(cfg: dict) -> list[str]:
    """Return a list of human-readable config errors (empty if valid)."""
    errors: list[str] = []

    dirs = resolve_dirs(cfg)
    for key in ("output", "originals", "failed"):
        if dirs[key].expanduser().resolve() == dirs["watch"]:
            errors.append(
                f"directories.{key}_dir resolves to the watch directory itself; "
                "autocon would reprocess its own output"
            )

    conv = cfg.get("conversion", {})
    crf = conv.get("crf", 23)
    if isinstance(crf, bool) or not isinstance(crf, int) or not 0 <= crf <= 51:
        errors.append(f"conversion.crf must be an integer between 0 and 51, got {crf!r}")
    for key in ("max_width", "max_height"):
        v = conv.get(key, 1)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            errors.append(f"conversion.{key} must be a positive integer, got {v!r}")
    flags = conv.get("extra_output_flags", [])
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
        errors.append("conversion.extra_output_flags must be a list of strings")

    general = cfg.get("general", {})
    workers = general.get("max_concurrent", 2)
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        errors.append(f"general.max_concurrent must be a positive integer, got {workers!r}")
    for key, default in (("file_stable_interval", 2.0), ("file_stable_timeout", 300.0)):
        v = general.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            errors.append(f"general.{key} must be a positive number, got {v!r}")
    mode = general.get("originals_mode", "keep")
    if mode not in ORIGINALS_MODES:
        errors.append(
            f"general.originals_mode must be one of {sorted(ORIGINALS_MODES)}, got {mode!r}"
        )

    hooks = cfg.get("hooks", {})
    for key in ("on_success", "on_failure"):
        cmd = hooks.get(key)
        if cmd is not None and (
            not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd)
        ):
            errors.append(f"hooks.{key} must be a non-empty list of strings")

    return errors


def unique_path(path: Path) -> Path:
    """Return `path`, or a ` (N)`-suffixed variant if it already exists."""
    if not path.exists():
        return path
    for i in range(1, 10000):
        candidate = path.with_stem(f"{path.stem} ({i})")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find a free name for {path}")


def format_duration(seconds: float) -> str:
    """Render a duration as e.g. '45s', '3m12s', or '1h02m03s'."""
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s"


def wait_for_stable(
    path: Path,
    interval: float = 2.0,
    timeout: float = 300.0,
    stop: threading.Event | None = None,
) -> bool:
    """Wait until the file size stops changing (and is non-zero).

    Returns False if the file disappears, stays empty / keeps growing past
    `timeout`, or `stop` is set.
    """
    deadline = time.monotonic() + timeout
    prev_size = -1
    while True:
        if stop is not None and stop.is_set():
            return False
        try:
            curr_size = path.stat().st_size
        except OSError:
            return False
        if curr_size == prev_size and curr_size > 0:
            return True
        if time.monotonic() >= deadline:
            return False
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


def build_remux_cmd(input_path: Path, output_path: Path, settings: dict) -> list[str]:
    """Stream-copy an already-compatible input into an MP4 container."""
    extra_flags = settings.get("extra_output_flags", ["-movflags", "+faststart"])
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-y",
        "-i", str(input_path),
        "-map", "0:v:0", "-map", "0:a?",
        "-c", "copy",
        *extra_flags,
        str(output_path),
    ]


def probe_streams(path: Path) -> list[dict] | None:
    """Return the input's streams as reported by ffprobe, or None on failure."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,codec_name,width,height",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout).get("streams", [])
    except (json.JSONDecodeError, AttributeError):
        return None


def is_remuxable(streams: list[dict], settings: dict) -> bool:
    """True if every stream already matches the target codecs and size cap."""
    target_video = CODEC_NAMES.get(settings.get("video_codec", "libx264"))
    target_audio = CODEC_NAMES.get(settings.get("audio_codec", "aac"))
    if target_video is None or target_audio is None:
        return False
    max_w = settings.get("max_width", 1920)
    max_h = settings.get("max_height", 1080)

    has_video = False
    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video":
            has_video = True
            if stream.get("codec_name") != target_video:
                return False
            if stream.get("width", 0) > max_w or stream.get("height", 0) > max_h:
                return False
        elif codec_type == "audio":
            if stream.get("codec_name") != target_audio:
                return False
    return has_video


def expand_hook(cmd: list[str], **values: str) -> list[str]:
    """Substitute {name}-style placeholders in each hook argument."""
    expanded = []
    for arg in cmd:
        for key, value in values.items():
            arg = arg.replace("{" + key + "}", value)
        expanded.append(arg)
    return expanded


def run_hook(hook: str, cmd: list[str] | None, **values: str):
    if not cmd:
        return
    expanded = expand_hook(cmd, **values)
    log.debug("Running %s hook: %s", hook, expanded)
    try:
        result = subprocess.run(expanded, capture_output=True, text=True)
    except OSError as exc:
        log.error("Hook %s failed to start: %s", hook, exc)
        return
    if result.returncode != 0:
        log.error(
            "Hook %s exited with %d: %s",
            hook, result.returncode, result.stderr.strip()[-500:],
        )


class Converter:
    """Runs conversions on a thread pool, tracking in-flight files and processes."""

    def __init__(self, cfg: dict, dirs: dict[str, Path], executor: ThreadPoolExecutor,
                 dry_run: bool = False):
        self.settings = cfg.get("conversion", {})
        general = cfg.get("general", {})
        self.stable_interval = general.get("file_stable_interval", 2.0)
        self.stable_timeout = general.get("file_stable_timeout", 300.0)
        self.originals_mode = general.get("originals_mode", "keep")
        self.remux_compatible = self.settings.get("remux_compatible", False)
        hooks = cfg.get("hooks", {})
        self.on_success = hooks.get("on_success")
        self.on_failure = hooks.get("on_failure")
        self.output_dir = dirs["output"]
        self.originals_dir = dirs["originals"]
        self.failed_dir = dirs["failed"]
        self.executor = executor
        self.dry_run = dry_run
        self.stop = threading.Event()
        self.stats = {"converted": 0, "remuxed": 0, "failed": 0}
        self._lock = threading.Lock()
        self._in_progress: set[Path] = set()
        self._procs: set[subprocess.Popen] = set()

    def submit(self, path: Path):
        """Queue a file for conversion unless it is already being handled."""
        if self.stop.is_set():
            return
        path = path.resolve()
        with self._lock:
            if path in self._in_progress:
                log.debug("Already converting, ignoring duplicate event: %s", path.name)
                return
            self._in_progress.add(path)
        try:
            self.executor.submit(self._run, path)
        except RuntimeError:  # executor already shut down
            with self._lock:
                self._in_progress.discard(path)

    def shutdown(self):
        """Stop accepting work and terminate any running ffmpeg processes."""
        self.stop.set()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            proc.terminate()

    def _run(self, path: Path):
        try:
            self.convert(path)
        except Exception:
            log.exception("Unexpected error converting %s", path.name)
        finally:
            with self._lock:
                self._in_progress.discard(path)

    def convert(self, input_path: Path):
        if self.dry_run:
            log.info(
                "DRY-RUN: would convert %s -> %s/%s.mp4",
                input_path.name, self.output_dir.name, input_path.stem,
            )
            return

        if not wait_for_stable(input_path, self.stable_interval, self.stable_timeout, self.stop):
            if not self.stop.is_set():
                log.warning(
                    "Gave up waiting for %s (removed, empty, or still being written)",
                    input_path.name,
                )
            return

        # Reserve a collision-free output name (foo.mkv and foo.avi both map
        # to foo.mp4 otherwise, and ffmpeg runs with -y).
        with self._lock:
            output_path = unique_path(self.output_dir / f"{input_path.stem}.mp4")
            output_path.touch()

        lock_path = input_path.with_name(input_path.name + ".lock")
        lock_path.touch()

        remux = False
        if self.remux_compatible:
            streams = probe_streams(input_path)
            remux = streams is not None and is_remuxable(streams, self.settings)

        if remux:
            cmd = build_remux_cmd(input_path, output_path, self.settings)
            log.info(
                "REMUXING: %s -> %s/%s (streams already compatible)",
                input_path.name, self.output_dir.name, output_path.name,
            )
        else:
            cmd = build_ffmpeg_cmd(input_path, output_path, self.settings)
            log.info(
                "CONVERTING: %s -> %s/%s",
                input_path.name, self.output_dir.name, output_path.name,
            )
        log.debug("ffmpeg command: %s", " ".join(cmd))

        input_size = input_path.stat().st_size
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            with self._lock:
                self._procs.add(proc)
            try:
                _, stderr = proc.communicate()
            finally:
                with self._lock:
                    self._procs.discard(proc)

            if self.stop.is_set() and proc.returncode != 0:
                log.info("CANCELLED: %s (shutting down)", input_path.name)
                output_path.unlink(missing_ok=True)
                return

            if proc.returncode == 0:
                self._finish_success(input_path, output_path, input_size, started, remux)
            else:
                self._finish_failure(input_path, output_path, stderr)
        finally:
            lock_path.unlink(missing_ok=True)

    def _finish_success(self, input_path: Path, output_path: Path,
                        input_size: int, started: float, remuxed: bool):
        duration = time.monotonic() - started
        output_size = output_path.stat().st_size
        saved_pct = (1 - output_size / input_size) * 100 if input_size else 0.0

        if self.originals_mode == "delete":
            input_path.unlink(missing_ok=True)
            original_note = "original deleted"
        else:
            # shutil.move survives originals_dir being on another filesystem
            dest = unique_path(self.originals_dir / input_path.name)
            shutil.move(str(input_path), str(dest))
            original_note = f"original moved to {self.originals_dir.name}/"

        action = "remuxed" if remuxed else "converted"
        self.stats[action] += 1
        log.info(
            "DONE: %s/%s (%.1fM -> %.1fM, %.0f%% saved, %s in %s) — %s",
            self.output_dir.name, output_path.name,
            input_size / (1024 * 1024), output_size / (1024 * 1024),
            saved_pct, action, format_duration(duration), original_note,
        )
        run_hook(
            "on_success", self.on_success,
            input=str(input_path), output=str(output_path), name=output_path.stem,
        )

    def _finish_failure(self, input_path: Path, output_path: Path, stderr: str):
        self.stats["failed"] += 1
        log.error("FAILED: %s\n%s", input_path.name, (stderr or "").strip()[-2000:])
        output_path.unlink(missing_ok=True)

        # Quarantine the input so it isn't retried (and re-failed) on every restart
        self.failed_dir.mkdir(parents=True, exist_ok=True)
        dest = unique_path(self.failed_dir / input_path.name)
        try:
            shutil.move(str(input_path), str(dest))
            log.info("Moved failed input to %s/%s", self.failed_dir.name, dest.name)
        except OSError as exc:
            log.error("Could not quarantine %s: %s", input_path.name, exc)
        run_hook(
            "on_failure", self.on_failure,
            input=str(input_path), name=input_path.stem,
        )


class VideoHandler(FileSystemEventHandler):
    def __init__(self, watch_dir: Path, extensions: set[str], converter: Converter):
        self.watch_dir = watch_dir
        self.extensions = extensions
        self.converter = converter

    def _handle(self, path: Path):
        if path.parent.resolve() != self.watch_dir:
            return
        if should_process(path, self.extensions):
            self.converter.submit(path)

    def on_closed(self, event):
        """Triggered on IN_CLOSE_WRITE (Linux inotify) — file finished writing."""
        if not event.is_directory:
            self._handle(Path(event.src_path))

    def on_moved(self, event):
        """Triggered on IN_MOVED_TO — file moved into the watch directory."""
        if not event.is_directory:
            self._handle(Path(event.dest_path))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="autocon",
        description="Watch a directory for new video files and convert them with ffmpeg.",
    )
    parser.add_argument(
        "config", nargs="?", type=Path, default=DEFAULT_CONFIG,
        help=f"path to config file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="log what would be converted without running ffmpeg",
    )
    parser.add_argument(
        "--one-shot", action="store_true",
        help="process existing files, then exit instead of watching",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="enable debug logging (includes full ffmpeg commands)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main():
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not args.config.exists():
        log.error("Config not found: %s", args.config)
        sys.exit(1)

    cfg = load_config(args.config)

    errors = validate_config(cfg)
    if errors:
        for err in errors:
            log.error("Config error: %s", err)
        sys.exit(1)

    if not args.dry_run and shutil.which("ffmpeg") is None:
        log.error("ffmpeg not found in PATH")
        sys.exit(1)
    if cfg.get("conversion", {}).get("remux_compatible", False) and shutil.which("ffprobe") is None:
        log.warning("ffprobe not found in PATH; remux_compatible disabled")
        cfg["conversion"]["remux_compatible"] = False

    # Directories (failed/ is created lazily on first failure)
    dirs = resolve_dirs(cfg)
    watch_dir = dirs["watch"]
    watch_dir.mkdir(parents=True, exist_ok=True)
    dirs["output"].mkdir(parents=True, exist_ok=True)
    dirs["originals"].mkdir(parents=True, exist_ok=True)

    extensions = get_video_extensions(cfg)
    max_workers = cfg.get("general", {}).get("max_concurrent", 2)

    executor = ThreadPoolExecutor(max_workers=max_workers)
    converter = Converter(cfg, dirs, executor, dry_run=args.dry_run)

    # Clean up stale lock files from previous runs
    for lock in watch_dir.glob("*.lock"):
        lock.unlink(missing_ok=True)
        log.info("Removed stale lock file: %s", lock.name)

    # Shut down cleanly on SIGTERM (systemd stop) and SIGINT (Ctrl-C)
    stop = threading.Event()

    def _on_signal(signum, frame):
        log.info("Received %s, shutting down ...", signal.Signals(signum).name)
        stop.set()
        converter.shutdown()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # Process existing videos
    log.info("Scanning for existing videos in %s ...", watch_dir)
    for f in sorted(watch_dir.iterdir()):
        if f.is_file() and should_process(f, extensions):
            converter.submit(f)

    observer = None
    if not args.one_shot:
        log.info("Watching %s for new videos ...", watch_dir)
        handler = VideoHandler(watch_dir, extensions, converter)
        observer = Observer()
        observer.schedule(handler, str(watch_dir), recursive=False)
        observer.start()

    try:
        if args.one_shot:
            executor.shutdown(wait=True)
        else:
            stop.wait()
    finally:
        if observer is not None:
            observer.stop()
        converter.shutdown()
        executor.shutdown(wait=True, cancel_futures=True)
        if observer is not None:
            observer.join()
        log.info(
            "Totals: %d converted, %d remuxed, %d failed",
            converter.stats["converted"], converter.stats["remuxed"],
            converter.stats["failed"],
        )


if __name__ == "__main__":
    main()
