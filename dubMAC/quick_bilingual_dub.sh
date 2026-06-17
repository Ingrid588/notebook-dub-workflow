#!/usr/bin/env bash
# Mac：双语字幕直配预览（等价 Windows quick_bilingual_dub.bat）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
COMP="$ROOT/components"

if command -v python3.12 &>/dev/null; then PY=python3.12
elif command -v python3 &>/dev/null; then PY=python3
else echo "[错误] 需要 Python 3.12：brew install python@3.12"; exit 1; fi

if ! command -v ffmpeg &>/dev/null; then
  echo "[警告] 未找到 ffmpeg，合并可能失败。brew install ffmpeg"
fi

WORKDIR="${1:-}"
EXTRA=()
if [[ $# -ge 1 ]]; then
  WORKDIR="$1"
  shift
  EXTRA=("$@")
fi
if [[ -z "$WORKDIR" ]]; then
  read -r -p "视频文件夹路径: " WORKDIR
fi
WORKDIR="${WORKDIR%\"}"
WORKDIR="${WORKDIR#\"}"

if [[ ! -d "$WORKDIR" ]]; then
  echo "[错误] 文件夹不存在: $WORKDIR"
  exit 1
fi

exec "$PY" -u "$COMP/quick_bilingual_dub.py" "$WORKDIR" "${EXTRA[@]}"
