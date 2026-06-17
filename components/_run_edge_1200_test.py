#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1200 字 Edge 分段实测：真实字幕、速度、成功率、音频时长。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent.resolve()
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cue_synth_eligibility import extract_chinese
from edge_segment_scheduler import (
    build_segments_from_cues,
    load_segment_profiles,
    print_segment_plan,
    run_edge_segment_scheduler,
    EdgeSegmentProfile,
)
from step1_subtitle_to_audio import (
    VOICE_MAP,
    load_subtitle,
    merge_cues,
    process_cue,
    FILLER_WORDS,
    SENSITIVE_WORDS,
)

CFG_PATH = HERE / 'tubeget.config.json'
SUB = HERE / '1 - Introduction' / '1 - Course Overview.en_US.bilingual.srt'
WORK = HERE / '_edge_1200_test_work'
MAX_CHARS = 1200
MIN_CHARS = 900


def mp3_duration(path: str) -> float | None:
    if not os.path.exists(path):
        return None
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', path],
        capture_output=True, text=True,
    )
    try:
        return round(float(r.stdout.strip()), 1)
    except ValueError:
        return None


def load_cues(sub_path: Path, max_cues: int | None = None) -> list[dict]:
    raw = load_subtitle(str(sub_path))
    out = []
    for cue in raw:
        p = process_cue(cue, FILLER_WORDS, SENSITIVE_WORDS, True)
        if not p:
            continue
        zh = extract_chinese(p.get('text', ''))
        if not zh.strip():
            continue
        out.append({
            'start': p['start'],
            'end': p['end'],
            'text': zh,
        })
    merged = merge_cues(out, max_gap=1.2, max_duration=7.0)
    if max_cues:
        merged = merged[:max_cues]
    return merged


def patch_config() -> str | None:
    if not CFG_PATH.exists():
        return None
    backup = CFG_PATH.read_text(encoding='utf-8')
    data = json.loads(backup)
    data['edge_segment_mode'] = True
    data['edge_segment_profile'] = 'large'
    data['edge_segment_chars_large'] = MAX_CHARS
    data['edge_segment_chars_min'] = MIN_CHARS
    data['edge_segment_timeout_large'] = 90
    data['edge_segment_concurrency_large'] = 3
    CFG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return backup


def restore_config(backup: str | None) -> None:
    if backup is not None:
        CFG_PATH.write_text(backup, encoding='utf-8')


async def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    if not SUB.exists():
        print(f'字幕不存在: {SUB}')
        return 1

    cues = load_cues(SUB, max_cues=None)
    total_zh = sum(len(c['text']) for c in cues)
    voice_id = VOICE_MAP['晓晓']
    rate = '+0%'

    profile = EdgeSegmentProfile(
        'large_1200', MAX_CHARS, MIN_CHARS, 3, 1.5, 90.0,
    )
    segments = build_segments_from_cues(
        cues,
        max_chars=MAX_CHARS,
        min_chars=MIN_CHARS,
        text_extractor=lambda c: c['text'],
    )

    print('=' * 60)
    print(f'字幕: {SUB.name}')
    print(f'句数: {len(cues)}  中文总字: {total_zh}')
    print(f'分段: {len(segments)} 段  目标 {MAX_CHARS} 字/段 (min_flush={MIN_CHARS})')
    print('=' * 60)
    print_segment_plan(segments, profile)
    print('\n开始 Edge 合成（真实 API）…\n')

    if WORK.exists():
        import shutil
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    backup = patch_config()
    t0 = time.perf_counter()
    try:
        results, failed, stats = await run_edge_segment_scheduler(
            cues, voice_id, rate, str(WORK),
            text_extractor=lambda c: c['text'],
        )
    finally:
        restore_config(backup)
    elapsed = time.perf_counter() - t0

    ok = len(results)
    fail = len(failed)
    seg_files = sorted(WORK.glob('seg_*.mp3'))

    print('\n' + '=' * 60)
    print('结果汇总')
    print('=' * 60)
    print(f'总耗时: {elapsed:.1f}s')
    print(f'成功句: {ok}/{len(cues)}  失败: {fail}')
    if ok:
        print(f'吞吐: {ok / max(elapsed, 0.1):.2f} 句/秒')
    print(f'Edge 统计: {stats}')
    print(f'\n分段 mp3 ({len(seg_files)} 个) → {WORK}')
    print(f'{"段":>4} {"字数":>6} {"句数":>4} {"合成s":>7} {"音频s":>7} 预览')
    print('-' * 60)

    for seg in segments:
        seg_mp3 = WORK / f'seg_{seg["id"]:04d}.mp3'
        dur = mp3_duration(str(seg_mp3)) if seg_mp3.exists() else None
        preview = seg['text'][:28].replace('\n', '')
        print(
            f'{seg["id"] + 1:4d} {seg["chars"]:6d} {len(seg["cue_indices"]):4d} '
            f'{"—":>7} {dur or "—":>7}  {preview}…')

    if fail:
        print(f'\n失败句索引: {failed[:20]}{"…" if len(failed) > 20 else ""}')
    else:
        print('\n全部句子合成成功。')
    print(f'\n可试听整段 mp3: {WORK}\\seg_0000.mp3 等')
    return 0 if not fail else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
