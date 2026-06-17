#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Edge 分段合成对比测试
====================
用法：
  py -3.12 test_edge_segment.py 字幕.srt
  py -3.12 test_edge_segment.py 字幕.srt --profiles small,medium
  py -3.12 test_edge_segment.py 字幕.srt --max-cues 30

依次用 small / medium / large 各合成前几段，输出耗时与成功率，便于选最优 profile。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent.resolve()
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from edge_segment_scheduler import (
    build_segments_from_cues,
    load_segment_profiles,
    print_segment_plan,
    run_edge_segment_scheduler,
)
from step1_subtitle_to_audio import (
    VOICE_MAP,
    load_subtitle,
    merge_cues,
    process_cue,
    FILLER_WORDS,
    SENSITIVE_WORDS,
)


async def _run_profile(
    cues: list[dict],
    profile_name: str,
    voice_id: str,
    rate: str,
    work_dir: str,
    only_indices: set[int],
) -> dict:
    import json
    cfg_path = HERE / 'tubeget.config.json'
    backup = None
    if cfg_path.exists():
        backup = cfg_path.read_text(encoding='utf-8')
    data = json.loads(backup or '{}')
    data['edge_segment_mode'] = True
    data['edge_segment_profile'] = profile_name
    cfg_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8',
    )
    t0 = time.perf_counter()
    try:
        results, failed, stats = await run_edge_segment_scheduler(
            cues, voice_id, rate, work_dir,
            only_indices=only_indices,
            text_extractor=lambda c: c.get('text', ''),
        )
    finally:
        if backup is not None:
            cfg_path.write_text(backup, encoding='utf-8')
    elapsed = time.perf_counter() - t0
    return {
        'profile': profile_name,
        'elapsed': elapsed,
        'success': len(results),
        'failed': len(failed),
        'stats': stats,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description='Edge 分段 profile 对比测试')
    ap.add_argument('subtitle', help='.srt / .vtt 字幕路径')
    ap.add_argument('--voice', default='晓晓')
    ap.add_argument('--rate', default='+0%')
    ap.add_argument(
        '--profiles', default='small,medium',
        help='逗号分隔：small,medium,large,auto',
    )
    ap.add_argument('--max-cues', type=int, default=40,
                    help='最多测试前 N 句（默认 40）')
    args = ap.parse_args()

    voice_id = VOICE_MAP.get(args.voice, VOICE_MAP['晓晓'])
    raw = load_subtitle(args.subtitle)
    if not raw:
        print('字幕为空')
        sys.exit(1)

    processed = []
    for cue in raw:
        p = process_cue(cue, FILLER_WORDS, SENSITIVE_WORDS, True)
        if p:
            processed.append(p)
    merged = merge_cues(processed, max_gap=1.2, max_duration=7.0)
    cues = merged[: args.max_cues]
    only = set(range(len(cues)))

    profiles = load_segment_profiles()
    print(f'测试字幕：{Path(args.subtitle).name}')
    print(f'句数：{len(cues)}（截断自 merge_cues 后）\n')

    for name in ['small', 'medium', 'large']:
        if name not in profiles:
            continue
        segs = build_segments_from_cues(
            cues,
            max_chars=profiles[name].max_chars,
            min_chars=profiles[name].min_chars,
            text_extractor=lambda c: c.get('text', ''),
            only_indices=only,
        )
        print(f'--- {name} 计划 ---')
        print_segment_plan(segs, profiles[name])

    print('\n开始实测（会真实调用 Edge）…\n')
    rows = []
    for pname in args.profiles.split(','):
        pname = pname.strip()
        if not pname:
            continue
        tmp = tempfile.mkdtemp(prefix='edge_seg_test_')
        try:
            row = asyncio.run(_run_profile(
                cues, pname, voice_id, args.rate, tmp, only,
            ))
            rows.append(row)
            rate_pct = row['success'] * 100 / max(row['success'] + row['failed'], 1)
            print(
                f'\n[{pname}] {row["elapsed"]:.1f}s  '
                f'成功 {row["success"]} 失败 {row["failed"]} '
                f'({rate_pct:.0f}%)\n',
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if rows:
        print('=' * 50)
        print('对比汇总（越快且成功率越高越好）')
        for r in rows:
            total = r['success'] + r['failed']
            pct = r['success'] * 100 / max(total, 1)
            spd = r['success'] / max(r['elapsed'], 0.1)
            print(
                f"  {r['profile']:8s}  {r['elapsed']:6.1f}s  "
                f"成功率 {pct:5.1f}%  {spd:.2f} 句/秒",
            )
        best = max(
            rows,
            key=lambda x: (x['success'] / max(x['success'] + x['failed'], 1),
                           -x['elapsed']),
        )
        print(f'\n建议优先试用 profile：{best["profile"]}')
        print('可在 tubeget.config.json 设 "edge_segment_profile": "<name>"')
        print('auto = 先 small 试跑 3 段，全成功再升 medium')


if __name__ == '__main__':
    main()
