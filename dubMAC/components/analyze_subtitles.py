#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析双语字幕：长句、过短句、合并计划、Gemini 待处理数量（不调用 API）

用法：
    py -3.12 analyze_subtitles.py              # 列出章节，回车=全部
    py -3.12 analyze_subtitles.py --folders 1  # 第 1 章
    py -3.12 analyze_subtitles.py "1 - Introduction"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ai_shorten import (
    analyze_cues,
    dub_prep_config,
    merge_short_adjacent_cues,
    split_bilingual_text,
)
from dub_naming import DUB_TAG

VIDEO_EXT = {'.mp4', '.mkv', '.avi', '.mov', '.webm'}


def natural_sort_key(s: str):
    return [
        int(t) if t.isdigit() else t.lower()
        for t in re.split(r'(\d+)', s)
    ]


def list_chapter_dirs() -> list[str]:
    names = []
    for d in HERE.iterdir():
        if not d.is_dir() or d.name.startswith('.') or d.name.startswith('_'):
            continue
        if d.name == '__pycache__':
            continue
        if any(
            f.suffix.lower() in VIDEO_EXT
            for f in d.iterdir() if f.is_file()
        ):
            names.append(d.name)
    return sorted(names, key=natural_sort_key)


def load_config() -> dict:
    cfg = dub_prep_config()
    p = HERE / 'tubeget.config.json'
    if p.exists():
        try:
            with open(p, encoding='utf-8') as f:
                cfg = dub_prep_config(json.load(f))
        except Exception:
            pass
    return cfg


def load_ratio() -> float:
    p = HERE / 'tubeget.config.json'
    if p.exists():
        try:
            return float(json.loads(p.read_text(encoding='utf-8')).get(
                'zh_en_char_ratio', 1.0))
        except Exception:
            pass
    return 1.0


def parse_srt(path: str) -> list:
    with open(path, encoding='utf-8-sig') as f:
        content = f.read()
    cues = []
    for block in re.split(r'\n\s*\n', content.strip()):
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        m = re.search(
            r'(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})',
            lines[1],
        )
        if not m:
            continue

        def _sec(t: str) -> float:
            t = t.replace(',', '.')
            p = t.split(':')
            return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2])

        text = '\n'.join(lines[2:]).strip()
        if text:
            cues.append({
                'start': _sec(m.group(1)),
                'end': _sec(m.group(2)),
                'text': text,
            })
    return cues


def find_bilingual_srts(root: Path) -> list[Path]:
    out = []
    for p in root.rglob('*.srt'):
        if 'bilingual' not in p.name.lower():
            continue
        # 跳过配音输出字幕：foo.en_US.bilingual_C.srt
        if p.stem.endswith(DUB_TAG):
            continue
        out.append(p)
    return sorted(out)


def resolve_folder_names(raw: str, available: list[str]) -> list[str]:
    picked: list[str] = []
    for part in re.split(r'[,，]', raw):
        token = part.strip()
        if not token:
            continue
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(available):
                picked.append(available[idx])
            else:
                print(f'  [警告] 序号超出范围：{token}')
            continue
        low = token.lower()
        exact = [a for a in available if a.lower() == low]
        if exact:
            picked.extend(exact)
            continue
        prefix = [a for a in available if a.lower().startswith(low)]
        if len(prefix) == 1:
            picked.append(prefix[0])
        elif len(prefix) > 1:
            print(f'  [警告] 「{token}」匹配多个文件夹：')
            for p in prefix[:5]:
                print(f'    - {p}')
        else:
            print(f'  [警告] 未找到文件夹：{token}')
    out: list[str] = []
    seen: set[str] = set()
    for n in picked:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def ask_select_folders_console(available: list[str]) -> str | None:
    print(f'\n  课程目录：{HERE}')
    print(f'  共 {len(available)} 个章节文件夹：')
    for i, name in enumerate(available, 1):
        n_subs = len(find_bilingual_srts(HERE / name))
        print(f'    {i:2}. {name}  ({n_subs} 个字幕)')

    print('\n  直接回车 = 分析全部章节')
    print('  输入序号或文件夹名，逗号分隔（例：1  或  1,3）')
    print('  输入 q = 退出')
    try:
        line = input('  请选择> ').strip()
    except (EOFError, KeyboardInterrupt):
        print('  已取消')
        return None

    if line.lower() in ('q', 'quit', 'exit'):
        return None
    return line


