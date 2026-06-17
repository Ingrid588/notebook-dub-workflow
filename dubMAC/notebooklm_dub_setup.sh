#!/usr/bin/env bash
# Mac：NotebookLM 一次性登录（等价 Windows notebooklm_dub_setup.bat）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
COMP="$ROOT/components"

if command -v python3.12 &>/dev/null; then PY=python3.12
elif command -v python3 &>/dev/null; then PY=python3
else echo "[错误] 需要 Python 3.12：brew install python@3.12"; exit 1; fi

echo "=================================================="
echo "  NotebookLM 登录与依赖（实验性 · 非官方 API）"
echo "=================================================="
echo

echo "[1/4] 安装 notebooklm-py …"
"$PY" -m pip install "notebooklm-py[browser]" rookiepy -q

echo
echo "[2/4] 安装 Playwright Chromium（首次约 180MB）…"
"$PY" -m playwright install chromium

echo
echo "[3/4] 登录 Google NotebookLM（浏览器将打开）…"
"$PY" -m notebooklm login

echo
echo "[4/4] 验证登录 …"
"$PY" -m notebooklm auth check --test --json
echo
echo "完成。日常请运行 ./notebooklm_dub_translate.sh"
