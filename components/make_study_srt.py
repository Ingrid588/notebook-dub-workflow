#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 *.bilingual.srt 生成与视频同名的学习用 xxx.srt。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from dub_naming import discover_bilingual_subtitles, write_subtitle_with_study_sync


def main() -> int:
    ap = argparse.ArgumentParser(description='从双语字幕生成原名.srt')
    ap.add_argument('folder', type=Path, help='含双语字幕的文件夹')
    args = ap.parse_args()
    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f'[错误] 不是文件夹：{folder}')
        return 1

    srts = discover_bilingual_subtitles(folder)
    if not srts:
        print(f'[错误] 未找到双语字幕：{folder}')
        return 1

    n = 0
    total_citations = 0
    for src in srts:
        body = src.read_text(encoding='utf-8-sig')
        _, study, removed = write_subtitle_with_study_sync(src, body)
        total_citations += removed
        print(f'  [OK] {src.name}  ->  {study.name if study else "?"}')
        if removed:
            print(f'       已去掉引用索引：{removed} 处')
        n += 1

    if total_citations:
        print(f'\n  [清理] 共去掉引用索引 {total_citations} 处')

    print(f'\n  完成：{n} 个学习字幕')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
