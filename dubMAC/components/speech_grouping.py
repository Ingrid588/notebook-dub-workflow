#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Natural Speech Grouping（V3）

优先保证自然口播：按语义停顿与英文未完句合并字幕组，
而非按字幕时间窗机械切分。

自然度 > 内容完整 > 节奏合理 > 时间同步 > 口型同步
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

GROUPING_DEFAULTS: dict[str, Any] = {
    'speech_natural_grouping': True,
    'speech_merge_gap_sec': 2.0,
    'speech_grouping_must_merge_gap_sec': 1.0,
    'speech_grouping_hard_pause_sec': 3.0,
    'speech_group_max_duration_sec': 25.0,
    'vad_preserve_pause': True,
    'long_pause_sec': 2.0,
}

# Rule 4：英文句末明显未完
_INCOMPLETE_EN_RE = re.compile(
    r'(?:'
    r'\b(?:i think|because|and then|what this means is|'
    r'and|but|or|so|if|when|while|although|however|'
    r'that|which|who|where|while|as|since|until|unless|'
    r'for example|such as|in other words|on the other hand|'
    r'the thing is|what i mean|let me|you know|you see'
    r')\s*$'
    r'|[,;:—–-]\s*$'
    r')',
    re.IGNORECASE,
)


def grouping_config(cfg: dict | None = None) -> dict:
    out = dict(GROUPING_DEFAULTS)
    if not cfg:
        return out
    for k in GROUPING_DEFAULTS:
        if k in cfg:
            out[k] = cfg[k]
    if 'speech_activity_long_pause' in cfg and 'long_pause_sec' not in cfg:
        out['long_pause_sec'] = cfg['speech_activity_long_pause']
    return out


def en_sentence_incomplete(en: str) -> bool:
    en = (en or '').strip()
    if not en:
        return False
    if _INCOMPLETE_EN_RE.search(en):
        return True
    # 无句末标点且较短 → 可能未完
    if not re.search(r'[.!?…"\')\]]\s*$', en) and len(en.split()) <= 6:
        return True
    return False


def _vad_blocks_merge(
    a_end: float,
    b_start: float,
    vad: dict | None,
    long_pause_sec: float,
) -> bool:
    """VAD 检测到长停顿区落在两 cue 之间 → 禁止合并（无 VAD 时由字幕 gap 规则决定）。"""
    if not vad:
        return False
    for p in vad.get('pause_segments') or []:
        dur = float(p.get('duration', 0))
        kind = p.get('kind', '')
        if kind != 'long' and dur < long_pause_sec:
            continue
        ps, pe = float(p['start']), float(p['end'])
        if pe > a_end - 0.05 and ps < b_start + 0.05:
            return True
    return False


def _merge_decision(
    buf: dict,
    cur: dict,
    gap: float,
    group_dur: float,
    *,
    cfg: dict,
    vad: dict | None,
) -> tuple[bool, str]:
    gc = grouping_config(cfg)
    merge_gap = float(gc['speech_merge_gap_sec'])
    must_merge = float(gc['speech_grouping_must_merge_gap_sec'])
    hard_pause = float(gc['speech_grouping_hard_pause_sec'])
    max_dur = float(gc['speech_group_max_duration_sec'])
    long_pause = float(gc['long_pause_sec'])
    preserve_vad = bool(gc['vad_preserve_pause'])

    from ai_shorten import split_bilingual_text, zh_ends_sentence

    if gap > hard_pause:
        return False, 'hard_pause_gt_3s'

    en_buf, _ = split_bilingual_text(buf.get('text', ''))

    if gap < must_merge:
        return True, 'must_merge_lt_1s'

    if en_sentence_incomplete(en_buf) or (
            en_buf and not zh_ends_sentence(buf.get('text', ''))):
        if preserve_vad and _vad_blocks_merge(
                buf['end'], cur['start'], vad, long_pause):
            return False, 'vad_long_pause'
        return True, 'incomplete_sentence'

    if preserve_vad and _vad_blocks_merge(buf['end'], cur['start'], vad, long_pause):
        return False, 'vad_long_pause'

    if group_dur > max_dur:
        return False, 'group_max_duration'

    if gap <= merge_gap and not zh_ends_sentence(buf.get('text', '')):
        return True, 'prefer_merge_1_2s'

    if gap > merge_gap:
        return False, 'gap_over_merge_default'

    return False, 'keep_pause'


