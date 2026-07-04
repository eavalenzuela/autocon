"""Unit tests for autocon's pure helper functions.

Run with: pytest -q
"""

import threading
from pathlib import Path

import autocon


# --- extensions / file filtering ---

def test_get_video_extensions_default():
    assert autocon.get_video_extensions({}) == autocon.DEFAULT_EXTENSIONS


def test_get_video_extensions_normalizes():
    cfg = {"conversion": {"extensions": {"video": ["MKV", ".avi", "Mp4"]}}}
    assert autocon.get_video_extensions(cfg) == {".mkv", ".avi", ".mp4"}


def test_is_video_case_insensitive():
    exts = {".mkv", ".mp4"}
    assert autocon.is_video(Path("Movie.MKV"), exts)
    assert autocon.is_video(Path("movie.mp4"), exts)
    assert not autocon.is_video(Path("notes.txt"), exts)
    assert not autocon.is_video(Path("movie.mkv.lock"), exts)


def test_should_process_skips_hidden_files():
    exts = {".mkv"}
    assert autocon.should_process(Path("movie.mkv"), exts)
    assert not autocon.should_process(Path("._movie.mkv"), exts)
    assert not autocon.should_process(Path(".hidden.mkv"), exts)


# --- directory resolution ---

def test_resolve_dirs_defaults_relative_to_watch(tmp_path):
    cfg = {"directories": {"watch_dir": str(tmp_path)}}
    dirs = autocon.resolve_dirs(cfg)
    assert dirs["watch"] == tmp_path.resolve()
    assert dirs["output"] == tmp_path.resolve() / "converted"
    assert dirs["originals"] == tmp_path.resolve() / "originals"
    assert dirs["failed"] == tmp_path.resolve() / "failed"


def test_resolve_dirs_absolute_override(tmp_path):
    cfg = {
        "directories": {
            "watch_dir": str(tmp_path),
            "output_dir": "/somewhere/else",
        }
    }
    dirs = autocon.resolve_dirs(cfg)
    assert dirs["output"] == Path("/somewhere/else")


# --- config validation ---

def test_validate_config_empty_is_valid():
    assert autocon.validate_config({}) == []


def test_validate_config_rejects_output_equal_to_watch(tmp_path):
    cfg = {"directories": {"watch_dir": str(tmp_path), "output_dir": "."}}
    errors = autocon.validate_config(cfg)
    assert any("output_dir" in e for e in errors)


def test_validate_config_rejects_bad_values():
    cfg = {
        "conversion": {"crf": "high", "max_width": 0},
        "general": {"max_concurrent": 0, "originals_mode": "explode"},
        "hooks": {"on_success": "notify-send done"},
    }
    errors = autocon.validate_config(cfg)
    assert any("crf" in e for e in errors)
    assert any("max_width" in e for e in errors)
    assert any("max_concurrent" in e for e in errors)
    assert any("originals_mode" in e for e in errors)
    assert any("on_success" in e for e in errors)


# --- unique output naming ---

def test_unique_path_free_name(tmp_path):
    target = tmp_path / "movie.mp4"
    assert autocon.unique_path(target) == target


def test_unique_path_collisions(tmp_path):
    (tmp_path / "movie.mp4").touch()
    assert autocon.unique_path(tmp_path / "movie.mp4") == tmp_path / "movie (1).mp4"
    (tmp_path / "movie (1).mp4").touch()
    assert autocon.unique_path(tmp_path / "movie.mp4") == tmp_path / "movie (2).mp4"


# --- duration formatting ---

def test_format_duration():
    assert autocon.format_duration(45.2) == "45s"
    assert autocon.format_duration(192) == "3m12s"
    assert autocon.format_duration(3723) == "1h02m03s"


# --- file stability wait ---

def test_wait_for_stable_stable_file(tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"data")
    assert autocon.wait_for_stable(f, interval=0.01, timeout=5.0)


def test_wait_for_stable_missing_file(tmp_path):
    assert not autocon.wait_for_stable(tmp_path / "gone.mkv", interval=0.01, timeout=1.0)


def test_wait_for_stable_empty_file_times_out(tmp_path):
    f = tmp_path / "empty.mkv"
    f.touch()
    assert not autocon.wait_for_stable(f, interval=0.01, timeout=0.05)


def test_wait_for_stable_stop_event(tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"data")
    stop = threading.Event()
    stop.set()
    assert not autocon.wait_for_stable(f, interval=0.01, timeout=5.0, stop=stop)


# --- ffmpeg command building ---

def test_build_ffmpeg_cmd_defaults():
    cmd = autocon.build_ffmpeg_cmd(Path("in.mkv"), Path("out.mp4"), {})
    assert cmd[0] == "ffmpeg"
    assert "-nostdin" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-crf") + 1] == "23"
    assert cmd[cmd.index("-preset") + 1] == "medium"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert "+faststart" in cmd
    assert cmd[-1] == "out.mp4"


