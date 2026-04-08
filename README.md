# autocon

Automatic video converter daemon. Watches a directory for new video files and converts them to MP4 using ffmpeg.

## Features

- Watches a directory for new video files using inotify (via [watchdog](https://github.com/gorber/watchdog))
- Converts to MP4 (H.264 + AAC) with configurable codec settings
- Scales video down to a max resolution while preserving aspect ratio
- Concurrent conversions with configurable worker count
- Waits for files to finish writing before converting
- Moves originals to a separate directory after conversion
- Processes existing files on startup
- Runs as a systemd user service

## Requirements

- Python 3.10+
- ffmpeg

## Installation

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configuration

Edit `config.toml` to customize behavior:

```toml
[directories]
watch_dir = "~/Videos/autocon"
output_dir = "converted"      # relative to watch_dir
originals_dir = "originals"   # relative to watch_dir

[conversion]
video_codec = "libx264"
crf = 23
preset = "medium"
max_width = 1920
max_height = 1080
audio_codec = "aac"
audio_bitrate = "128k"
extra_output_flags = ["-movflags", "+faststart"]

[conversion.extensions]
video = ["mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "ts", "3gp", "mp4"]

[general]
max_concurrent = 2
file_stable_interval = 2.0
```

## Usage

```sh
# Uses config.toml from the same directory
./autocon.py

# Or specify a config file
./autocon.py /path/to/config.toml
```

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

## License

MIT
