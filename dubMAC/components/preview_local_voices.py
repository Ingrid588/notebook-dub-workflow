#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
试听 Windows 本地中文语音（生成 MP3 并用系统默认播放器打开）。

用法：
  python preview_local_voices.py              # 试听所有男声
  python preview_local_voices.py --all        # 试听全部中文语音
  python preview_local_voices.py kangkang     # 只试听康康
  python preview_local_voices.py kangkang zhiwei
"""
import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))

from step1_subtitle_to_audio import (
    find_local_voices,
    setup_console_encoding,
    synth_one_local,
)

DEFAULT_TEXT = (
    '你好，这是 AWS 课程的中文配音试听。'
    'Amazon Transcribe 可以把语音自动转成文字。'
)
OUT_DIR = HERE / '本地语音试听'


def main():
    setup_console_encoding()
    parser = argparse.ArgumentParser(description='试听本地 TTS 男声/女声')
    parser.add_argument(
        'voices', nargs='*',
        help='语音 key，如 kangkang、zhiwei；留空则按 --gender 筛选',
    )
    parser.add_argument(
        '--gender', choices=('male', 'female', 'all'), default='male',
        help='未指定 voices 时按性别筛选（默认仅男声）',
    )
    parser.add_argument('--all', action='store_true', help='试听全部中文语音')
    parser.add_argument('--text', default=DEFAULT_TEXT, help='试听朗读文本')
    parser.add_argument('--no-play', action='store_true', help='只生成文件，不自动播放')
    args = parser.parse_args()

    if args.all:
        gender = None
    elif args.voices:
        gender = None
    else:
        gender = None if args.gender == 'all' else args.gender

    all_zh = find_local_voices(zh_only=True, gender=gender)
    if not all_zh:
        print('未找到中文本地语音，请先安装并运行 enable_onecore_sapi_voices.ps1')
        sys.exit(1)

    if args.voices:
        keys = [v.lower() for v in args.voices]
        selected = [x for x in all_zh if x['key'] in keys]
        missing = set(keys) - {x['key'] for x in selected}
        if missing:
            print(f'未找到：{", ".join(sorted(missing))}')
            print('可用：', ', '.join(x['key'] for x in all_zh))
        if not selected:
            sys.exit(1)
    else:
        selected = all_zh

    OUT_DIR.mkdir(exist_ok=True)
    print(f'\n  将生成 {len(selected)} 个试听文件 → {OUT_DIR}\n')

    for item in selected:
        key = item['key']
        g = {'male': '男', 'female': '女'}.get(item['gender'], '?')
        mp3 = OUT_DIR / f'{key}_试听.mp3'
        print(f'  [{g}] {item["name"]}')
        ok = synth_one_local(
            args.text, str(mp3), rate='+0%',
            voice_key=key, mark_fallback=False,
        )
        if not ok:
            print(f'    ❌ 生成失败')
            continue
        print(f'    ✅ {mp3.name}')
        if not args.no_play:
            try:
                os.startfile(mp3)  # type: ignore[attr-defined]
            except Exception:
                print(f'    请手动打开：{mp3}')

    print(f'\n  完成。文件夹：{OUT_DIR}')
    print('  选定后请在 tubeget.config.json 设置：')
    print('    "engine": "local",')
    print('    "local_voice": "kangkang",')
    print('    "local_voice_gender": "male"')


if __name__ == '__main__':
    main()