def natural_speech_grouping(
    cues: list,
    vad: dict | None = None,
    cfg: dict | None = None,
) -> tuple[list, dict]:
    """
    按 V3 规则将相邻字幕合并为自然口播组。
    返回 (分组后 cues, 统计)。
    """
    from ai_shorten import combine_bilingual_text, split_bilingual_text

    gc = grouping_config(cfg)
    if not gc.get('speech_natural_grouping', True) or not cues:
        return cues, {'enabled': False, 'before': len(cues), 'after': len(cues)}

    before = len(cues)
    boundaries: list[dict] = []
    out: list[dict] = []
    buf = dict(cues[0])
    buf['_group_sources'] = 1
    merged_count = 0
    merge_reasons: dict[str, int] = {}

    for cur in cues[1:]:
        gap = round(float(cur['start']) - float(buf['end']), 3)
        group_dur = float(cur['end']) - float(buf['start'])
        do_merge, reason = _merge_decision(
            buf, cur, gap, group_dur, cfg=cfg or {}, vad=vad)

        boundaries.append({
            'gap_sec': gap,
            'merged': do_merge,
            'reason': reason,
            'buf_end': buf['end'],
            'cur_start': cur['start'],
        })

        if do_merge:
            en_a, zh_a = split_bilingual_text(buf['text'])
            en_b, zh_b = split_bilingual_text(cur['text'])
            buf = {
                **buf,
                'end': cur['end'],
                'text': combine_bilingual_text(en_a, zh_a, en_b, zh_b),
                '_group_sources': int(buf.get('_group_sources', 1)) + 1,
            }
            merged_count += 1
            merge_reasons[reason] = merge_reasons.get(reason, 0) + 1
        else:
            out.append(buf)
            buf = dict(cur)
            buf['_group_sources'] = 1

    out.append(buf)

    gaps = [b['gap_sec'] for b in boundaries]
    stats = {
        'enabled': True,
        'version': 3,
        'before': before,
        'after': len(out),
        'merged_pairs': merged_count,
        'boundaries': boundaries,
        'merge_reasons': merge_reasons,
        'gaps': _summarize_gaps(gaps, boundaries),
        'config': {
            'speech_merge_gap_sec': gc['speech_merge_gap_sec'],
            'speech_grouping_hard_pause_sec': gc['speech_grouping_hard_pause_sec'],
            'vad_preserve_pause': gc['vad_preserve_pause'],
            'long_pause_sec': gc['long_pause_sec'],
        },
    }
    return out, stats


def _summarize_gaps(gaps: list[float], boundaries: list[dict]) -> dict:
    if not gaps:
        return {
            'count': 0, 'avg_gap_sec': 0.0, 'max_gap_sec': 0.0,
            'buckets': {}, 'cross_group_count': 0,
            'preserve_pause_count': 0,
        }
    buckets = {
        'lt_1s': {'count': 0, 'merged': 0},
        '1_to_2s': {'count': 0, 'merged': 0},
        '2_to_3s': {'count': 0, 'merged': 0},
        'gt_3s': {'count': 0, 'merged': 0},
    }
    for g, b in zip(gaps, boundaries):
        merged = bool(b.get('merged'))
        if g < 1.0:
            key = 'lt_1s'
        elif g <= 2.0:
            key = '1_to_2s'
        elif g <= 3.0:
            key = '2_to_3s'
        else:
            key = 'gt_3s'
        buckets[key]['count'] += 1
        if merged:
            buckets[key]['merged'] += 1

    def _pct(n: int, d: int) -> float:
        return round(n / max(d, 1) * 100, 2)

    bucket_out = {}
    for key, v in buckets.items():
        bucket_out[key] = {
            'count': v['count'],
            'merged': v['merged'],
            'pct_of_gaps': _pct(v['count'], len(gaps)),
        }

    preserve = sum(1 for b in boundaries if not b.get('merged'))
    long_preserve = sum(
        1 for g, b in zip(gaps, boundaries)
        if not b.get('merged') and g > 2.0)

    return {
        'count': len(gaps),
        'avg_gap_sec': round(sum(gaps) / len(gaps), 3),
        'max_gap_sec': round(max(gaps), 3),
        'buckets': bucket_out,
        'cross_group_count': preserve,
        'preserve_pause_count': preserve,
        'long_pause_preserve_count': long_preserve,
        'long_pause_preserve_rate': round(
            long_preserve / max(sum(1 for g in gaps if g > 2.0), 1), 4),
    }


def build_speech_grouping_report(
    original_count: int,
    grouped_cues: list,
    grouping_stats: dict,
    *,
    vad: dict | None = None,
) -> dict:
    """生成 speech_grouping_report.json 内容。"""
    gs = grouping_stats.get('gaps') or {}
    buckets = gs.get('buckets') or {}
    group_sizes = [int(c.get('_group_sources', 1)) for c in grouped_cues]

    return {
        'version': 3,
        'summary': {
            'original_subtitle_count': original_count,
            'group_count': len(grouped_cues),
            'avg_cues_per_group': round(
                sum(group_sizes) / max(len(group_sizes), 1), 2),
            'max_group_size': max(group_sizes) if group_sizes else 0,
            'merged_pairs': grouping_stats.get('merged_pairs', 0),
            'avg_gap_sec': gs.get('avg_gap_sec', 0),
            'max_gap_sec': gs.get('max_gap_sec', 0),
            'cross_group_count': gs.get('cross_group_count', 0),
            'preserve_pause_count': gs.get('preserve_pause_count', 0),
            'long_pause_preserve_rate': gs.get('long_pause_preserve_rate', 0),
        },
        'gap_distribution': buckets,
        'merge_reasons': grouping_stats.get('merge_reasons') or {},
        'config': grouping_stats.get('config') or {},
        'vad_summary': (vad or {}).get('stats'),
    }


def report_path_for_video(video: str | Path) -> Path:
    return Path(video).parent / f'{Path(video).stem}.speech_grouping_report.json'


def save_speech_grouping_report(video: str | Path, report: dict) -> Path:
    path = report_path_for_video(video)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path
