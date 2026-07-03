# autocon

Automatic video converter daemon. Watches a directory for new video files and converts them to MP4 using ffmpeg.

## Features

- Watches a directory for new video files using inotify (via [watchdog](https://github.com/gorakhargosh/watchdog))
- Converts to MP4 (H.264 + AAC) with configurable codec settings
- Scales video down to a max resolution while preserving aspect ratio
- Optionally remuxes already-compatible files (stream copy, no re-encode)
- Concurrent conversions with configurable worker count
- Waits for files to finish writing before converting (with a give-up timeout)
- Moves originals to a separate directory (or deletes them) after conversion
- Quarantines failed inputs in a `failed/` directory so they aren't retried forever
- Collision-safe output naming (`movie (1).mp4` instead of silent overwrite)
- Post-conversion success/failure hook commands
- Processes existing files on startup; `--one-shot` and `--dry-run` modes
- Shuts down cleanly on SIGTERM/SIGINT: terminates in-flight ffmpeg and removes partial outputs
- Runs as a systemd user service

## Requirements

- Python 3.10+
- ffmpeg (and ffprobe, if `remux_compatible` is enabled)

## Installation

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configuration

Edit `config.toml` to customize behavior. All keys are optional and default to the values shown:

```toml
[directories]
watch_dir = "~/Videos/autocon"
output_dir = "converted"      # relative to watch_dir
originals_dir = "originals"   # relative to watch_dir
failed_dir = "failed"         # relative to watch_dir; created on first failure

[conversion]
video_codec = "libx264"
crf = 23
preset = "medium"
max_width = 1920
max_height = 1080
audio_codec = "aac"
audio_bitrate = "128k"
extra_output_flags = ["-movflags", "+faststart"]

# Stream-copy inputs whose codecs and resolution already match the targets
# instead of re-encoding them (fast, lossless; requires ffprobe)
remux_compatible = false

[conversion.extensions]
video = ["mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "ts", "3gp", "mp4"]

[general]
max_concurrent = 2
file_stable_interval = 2.0    # seconds between size checks when waiting for writes to finish
file_stable_timeout = 300.0   # give up if a file is still empty/growing after this many seconds
originals_mode = "keep"       # "keep" moves originals to originals_dir, "delete" removes them

[hooks]
# Optional commands run after each conversion. Placeholders: {input} (original
# path), {output} (converted file; on_success only), {name} (output stem).
# on_success = ["notify-send", "autocon", "Converted {name}"]
# on_failure = ["notify-send", "autocon", "FAILED: {input}"]
```

Bad values (e.g. `output_dir` pointing back at the watch directory, which would make autocon reprocess its own output) are rejected at startup with a clear error.

## Usage

```sh
# Uses config.toml from the same directory
./autocon.py

# Or specify a config file
./autocon.py /path/to/config.toml

# Process existing files, then exit (useful from cron)
./autocon.py --one-shot

# Show what would be converted without running ffmpeg
./autocon.py --dry-run --one-shot

# Debug logging, including the full ffmpeg command lines
./autocon.py --verbose
```

Hidden files (e.g. macOS `._foo.mkv` sidecars) are ignored. If two inputs map to the same output name (`foo.mkv` and `foo.avi`), the second gets a ` (N)` suffix instead of overwriting the first.

A legacy bash version (`autocon.sh`) is also included, which takes the watch directory as an argument and uses hardcoded conversion settings.

## Systemd service

Copy the provided unit file and edit the `ExecStart` paths to match your installation directory:

```sh
mkdir -p ~/.config/systemd/user
cp autocon.service ~/.config/systemd/user/

# Edit ExecStart to point to your clone location — %h expands to your home directory
# e.g. ExecStart=%h/gits/autocon/.venv/bin/python3 %h/gits/autocon/autocon.py
nano ~/.config/systemd/user/autocon.service

systemctl --user daemon-reload
systemctl --user enable --now autocon
```

The unit runs conversions at low CPU/IO priority (`Nice=10`, `IOSchedulingClass=idle`) so background transcodes don't interfere with interactive use. On `systemctl --user stop autocon`, in-flight ffmpeg processes are terminated and their partial outputs cleaned up.

## Testing

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

## License

MIT
