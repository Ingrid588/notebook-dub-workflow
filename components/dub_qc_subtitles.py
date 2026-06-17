#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配音质检字幕：与最终中文声对齐，便于观看时查缺句/截断。

输出（与 _C 成片同目录）：
  {stem}_C.qc-zh.srt       纯中文，带 [#序号] 与状态标记
  {stem}_C.qc-bilingual.srt  英+中（与 qc-zh 同时间轴）
  {stem}_C.qc-report.json   缺句/可能截断/未完 汇总
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dub_naming import dub_stem, format_srt_time


def dub_qc_zh_path(video: Path) -> Path:
    return video.parent / f'{dub_stem(video.stem)}.qc-zh.srt'


def dub_qc_bilingual_path(video: Path) -> Path:
    return video.parent / f'{dub_stem(video.stem)}.qc-bilingual.srt'


def dub_qc_report_path(video: Path) -> Path:
    return video.parent / f'{dub_stem(video.stem)}.qc-report.json'


def _dub_en_text(cue: dict) -> str:
    text = str(cue.get('text', '')).strip()
    if not text:
        return ''
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) >= 2:
        return lines[0]
    try:
        from ai_shorten import split_bilingual_text
        en, _ = split_bilingual_text(text)
        return (en or '').strip()
    except Exception:
        return ''


def _dub_zh_text(cue: dict) -> str:
    if cue.get('zh_dub'):
        return str(cue['zh_dub']).strip()
    text = str(cue.get('text', '')).strip()
    if not text:
        return ''
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) >= 2:
        return lines[1]
    try:
        from ai_shorten import split_bilingual_text
        _, zh = split_bilingual_text(text)
        if zh:
            return zh.strip()
    except Exception:
        pass
    try:
        from cue_synth_eligibility import extract_chinese
        return extract_chinese(text).strip()
    except Exception:
        return text


def _mp3_ok(path: str | None) -> bool:
    return bool(
        path and os.path.exists(path)
        and os.path.getsize(path) > 500)


def _audio_duration_sec(path: str) -> float:
    try:
        from step1_subtitle_to_audio import get_audio_duration
        return float(get_audio_duration(path))
    except Exception:
        return 0.0


def _strict_window(cfg: dict | None) -> bool:
    try:
        from step1_subtitle_to_audio import _speech_strict_window_enabled
        return bool(_speech_strict_window_enabled(cfg or {}))
    except Exception:
        return bool((cfg or {}).get('speech_window_soft', True))


def _continuation_slack(cfg: dict | None) -> float:
    return float((cfg or {}).get('speech_activity_continuation_slack_sec', 1.5))


def build_dub_qc_payload(
    dub_cues: list[dict],
    results: list,
    synced_cues: list[dict],
    failed_indices: list[int] | None = None,
    *,
    cfg: dict | None = None,
    sa_tts_stats: dict | None = None,
    video: Path | str | None = None,
) -> dict[str, Any]:
    """在删除 dub-work 前调用（需能读取各句 mp3 时长）。"""
    from step1_subtitle_to_audio import zh_has_continuation_mark

    cfg = cfg or {}
    failed = set(failed_indices or [])
    result_mp3 = {i: p for i, _, _, p in results}
    strict = _strict_window(cfg)
    slack = _continuation_slack(cfg)
    fit_actions: dict = {}
    if sa_tts_stats:
        fit_actions = sa_tts_stats.get('fit_actions') or {}

    rows: list[dict] = []
    missing: list[int] = []
    maybe_trimmed: list[int] = []  # 保留字段兼容；不再截断音频
    continuation: list[int] = []
    speedup_rows: list[int] = []

    for i, cue in enumerate(dub_cues):
        sync = synced_cues[i] if i < len(synced_cues) else cue
        zh = _dub_zh_text(cue)
        en = _dub_en_text(cue)
        mp3 = result_mp3.get(i)
        has_audio = _mp3_ok(mp3)
        flags: list[str] = []

        orig_start = float(cue.get('start', 0))
        orig_end = float(cue.get('end', 0))
        win_sec = max(0.05, orig_end - orig_start)
        play_start = float(sync.get('start', orig_start))
        play_end = float(sync.get('end', orig_end))
        audio_sec = _audio_duration_sec(mp3) if has_audio else 0.0

        if i in failed or not has_audio:
            flags.append('缺句')
            missing.append(i)
        else:
            cap = win_sec + (
                slack if zh_has_continuation_mark(zh) else 0.0)
            action = fit_actions.get(i) or ''
            if action == 'needs_regenerate':
                flags.append('超窗·已送Gemini')
                speedup_rows.append(i)
            elif action in ('speedup', 'speedup_extended', 'over_window_kept'):
                flags.append('加速')
                speedup_rows.append(i)
            elif strict and audio_sec > cap + 0.08:
                flags.append('超窗·保留全长')

        if zh_has_continuation_mark(zh):
            flags.append('未完')
            if i not in continuation:
                continuation.append(i)

        tag = f'#{i + 1:03d}'
        if flags:
            tag += '·' + '·'.join(flags)

        rows.append({
            'index': i,
            'cue_no': i + 1,
            'tag': tag,
            'flags': flags,
            'zh': zh,
            'en': en,
            'start': play_start,
            'end': play_end,
            'orig_start': orig_start,
            'orig_end': orig_end,
            'window_sec': round(win_sec, 3),
            'audio_sec': round(audio_sec, 3),
            'has_audio': has_audio,
            'source_cue': cue.get('source_cue'),
            'unit_idx': cue.get('unit_idx'),
        })

    return {
        'version': 1,
        'video': str(video) if video else '',
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'total_units': len(rows),
        'missing_indices': missing,
        'maybe_trimmed_indices': maybe_trimmed,
        'continuation_indices': continuation,
        'speedup_indices': speedup_rows,
        'strict_window': strict,
        'rows': rows,
    }


