#!/usr/bin/env bash
# Mac 本地配音测试（等价 Windows「测试本地配音.bat」）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
COMP="$ROOT/components"

if command -v python3.12 &>/dev/null; then
  PY=python3.12
elif command -v python3 &>/dev/null; then
  PY=python3
else
  echo "[错误] 未找到 python3，请先：brew install python@3.12"
  exit 1
fi

if ! command -v ffmpeg &>/dev/null; then
  echo "[警告] 未找到 ffmpeg，合并视频可能失败。brew install ffmpeg"
fi

echo "=================================================="
echo "  本地视频配音测试（Mac）"
echo "  步骤：中文字幕 → TTS → 合并成片 _C.mp4"
echo "  配置：components/tubeget.config.json"
echo "=================================================="
echo

WORKDIR="${1:-}"
if [[ -z "$WORKDIR" ]]; then
  read -r -p "请输入视频文件夹路径（可拖入后回车）: " WORKDIR
fi
WORKDIR="${WORKDIR%\"}"
WORKDIR="${WORKDIR#\"}"
if [[ ! -d "$WORKDIR" ]]; then
  echo "[错误] 文件夹不存在: $WORKDIR"
  exit 1
fi

echo "[1/2] 检查依赖..."
cd "$COMP"
if ! "$PY" -u check_env.py; then
  echo "正在安装 pip 依赖..."
  "$PY" -m pip install -r requirements.txt
  "$PY" -u check_env.py || exit 1
fi

echo
echo "[2/2] 开始配音..."
cd "$WORKDIR"
"$PY" -u "$COMP/run_batch.py"
echo
echo "完成。"
