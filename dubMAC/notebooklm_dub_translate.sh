#!/usr/bin/env bash
# Mac：NotebookLM 全自动翻译 + 配音（等价 Windows notebooklm_dub_translate.bat）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
COMP="$ROOT/components"

if command -v python3.12 &>/dev/null; then PY=python3.12
elif command -v python3 &>/dev/null; then PY=python3
else echo "[错误] 需要 Python 3.12：brew install python@3.12"; exit 1; fi

DODUB=1
LESSON_ARGS=()
WORKDIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-dub) DODUB=0; shift ;;
    --dub) DODUB=1; shift ;;
    --from-lesson|--to-lesson)
      LESSON_ARGS+=("$1" "$2")
      shift 2
      ;;
    *)
      if [[ -z "$WORKDIR" ]]; then WORKDIR="$1"; fi
      shift
      ;;
  esac
done

WORKDIR="${WORKDIR%\"}"
WORKDIR="${WORKDIR#\"}"

if [[ -z "$WORKDIR" ]]; then
  read -r -p "拖入或粘贴视频文件夹路径: " WORKDIR
fi
WORKDIR="${WORKDIR%\"}"
WORKDIR="${WORKDIR#\"}"

if [[ ! -d "$WORKDIR" ]]; then
  echo "[错误] 文件夹不存在: $WORKDIR"
  exit 1
fi

echo "=================================================="
echo "  NotebookLM 全自动：登录 + 翻译 + 配音"
echo "  文件夹: $WORKDIR"
if [[ "$DODUB" == "0" ]]; then echo "  模式: 仅翻译 (--no-dub)"
else echo "  模式: 翻译后 quick_bilingual_dub"; fi
echo "=================================================="
echo

echo "[0/3] 检查依赖与 NotebookLM 登录…"
"$PY" -u "$COMP/notebooklm_dub_automation.py" --ensure-ready

echo
echo "[1/3] 课序号"
"$PY" -u "$COMP/notebooklm_dub_automation.py" "$WORKDIR" --list-lessons

echo
echo "[2/3] 选择课序号（回车=全部）"
read -r -p "起始课 # (Enter=全部): " LFROM
if [[ -n "$LFROM" ]]; then
  read -r -p "结束课 # (Enter=$LFROM): " LTO
  LTO="${LTO:-$LFROM}"
  LESSON_ARGS=(--from-lesson "$LFROM" --to-lesson "$LTO")
  echo "  已选: $LFROM ~ $LTO"
else
  LESSON_ARGS=()
  echo "  已选: 全部"
fi

echo
echo "[3/3] 翻译 (+ 配音) …"
echo "  大批量会自动按文件/字幕条拆分，耗时会变长，可挂机。"
echo "  中断后重跑同一命令会跳过已完成（断点续跑）。"
echo

CMD=("$PY" -u "$COMP/notebooklm_dub_automation.py" "$WORKDIR")
if [[ "$DODUB" == "0" ]]; then CMD+=(--no-dub); fi
if ((${#LESSON_ARGS[@]})); then CMD+=("${LESSON_ARGS[@]}"); fi
exec "${CMD[@]}"