def _write_srt(blocks: list[tuple[float, float, str]], path: Path) -> None:
    lines: list[str] = []
    for n, (start, end, text) in enumerate(blocks, 1):
        body = (text or '').strip()
        if not body:
            continue
        lines.append(
            f'{n}\n'
            f'{format_srt_time(start)} --> {format_srt_time(end)}\n'
            f'{body}\n'
        )
    path.write_text('\n'.join(lines), encoding='utf-8')


def write_dub_qc_files(
    video: Path,
    payload: dict[str, Any],
    *,
    quiet: bool = False,
) -> tuple[Path, Path, Path]:
    """写出 qc-zh / qc-bilingual / qc-report。"""
    zh_path = dub_qc_zh_path(video)
    bil_path = dub_qc_bilingual_path(video)
    rep_path = dub_qc_report_path(video)

    zh_blocks: list[tuple[float, float, str]] = []
    bil_blocks: list[tuple[float, float, str]] = []

    for row in payload.get('rows') or []:
        start = float(row['start'])
        end = max(float(row['end']), start + 0.35)
        tag = row.get('tag') or f"#{row.get('cue_no', 0):03d}"
        zh = row.get('zh') or ''
        en = row.get('en') or ''
        zh_blocks.append((start, end, f'[{tag}] {zh}'.strip()))
        if en and zh:
            bil_blocks.append((start, end, f'[{tag}]\n{en}\n{zh}'))
        elif zh:
            bil_blocks.append((start, end, f'[{tag}]\n{zh}'))
        elif en:
            bil_blocks.append((start, end, f'[{tag}]\n{en}'))

    _write_srt(zh_blocks, zh_path)
    _write_srt(bil_blocks, bil_path)

    summary = {
        k: payload.get(k)
        for k in (
            'version', 'video', 'generated_at', 'total_units',
            'missing_indices', 'maybe_trimmed_indices',
            'continuation_indices', 'speedup_indices', 'strict_window',
        )
    }
    summary['missing_count'] = len(payload.get('missing_indices') or [])
    summary['maybe_trimmed_count'] = len(payload.get('maybe_trimmed_indices') or [])
    summary['continuation_count'] = len(payload.get('continuation_indices') or [])
    summary['legend'] = {
        '缺句': '未合成或 mp3 无效，成片该时段为静音',
        '超窗·已送Gemini': '1.5× 加速仍明显超窗，已送 Gemini 压缩并重合成',
        '超窗·保留全长': '音频长于时间窗，已加速但未截断，完整保留语义',
        '未完': '句尾有 ——，表示与下一句连读',
        '加速': 'Speech Activity 窗口内加速对齐',
    }
    summary['rows'] = [
        {
            'cue_no': r['cue_no'],
            'flags': r.get('flags') or [],
            'zh_preview': (r.get('zh') or '')[:80],
            'start': r['start'],
            'end': r['end'],
            'audio_sec': r.get('audio_sec'),
            'window_sec': r.get('window_sec'),
            'has_audio': r.get('has_audio'),
        }
        for r in (payload.get('rows') or [])
    ]
    rep_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    if not quiet:
        miss = summary['missing_count']
        trim = summary['maybe_trimmed_count']
        print(
            f'  质检字幕：{zh_path.name}'
            f'（{len(zh_blocks)} 条，缺句 {miss}）')
        print(f'             双语：{bil_path.name}')
        print(f'             报告：{rep_path.name}')
        if miss:
            nos = [i + 1 for i in (payload.get('missing_indices') or [])[:12]]
            tail = '…' if miss > 12 else ''
            print(f'             缺句序号：{nos}{tail}')

    return zh_path, bil_path, rep_path


def save_dub_qc_subtitles(
    video: Path,
    dub_cues: list[dict],
    results: list,
    synced_cues: list[dict],
    *,
    failed_indices: list[int] | None = None,
    cfg: dict | None = None,
    sa_tts_stats: dict | None = None,
    quiet: bool = False,
) -> tuple[Path, Path, Path] | None:
    if not (cfg or {}).get('generate_dub_qc_subtitles', True):
        return None
    payload = build_dub_qc_payload(
        dub_cues, results, synced_cues, failed_indices,
        cfg=cfg, sa_tts_stats=sa_tts_stats, video=video,
    )
    return write_dub_qc_files(video, payload, quiet=quiet)
