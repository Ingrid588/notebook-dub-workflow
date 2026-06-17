#!/usr/bin/env bash
# 合并视频 + 配音 → 原名_C.mp4（仅配音轨，递归子文件夹）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=================================================="
echo "  合并视频 + 配音 → 原名_C.mp4"
echo "  仅中文配音轨，不含原声；递归处理子文件夹"
echo "=================================================="
echo

# Python
if command -v python3.12 &>/dev/null; then
    PY=python3.12
elif command -v python3 &>/dev/null; then
    PY=python3
else
    echo "[错误] 未找到 python3，请先安装 Python 3.10+"
    exit 1
fi

# ffmpeg
if ! command -v ffmpeg &>/dev/null; then
    echo "[错误] 未找到 ffmpeg。"
    echo "       macOS 可执行：brew install ffmpeg"
    exit 1
fi

echo "说明："
echo "  - 同一文件夹内，视频与配音「文件名前 4 个单词」相同即配对"
echo "  - 配音可以是 .mp3 / .m4a，也可能是体量较小的 .mp4"
echo "  - 输出：原视频名_C.mp4（保存在同一文件夹）"
echo

if [[ $# -ge 1 ]]; then
    ROOT="$1"
else
    read -r -p "请输入课程根目录: " ROOT
fi

if [[ -z "${ROOT}" || ! -d "${ROOT}" ]]; then
    echo "[错误] 目录不存在：${ROOT:-（空）}"
    exit 1
fi

if [[ "${2:-}" == "--dry-run" ]]; then
    MODE=1
else
    echo
    echo "[1] 先预览匹配（--dry-run）"
    echo "[2] 正式合并"
    read -r -p "请选择 1 或 2（直接回车=正式合并）: " CHOICE
    if [[ "${CHOICE}" == "1" ]]; then
        MODE=1
    else
        MODE=2
    fi
fi

echo
if [[ "${MODE}" == "1" ]]; then
    echo "--- 预览模式 ---"
    exec "$PY" -u "$SCRIPT_DIR/merge_dub_videos.py" "$ROOT" --dry-run
else
    echo "--- 开始合并 ---"
    exec "$PY" -u "$SCRIPT_DIR/merge_dub_videos.py" "$ROOT"
fi
