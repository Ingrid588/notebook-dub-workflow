#!/usr/bin/env bash
# Mac：补生成学习用 {视频主名}.srt
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
COMP="$ROOT/components"

if command -v python3.12 &>/dev/null; then PY=python3.12
elif command -v python3 &>/dev/null; then PY=python3
else echo "[错误] 需要 Python 3.12"; exit 1; fi

WORKDIR="${1:-}"
if [[ -z "$WORKDIR" ]]; then
  read -r -p "视频文件夹路径: " WORKDIR
fi
WORKDIR="${WORKDIR%\"}"
WORKDIR="${WORKDIR#\"}"

exec "$PY" -u "$COMP/make_study_srt.py" "$WORKDIR" "${@:2}"
