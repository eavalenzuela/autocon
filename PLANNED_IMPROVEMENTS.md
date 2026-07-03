# Planned improvements & features

Plan for the next round of autocon development. Improvements target existing
behavior; features add new capability. Each item is scoped to land as part of
one coherent change.

## Improvements

1. **Deduplicate in-flight conversions** — the startup scan and duplicate
   inotify events (close_write firing more than once, or moved-then-closed)
   can submit the same file twice, producing two ffmpeg processes racing on
   one output; track an in-progress set behind a lock.
2. **Validate config at startup with clear errors** — a typo like
   `output_dir = "."` makes autocon convert its own output forever; refuse
   output/originals dirs that resolve to the watch dir and type-check numeric
   settings instead of failing deep inside ffmpeg.
3. **Timeout for the file-stability wait** — a zero-byte or endlessly-growing
   file currently pins a worker thread forever in `wait_for_stable`; add a
   configurable `file_stable_timeout` and give up cleanly.
4. **Check for ffmpeg at startup and surface worker exceptions** — a missing
   ffmpeg binary today raises `FileNotFoundError` inside the thread pool and
   vanishes silently; probe with `shutil.which` at startup and log unexpected
   exceptions from conversion workers.
5. **Collision-safe output and originals naming** — `foo.mkv` and `foo.avi`
   both map to `converted/foo.mp4` and the second silently overwrites the
   first (ffmpeg runs with `-y`); pick a unique ` (N)`-suffixed name instead.
6. **Cross-filesystem-safe original move** — `Path.rename` raises `EXDEV`
   when `originals_dir` points at another mount; use `shutil.move`.
7. **Graceful SIGTERM shutdown** — systemd stops the service with SIGTERM,
   which currently kills mid-conversion ffmpeg abruptly and leaves partial
   MP4s in `converted/`; catch the signal, terminate child processes, and
   clean up partial outputs and lock files.
8. **Richer conversion logging** — log input size, conversion duration, and
   space saved per file, plus a totals summary on shutdown, so the journal
   answers "was this worth it" at a glance.
9. **Skip hidden files** — dotfiles like macOS `._foo.mkv` AppleDouble
   sidecars match the extension check today and get "converted" to garbage;
   ignore names starting with a dot.
10. **Unit tests + docs polish** — add a pytest suite for the pure helpers
    (command building, config validation, naming, stability wait), fix the
    broken watchdog link in the README, and harden the systemd unit with
    `Nice=` / `IOSchedulingClass=idle` so transcodes don't starve the desktop.

## New features

1. **Real CLI via argparse** — `--dry-run` (log what would convert without
   running ffmpeg), `--one-shot` (process existing files then exit, for cron
   use), `--verbose` (DEBUG logging incl. full ffmpeg command), `--version`.
2. **Smart remux of already-compatible files** — ffprobe the input first and,
   when streams already match the target codecs and resolution cap, stream-copy
   into MP4 (`-c copy`) instead of re-encoding: seconds instead of minutes,
   zero generation loss. Opt-in via `remux_compatible = true`.
3. **Failure quarantine directory** — failed inputs currently stay in the
   watch dir and are retried (and re-fail) on every restart; move them to a
   configurable `failed/` directory instead.
4. **Configurable original handling** — `originals_mode = "keep" | "delete"`:
   users who trust the conversion can reclaim disk space immediately instead
   of manually emptying `originals/`.
5. **Post-conversion hooks** — optional `[hooks] on_success` / `on_failure`
   commands with `{input}` / `{output}` / `{name}` placeholders, e.g. to send
   a desktop notification or kick off a library rescan.
