#!/usr/bin/env bash
# 合并视频 + 配音 → 原名_C.mp4（便携版）
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if command -v python3.12 &>/dev/null; then PY=python3.12
elif command -v python3 &>/dev/null; then PY=python3
else echo "[错误] 需要 Python 3.10+"; exit 1; fi

if ! command -v ffmpeg &>/dev/null; then
  echo "[错误] 需要 ffmpeg。macOS: brew install ffmpeg"
  exit 1
fi

echo "=================================================="
echo "  合并视频 + 配音 → *_C.mp4（仅配音轨）"
echo "=================================================="
echo "[1] 图形选文件夹 + 预览"
echo "[2] 图形选文件夹 + 合并"
echo "[3] 命令行指定目录"
read -r -p "选择 1/2/3（回车=2）: " MODE
MODE=${MODE:-2}

case "$MODE" in
  1) exec "$PY" -u "$SCRIPT_DIR/merge_dub_videos.py" --pick-folder --dry-run ;;
  3)
    read -r -p "目录路径: " ROOT
    exec "$PY" -u "$SCRIPT_DIR/merge_dub_videos.py" "$ROOT" ;;
  *) exec "$PY" -u "$SCRIPT_DIR/merge_dub_videos.py" --pick-folder ;;
esac