def collect_subtitle_paths(
    path_arg: str,
    folders_arg: str,
    chapters: list[str],
) -> list[Path] | None:
    """解析路径 / 章节选择，返回待分析的字幕列表。"""
    if path_arg:
        p = Path(path_arg)
        if not p.is_absolute():
            p = (HERE / path_arg).resolve()
        if p.is_file():
            return [p]
        if p.is_dir():
            subs = find_bilingual_srts(p)
            return subs if subs else []

        # 当作章节序号或名称
        names = resolve_folder_names(path_arg, chapters)
        if names:
            subs: list[Path] = []
            for name in names:
                subs.extend(find_bilingual_srts(HERE / name))
            return subs
        return None

    raw = folders_arg.strip()
    if not raw:
        raw = ask_select_folders_console(chapters)
        if raw is None:
            return None

    if not raw.strip():
        return find_bilingual_srts(HERE)

    names = resolve_folder_names(raw, chapters)
    if not names:
        return []
    subs = []
    for name in names:
        subs.extend(find_bilingual_srts(HERE / name))
    return subs


def show_samples(cues: list, indices: list[int], title: str, limit: int = 5) -> None:
    if not indices:
        return
    print(f'  {title}（示例 {min(limit, len(indices))} 条）：')
    for i in indices[:limit]:
        en, zh = split_bilingual_text(cues[i]['text'])
        win = cues[i]['end'] - cues[i]['start']
        if len(zh) > 40:
            print(f'    #{i + 1} [{win:.2f}s] {len(zh)}字 | {zh[:40]}...')
        else:
            print(f'    #{i + 1} [{win:.2f}s] {len(zh)}字 | {zh}')


def analyze_file(sub: Path, cfg: dict, ratio: float) -> None:
    cues = parse_srt(str(sub))
    if not cues:
        print(f'  [空] {sub}')
        return

    before = analyze_cues(cues, ratio, cfg, label='before')
    merged, prep = merge_short_adjacent_cues(cues, cfg)
    after = analyze_cues(merged, ratio, cfg, label='after')

    print(f'\n{"=" * 60}')
    try:
        rel = sub.relative_to(HERE)
    except ValueError:
        rel = sub
    print(f'  {rel}')
    print(f'  原始：{before["total_zh"]} 条中文')
    print(f'    长句（超字数/时长）：{before["long"]}')
    print(f'    过短（易一晃而过）：{before["short_rush"]}')
    if prep['merged_pairs']:
        print(
            f'  合并后：{prep["before"]} → {prep["after"]} 条'
            f'（{prep["merged_pairs"]} 对）')
    print(f'    待 Gemini：{after["gemini_targets"]}/{after["total_zh"]} 条')
    print(f'    可跳过：{after["ok"]} 条')

    show_samples(cues, before['long_indices'], '长句')
    show_samples(cues, before['short_indices'], '过短句')
    if after['merge_indices']:
        show_samples(merged, after['merge_indices'], '合并后待 Gemini 修整')


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    parser = argparse.ArgumentParser(description='分析字幕长句/短句/Gemini 用量')
    parser.add_argument(
        'path', nargs='?', default='',
        help='可选：章节文件夹 / .srt 路径 / 序号',
    )
    parser.add_argument(
        '--folders', default='',
        help='章节序号或文件夹名，逗号分隔（不弹交互）',
    )
    args = parser.parse_args()

    cfg = load_config()
    ratio = load_ratio()
    chapters = list_chapter_dirs()

    print('配置：')
    print(f'  min_cue_sec={cfg["min_cue_sec"]}  '
          f'short_zh_merge_max_chars={cfg["short_zh_merge_max_chars"]}')
    print(f'  gemini_only_problem_cues={cfg["only_problem_cues"]}')

    if not chapters:
        print('\n  [错误] 当前目录下没有含视频的章节文件夹')
        sys.exit(1)

    subs = collect_subtitle_paths(args.path, args.folders, chapters)
    if subs is None:
        sys.exit(0)
    if not subs:
        print('\n  未找到 bilingual.srt')
        sys.exit(1)

    print(f'\n  将分析 {len(subs)} 个字幕文件')
    for sub in subs:
        analyze_file(sub, cfg, ratio)
    print(f'\n{"=" * 60}')
    print(f'  [OK] 分析完成，共 {len(subs)} 个文件')


if __name__ == '__main__':
    main()
