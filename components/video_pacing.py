#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全片配速（马拉松配速）：从原文字幕统计说话节奏 → 设定配音基准语速 → 句级微调。

课程类视频以每条字幕 start 为锚点，保证「说到哪、画面到哪」；整片变速仅作无感知修正。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Callable

from ai_shorten import (
    cue_source_text,
    cue_speech_params,
    en_char_count,
    format_rate_pct,
    split_bilingual_text,
    zh_char_count,
)


def pacing_config(cfg: dict | None = None) -> dict:
    c = cfg or {}
    zh = float(c.get('pacing_zh_normal_cps', c.get('zh_chars_per_sec', 3.2)))
    return {
        'enabled': bool(c.get('pacing_enabled', True)),
        'reference_en_cps': float(c.get('pacing_reference_en_cps', 12.0)),
        'zh_normal_cps': zh,
        'max_playback_speed': float(c.get('max_playback_speed', 1.5)),
        'video_speed_min': float(c.get('pacing_video_speed_min', 0.98)),
        'video_speed_max': float(c.get('pacing_video_speed_max', 1.05)),
        'max_base_rate_pct': int(c.get('pacing_max_base_rate_pct', 10)),
        'max_cue_rate_pct': int(c.get('pacing_max_cue_rate_pct', c.get('max_rate_pct', 15))),
        'video_adjust': bool(c.get('pacing_video_adjust', True)),
        'imperceptible_threshold': float(c.get('pacing_imperceptible_threshold', 0.012)),
    }


def _zh_from_cue(cue: dict) -> str:
    from cue_synth_eligibility import chinese_speech_text
    tts = (cue.get('tts_text') or '').strip()
    if tts:
        return tts
    _, zh = split_bilingual_text(cue.get('text', ''))
    if zh:
        return zh
    return chinese_speech_text(cue.get('text', ''))


def analyze_source_pacing(cues: list, cfg: dict | None = None) -> dict:
    """
    从原文字幕统计总说话时长、总字数 → 平均语速 → 中文等效配速。
    """
    pc = pacing_config(cfg)
    total_sec = 0.0
    total_chars = 0
    cue_count = 0

    for cue in cues:
        src = cue_source_text(cue)
        if not src.strip():
            continue
        dur = float(cue.get('end', 0)) - float(cue.get('start', 0))
        if dur < 0.08:
            continue
        total_sec += dur
        total_chars += en_char_count(src)
        cue_count += 1

    ref_en = pc['reference_en_cps']
    zh_normal = pc['zh_normal_cps']

    if total_sec < 1.0 or total_chars < 5:
        return {
            'enabled': pc['enabled'],
            'source': 'default',
            'total_speech_sec': round(total_sec, 2),
            'total_en_chars': total_chars,
            'cue_count': cue_count,
            'source_cps': 0.0,
            'pace_ratio': 1.0,
            'target_zh_cps': zh_normal,
            'effective_zh_cps': zh_normal,
            'base_rate': '+0%',
            'base_rate_pct': 0,
            'video_speed_factor': 1.0,
        }

    source_cps = total_chars / total_sec
    pace_ratio = source_cps / max(ref_en, 0.1)
    pace_ratio = max(0.75, min(1.35, pace_ratio))

    target_zh_cps = zh_normal * pace_ratio
    base_pct = int(round((pace_ratio - 1.0) * 100))
    base_pct = max(-6, min(pc['max_base_rate_pct'], base_pct))
    effective_zh_cps = zh_normal * (1.0 + base_pct / 100.0)

    return {
        'enabled': pc['enabled'],
        'source': 'subtitle',
        'total_speech_sec': round(total_sec, 2),
        'total_en_chars': total_chars,
        'cue_count': cue_count,
        'source_cps': round(source_cps, 3),
        'reference_en_cps': ref_en,
        'pace_ratio': round(pace_ratio, 4),
        'target_zh_cps': round(target_zh_cps, 3),
        'effective_zh_cps': round(effective_zh_cps, 3),
        'base_rate': format_rate_pct(base_pct),
        'base_rate_pct': base_pct,
        'video_speed_factor': 1.0,
    }


def apply_cue_tts_rates(
    cues: list,
    pacing: dict,
    cfg: dict | None = None,
) -> list[dict]:
    """在基准配速上为每条 cue 计算 Edge TTS rate（句级微调）。"""
    pc = pacing_config(cfg)
    base_rate = pacing.get('base_rate', '+0%')
    cps = float(pacing.get('effective_zh_cps', pc['zh_normal_cps']))
    max_spd = pc['max_playback_speed']

    for cue in cues:
        zh = _zh_from_cue(cue)
        window = max(0.3, float(cue.get('end', 0)) - float(cue.get('start', 0)))
        rate_str, _factor, extra = cue_speech_params(
            zh,
            window,
            base_rate,
            chars_per_sec=cps,
            max_speed=max_spd,
            max_rate_pct=pc['max_cue_rate_pct'],
        )
        cue['tts_rate'] = rate_str
        cue['pacing_extra_pct'] = extra
    return cues


