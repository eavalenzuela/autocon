#!/usr/bin/env bash
set -euo pipefail

WATCH_DIR="${1:?Usage: autocon.sh <watch-directory>}"
OUTPUT_DIR="${WATCH_DIR}/converted"
ORIGINALS_DIR="${WATCH_DIR}/originals"

mkdir -p "$OUTPUT_DIR" "$ORIGINALS_DIR"

VIDEO_EXTS="mkv|avi|mov|wmv|flv|webm|m4v|mpg|mpeg|ts|3gp|mp4"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

convert_video() {
    local input="$1"
    local basename
    basename="$(basename "$input")"
    local name="${basename%.*}"
    local output="${OUTPUT_DIR}/${name}.mp4"

    # Skip files in subdirectories
    local input_dir
    input_dir="$(dirname "$(realpath "$input")")"
    if [[ "$input_dir" != "$(realpath "$WATCH_DIR")" ]]; then
        return 0
    fi

    # Wait a moment for the file to be fully written
    local prev_size=-1
    local curr_size
    while true; do
        curr_size=$(stat --format=%s "$input" 2>/dev/null || echo 0)
        [[ "$curr_size" == "$prev_size" && "$curr_size" -gt 0 ]] && break
        prev_size=$curr_size
        sleep 2
    done

    log "CONVERTING: $basename -> converted/${name}.mp4"

    if ffmpeg -nostdin -hide_banner -y -i "$input" \
        -c:v libx264 -crf 23 -preset medium \
        -vf "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2" \
        -c:a aac -b:a 128k \
        -movflags +faststart \
        "$output" 2>&1 | while IFS= read -r line; do log "  ffmpeg: $line"; done; then
        mv "$input" "$ORIGINALS_DIR/"
        log "DONE: converted/${name}.mp4 ($(du -h "$output" | cut -f1)) — original moved to originals/"
    else
        log "FAILED: $basename"
        rm -f "$output"
    fi
}

# Process any existing videos first
log "Scanning for existing videos in $WATCH_DIR ..."
for f in "$WATCH_DIR"/*; do
    [[ -f "$f" ]] || continue
    if [[ "${f,,}" =~ \.($VIDEO_EXTS)$ ]]; then
        convert_video "$f" &
    fi
done
wait

log "Watching $WATCH_DIR for new videos ..."
inotifywait -m -e close_write --format '%f' "$WATCH_DIR" | while IFS= read -r filename; do
    if [[ "$filename" =~ \.($VIDEO_EXTS)$ ]]; then
        convert_video "${WATCH_DIR}/${filename}" &
    fi
done
