#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用术语表校正双语字幕中文行（零 API），再供 quick_bilingual_dub 配音。

用法：
  py -3.12 apply_glossary_to_subs.py 文件夹
  py -3.12 apply_glossary_to_subs.py 文件夹 --dry-run
  py -3.12 apply_glossary_to_subs.py 文件夹 --inplace
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = Path(__file__).parent.resolve()


def select_glossary_targets(folder: Path) -> list[Path]:
    """优先原始 *merge* / *bilingual* 字幕，排除 fit-dub、qc、配音副本。"""
    from dub_naming import discover_bilingual_subtitles, is_bilingual_sub_dub_copy

    skip = ('.fit-dub', '.term-fixed', '.quick-fit', 'qc-', '.gemini', '.dub-')
    out: list[Path] = []
    for f in folder.iterdir():
        if not f.is_file() or f.suffix.lower() != '.srt':
            continue
        if is_bilingual_sub_dub_copy(f):
            continue
        low = f.name.lower()
        if any(x in low for x in skip):
            continue
        if 'en-zh-merge' in low or 'zh-en-merge' in low or 'bilingual' in low:
            out.append(f)
    if out:
        return sorted(out, key=lambda p: p.name.lower())
    return discover_bilingual_subtitles(folder)


def process_srt(path: Path, gloss, *, dry_run: bool, inplace: bool) -> tuple[int, int]:
    from ai_shorten import split_bilingual_text
    from dub_naming import write_bilingual_srt, write_subtitle_with_study_sync
    from step1_subtitle_to_audio import parse_srt

    text = path.read_text(encoding='utf-8-sig')
    cues = parse_srt(text)
    if not cues:
        return 0, 0

    changed_cues = 0
    total_notes = 0
    new_cues = []
    for cue in cues:
        en, zh = split_bilingual_text(cue.get('text', ''))
        if not zh.strip():
            new_cues.append(cue)
            continue
        new_zh, notes = gloss.apply_to_zh(en, zh)
        if new_zh != zh:
            changed_cues += 1
            total_notes += len(notes)
            if dry_run:
                print(f'  [{path.name}] {notes[:3]}')
            body = f'{en}\n{new_zh}' if en else new_zh
            new_cues.append({**cue, 'text': body})
        else:
            new_cues.append(cue)

    if changed_cues and not dry_run:
        if inplace:
            tmp = path.with_suffix('.tmp.srt')
            write_bilingual_srt(new_cues, tmp)
            body = tmp.read_text(encoding='utf-8')
            try:
                tmp.unlink()
            except OSError:
                pass
            write_subtitle_with_study_sync(path, body)
            print(f'  [OK] 已覆盖 {path.name}（改 {changed_cues} 条）')
        else:
            out = path.with_name(f'{path.stem}.term-fixed{path.suffix}')
            write_bilingual_srt(new_cues, out)
            print(f'  [OK] {out.name}（改 {changed_cues} 条）')
    elif changed_cues and dry_run:
        print(f'  [预览] {path.name} 将改 {changed_cues} 条')
    else:
        print(f'  [跳过] {path.name} 无需替换')
    return changed_cues, total_notes


def main() -> int:
    ap = argparse.ArgumentParser(description='术语表校正双语字幕')
    ap.add_argument('folder', type=Path)
    ap.add_argument('--dry-run', action='store_true', help='仅预览不写文件')
    ap.add_argument('--inplace', action='store_true', help='覆盖原 en-zh-merge / bilingual 文件')
    ap.add_argument('--glossary-dir', type=Path, default=None)
    args = ap.parse_args()

    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f'[错误] 不是文件夹：{folder}')
        return 1

    from dub_naming import discover_bilingual_subtitles
    from terminology import reload_glossary

    gloss = reload_glossary(args.glossary_dir)
    if gloss.size == 0:
        print('[错误] 术语表为空，请检查 术语表/*.csv')
        return 1

    srts = select_glossary_targets(folder)
    if not srts:
        print(f'[错误] 未找到双语字幕：{folder}')
        return 1

    print('=' * 50)
    print('  术语表校正（零 API）')
    print(f'  文件夹：{folder}')
    print(f'  术语：{gloss.size} 条（{len(gloss.loaded_files)} 个 CSV）')
    print(f'  字幕：{len(srts)} 个')
    mode = '预览' if args.dry_run else ('覆盖' if args.inplace else '另存 *.term-fixed.srt')
    print(f'  模式：{mode}')
    print('=' * 50)

    total_cues = 0
    files_hit = 0
    for srt in srts:
        n, _ = process_srt(srt, gloss, dry_run=args.dry_run, inplace=args.inplace)
        if n:
            files_hit += 1
            total_cues += n

    print(f'\n完成：{files_hit} 个文件、共 {total_cues} 条 cue 有替换')
    if not args.dry_run and not args.inplace:
        print('  下一步：quick_bilingual_dub.bat 对 term-fixed 或原 merge 配音')
        print('  （fit-dub 优先；若无 term-fixed 则用 en-zh-merge）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