def rate_for_segment(cues: list[dict], cue_indices: list[int], default_rate: str) -> str:
    """分段 TTS 取段内最高语速（偏快侧，避免超窗）。"""
    best_pct = -999
    best = default_rate
    for i in cue_indices:
        if i < 0 or i >= len(cues):
            continue
        r = cues[i].get('tts_rate') or default_rate
        try:
            pct = int(str(r).replace('%', '').replace('+', '').strip())
        except ValueError:
            pct = 0
        if pct > best_pct:
            best_pct = pct
            best = r
    return best


def compute_dub_timeline_end(
    placements: list[tuple],
    *,
    get_duration: Callable[[str], float],
) -> float:
    end = 0.0
    for item in placements:
        if len(item) < 5:
            continue
        _idx, _os, _oe, mp3_path, play_start = item[:5]
        if not mp3_path:
            continue
        dur = get_duration(mp3_path)
        if dur <= 0:
            continue
        end = max(end, play_start + dur)
    return end


def compute_video_speed_factor(
    *,
    subtitle_span: float,
    dub_timeline_end: float,
    video_dur: float,
    cfg: dict | None = None,
) -> float:
    """
    整片无感知变速：配音时间轴与字幕跨度/视频时长偏差很小时才微调。
    factor>1 → 成片略快（缩短），factor<1 → 略慢。
    """
    pc = pacing_config(cfg)
    if not pc['enabled'] or not pc['video_adjust']:
        return 1.0
    if subtitle_span < 30 or dub_timeline_end < 30:
        return 1.0

    # 以字幕内容跨度为主锚；片尾无字幕时参考视频全长
    target = subtitle_span
    if video_dur > subtitle_span + 5.0:
        tail = video_dur - subtitle_span
        if tail / video_dur > 0.15:
            target = video_dur * 0.92 + subtitle_span * 0.08

    raw = dub_timeline_end / max(target, 1.0)
    if raw <= 1.0 + pc['imperceptible_threshold']:
        factor = 1.0
    else:
        factor = raw

    lo, hi = pc['video_speed_min'], pc['video_speed_max']
    factor = max(lo, min(hi, factor))

    if abs(factor - 1.0) < pc['imperceptible_threshold']:
        return 1.0
    return round(factor, 4)


def pacing_report_path_for_video(video: Path) -> Path:
    return video.parent / f'{video.stem}.pacing.json'


def save_pacing_report(path: Path, report: dict) -> Path:
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    return path


def load_pacing_report(path: Path | str) -> dict | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None


def print_pacing_summary(pacing: dict) -> None:
    if not pacing.get('enabled', True):
        print('  [配速] 已关闭（pacing_enabled=false）')
        return
    src = pacing.get('source', '?')
    if src == 'default':
        print('  [配速] 原文字幕不足，使用默认 +0% 语速')
        return
    print(
        f'  [配速] 原文 {pacing["total_en_chars"]} 字 / '
        f'{pacing["total_speech_sec"]:.0f}s → '
        f'{pacing["source_cps"]:.2f} 字/秒（参考 {pacing.get("reference_en_cps", 12)}）'
    )
    print(
        f'  [配速] 节奏比 {pacing["pace_ratio"]:.2f} → '
        f'基准语速 {pacing["base_rate"]}，'
        f'等效中文 {pacing["effective_zh_cps"]:.2f} 字/秒'
    )
    vf = pacing.get('video_speed_factor', 1.0)
    if vf and abs(vf - 1.0) >= 0.008:
        pct = (vf - 1.0) * 100
        print(f'  [配速] 整片微调 {vf:.3f}×（约 {pct:+.1f}%，配音+画面同步变速）')
    else:
        print('  [配速] 整片变速：无需调整（偏差在感知阈值内）')


def analyze_and_prepare(
    source_cues: list,
    dub_cues: list,
    cfg: dict | None = None,
) -> dict:
    """分析原文字幕并为配音 cue 写入 tts_rate。"""
    pc = pacing_config(cfg)
    pacing = analyze_source_pacing(source_cues, cfg)
    if pc['enabled'] and dub_cues:
        apply_cue_tts_rates(dub_cues, pacing, cfg)
        extras = [c.get('pacing_extra_pct', 0) for c in dub_cues]
        pacing['cue_count_dub'] = len(dub_cues)
        pacing['cue_extra_avg_pct'] = round(
            sum(extras) / max(len(extras), 1), 2)
        pacing['cue_extra_max_pct'] = max(extras) if extras else 0
    return pacing
