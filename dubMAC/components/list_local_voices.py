#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""列出本机可用于配音补全的 Windows 本地语音，并显示配置写法。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from step1_subtitle_to_audio import print_local_voices_help, setup_console_encoding

if __name__ == '__main__':
    setup_console_encoding()
    print_local_voices_help()