def test_build_ffmpeg_cmd_custom_settings():
    settings = {
        "video_codec": "libx265",
        "crf": 28,
        "preset": "slow",
        "max_width": 1280,
        "max_height": 720,
        "audio_codec": "libopus",
        "audio_bitrate": "96k",
        "extra_output_flags": [],
    }
    cmd = autocon.build_ffmpeg_cmd(Path("in.mkv"), Path("out.mp4"), settings)
    assert cmd[cmd.index("-c:v") + 1] == "libx265"
    assert cmd[cmd.index("-crf") + 1] == "28"
    assert "min(1280,iw)" in cmd[cmd.index("-vf") + 1]
    assert "+faststart" not in cmd


def test_build_ffmpeg_cmd_maps_all_audio():
    # Re-encode must keep the primary video and every audio track, matching the
    # remux path; without these maps ffmpeg's default selection drops all but
    # one audio stream.
    cmd = autocon.build_ffmpeg_cmd(Path("in.mkv"), Path("out.mp4"), {})
    assert "-map" in cmd
    map_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert "0:v:0" in map_values
    assert "0:a?" in map_values


def test_build_remux_cmd_stream_copies():
    cmd = autocon.build_remux_cmd(Path("in.mkv"), Path("out.mp4"), {})
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert "-crf" not in cmd
    assert "+faststart" in cmd
    assert cmd[-1] == "out.mp4"


# --- remux compatibility check ---

COMPATIBLE_STREAMS = [
    {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
    {"codec_type": "audio", "codec_name": "aac"},
]


def test_is_remuxable_compatible():
    assert autocon.is_remuxable(COMPATIBLE_STREAMS, {})


def test_is_remuxable_wrong_video_codec():
    streams = [
        {"codec_type": "video", "codec_name": "hevc", "width": 1280, "height": 720},
        {"codec_type": "audio", "codec_name": "aac"},
    ]
    assert not autocon.is_remuxable(streams, {})
    # ... unless the target codec matches
    assert autocon.is_remuxable(streams, {"video_codec": "libx265"})


def test_is_remuxable_too_large():
    streams = [
        {"codec_type": "video", "codec_name": "h264", "width": 3840, "height": 2160},
        {"codec_type": "audio", "codec_name": "aac"},
    ]
    assert not autocon.is_remuxable(streams, {})
    assert autocon.is_remuxable(streams, {"max_width": 3840, "max_height": 2160})


def test_is_remuxable_wrong_audio_codec():
    streams = [
        {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
        {"codec_type": "audio", "codec_name": "vorbis"},
    ]
    assert not autocon.is_remuxable(streams, {})


def test_is_remuxable_requires_video_stream():
    assert not autocon.is_remuxable([{"codec_type": "audio", "codec_name": "aac"}], {})
    assert not autocon.is_remuxable([], {})


def test_is_remuxable_unknown_target_codec():
    assert not autocon.is_remuxable(COMPATIBLE_STREAMS, {"video_codec": "libcustom"})


# --- hook expansion ---

def test_expand_hook_substitutes_placeholders():
    cmd = ["notify-send", "autocon", "{name} done: {output}"]
    expanded = autocon.expand_hook(cmd, name="movie", output="/x/movie.mp4")
    assert expanded == ["notify-send", "autocon", "movie done: /x/movie.mp4"]


def test_expand_hook_leaves_unknown_braces():
    expanded = autocon.expand_hook(["echo", "{unknown}"], name="movie")
    assert expanded == ["echo", "{unknown}"]


# --- watch handler ---

class _RecordingConverter:
    def __init__(self):
        self.submitted = []

    def submit(self, path):
        self.submitted.append(path)


class _FakeEvent:
    def __init__(self, src_path, is_directory=False):
        self.src_path = src_path
        self.dest_path = src_path
        self.is_directory = is_directory


def test_on_created_submits_video(tmp_path):
    # A same-filesystem `mv` into the watch dir emits only a create event, so
    # on_created must queue the file (files were silently ignored before).
    watch = tmp_path.resolve()
    conv = _RecordingConverter()
    handler = autocon.VideoHandler(watch, {".mkv"}, conv)
    movie = watch / "movie.mkv"
    handler.on_created(_FakeEvent(str(movie)))
    assert conv.submitted == [movie]


def test_on_created_ignores_non_video_and_dirs(tmp_path):
    watch = tmp_path.resolve()
    conv = _RecordingConverter()
    handler = autocon.VideoHandler(watch, {".mkv"}, conv)
    handler.on_created(_FakeEvent(str(watch / "notes.txt")))
    handler.on_created(_FakeEvent(str(watch / "subdir"), is_directory=True))
    assert conv.submitted == []
