#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Speech Activity Alignment（配音节奏优化 V2）

从原视频音轨检测说话区/停顿区，在 TTS 前约束口播窗口，
使「人物开口时有配音，长时间闭口时保持静音」。
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent.resolve()

# 默认阈值（可被 tubeget.config.json 覆盖）
DEFAULTS: dict[str, Any] = {
    'speech_activity_align': True,
    'speech_activity_short_pause': 0.8,
    'speech_activity_long_pause': 2.0,
    'speech_activity_vad_noise_db': -35,
    'speech_activity_min_silence': 0.3,
    'speech_activity_tts_ok_ratio': 1.15,
    'speech_activity_tts_fail_ratio': 1.30,
    'speech_activity_chars_per_sec': 4.0,
    'speech_activity_target_fill': 0.68,
    'speech_activity_max_tempo': 1.3,
    'speech_activity_end_buffer_sec': 0.0,
    'speech_activity_fade_out_ms': 0,
    'speech_activity_continuation_slack_sec': 1.5,
    'speech_window_soft': True,
    'speech_window_split_enabled': False,
    'vad_preserve_pause': True,
    'long_pause_sec': 2.0,
    'speech_natural_grouping': True,
    'speech_merge_gap_sec': 2.0,
    'speech_grouping_must_merge_gap_sec': 1.0,
    'speech_grouping_hard_pause_sec': 3.0,
    'speech_group_max_duration_sec': 25.0,
}


def speech_activity_config(cfg: dict | None = None) -> dict:
    """合并配置与默认值。"""
    out = dict(DEFAULTS)
    if cfg:
        for k in DEFAULTS:
            if k in cfg:
                out[k] = cfg[k]
    return out


def _run_ffmpeg(args: list[str], *, timeout: float = 600) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            args, capture_output=True, text=True,
            errors='replace', timeout=timeout,
        )
        return r.returncode, r.stdout or '', r.stderr or ''
    except subprocess.TimeoutExpired:
        return -1, '', 'ffmpeg timeout'
    except FileNotFoundError:
        return -1, '', 'ffmpeg not found'


def extract_audio_wav(video_path: str | Path, out_wav: str | Path | None = None) -> Path | None:
    """从视频提取 16kHz 单声道 WAV（供 VAD 分析）。"""
    video_path = Path(video_path)
    if not video_path.is_file():
        return None
    if out_wav is None:
        out_wav = video_path.parent / f'{video_path.stem}._vad.wav'
    out_wav = Path(out_wav)
    code, _, err = _run_ffmpeg([
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-i', str(video_path),
        '-vn', '-ac', '1', '-ar', '16000', '-f', 'wav', str(out_wav),
    ])
    if code != 0 or not out_wav.is_file() or out_wav.stat().st_size < 1000:
        return None
    return out_wav


def probe_duration(path: str | Path) -> float:
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
            capture_output=True, text=True, errors='replace', timeout=30,
        )
        return max(0.0, float(r.stdout.strip()))
    except Exception:
        return 0.0


def detect_silence_segments(
    audio_path: str | Path,
    *,
    noise_db: float = -35,
    min_silence: float = 0.3,
) -> list[dict]:
    """
    ffmpeg silencedetect → 静音段 [{start, end}, ...]。
    """
    code, _, stderr = _run_ffmpeg([
        'ffmpeg', '-hide_banner', '-i', str(audio_path),
        '-af', f'silencedetect=noise={noise_db}dB:d={min_silence}',
        '-f', 'null', '-',
    ])
    if code != 0 and 'silence_' not in stderr:
        return []

    segments: list[dict] = []
    cur_start: float | None = None
    for line in stderr.splitlines():
        m = re.search(r'silence_start:\s*([\d.]+)', line)
        if m:
            cur_start = float(m.group(1))
            continue
        m = re.search(r'silence_end:\s*([\d.]+)', line)
        if m and cur_start is not None:
            segments.append({'start': cur_start, 'end': float(m.group(1))})
            cur_start = None
    return segments


def silence_to_speech(
    silence_segments: list[dict],
    total_duration: float,
    *,
    min_speech: float = 0.15,
) -> list[dict]:
    """静音段取补集 → 说话区。"""
    if total_duration <= 0:
        return []
    sil = sorted(silence_segments, key=lambda x: x['start'])
    speech: list[dict] = []
    cursor = 0.0
    for seg in sil:
        if seg['start'] > cursor + min_speech:
            speech.append({'start': cursor, 'end': seg['start']})
        cursor = max(cursor, seg['end'])
    if total_duration > cursor + min_speech:
        speech.append({'start': cursor, 'end': total_duration})
    return speech


def classify_pauses(
    speech_segments: list[dict],
    total_duration: float,
    *,
    short_pause: float = 0.8,
    long_pause: float = 2.0,
) -> list[dict]:
    """说话区之间的停顿，标注 short / medium / long。"""
    pauses: list[dict] = []
    if not speech_segments:
        return pauses
    sorted_sp = sorted(speech_segments, key=lambda x: x['start'])
    if sorted_sp[0]['start'] > 0.05:
        pauses.append(_pause_dict(0.0, sorted_sp[0]['start'], short_pause, long_pause))
    for i in range(len(sorted_sp) - 1):
        gap_start = sorted_sp[i]['end']
        gap_end = sorted_sp[i + 1]['start']
        if gap_end > gap_start + 0.05:
            pauses.append(_pause_dict(gap_start, gap_end, short_pause, long_pause))
    if sorted_sp[-1]['end'] < total_duration - 0.05:
        pauses.append(_pause_dict(
            sorted_sp[-1]['end'], total_duration, short_pause, long_pause))
    return pauses


def _pause_dict(start: float, end: float, short: float, long: float) -> dict:
    dur = end - start
    if dur < short:
        kind = 'short'
    elif dur <= long:
        kind = 'medium'
    else:
        kind = 'long'
    return {'start': round(start, 3), 'end': round(end, 3),
            'duration': round(dur, 3), 'kind': kind}


def analyze_video_speech(
    video_path: str | Path,
    cfg: dict | None = None,
    *,
    keep_wav: bool = False,
) -> dict:
    """
    完整 VAD 分析：说话区、停顿区、统计。
    返回 {speech_segments, pause_segments, stats, wav_path?}。
    """
    cfg = speech_activity_config(cfg)
    video_path = Path(video_path)
    wav = extract_audio_wav(video_path)
    if not wav:
        return {'error': 'audio_extract_failed', 'speech_segments': [], 'pause_segments': []}

    total = probe_duration(wav)
    sil = detect_silence_segments(
        wav,
        noise_db=float(cfg['speech_activity_vad_noise_db']),
        min_silence=float(cfg['speech_activity_min_silence']),
    )
    speech = silence_to_speech(sil, total)
    pauses = classify_pauses(
        speech, total,
        short_pause=float(cfg['speech_activity_short_pause']),
        long_pause=float(cfg['speech_activity_long_pause']),
    )

    speech_dur = sum(s['end'] - s['start'] for s in speech)
    pause_dur = max(0.0, total - speech_dur)
    long_pauses = [p for p in pauses if p['kind'] == 'long']
    medium_pauses = [p for p in pauses if p['kind'] == 'medium']
    pause_durs = [p['duration'] for p in pauses if p['kind'] != 'short']

    stats = {
        'video_duration_sec': round(total, 3),
        'total_speech_sec': round(speech_dur, 3),
        'total_silence_sec': round(pause_dur, 3),
        'speech_ratio': round(speech_dur / max(total, 0.001), 4),
        'avg_pause_sec': round(sum(pause_durs) / max(len(pause_durs), 1), 3),
        'long_pause_count': len(long_pauses),
        'medium_pause_count': len(medium_pauses),
    }

    if not keep_wav:
        try:
            wav.unlink(missing_ok=True)
        except Exception:
            pass
        wav_path = None
    else:
        wav_path = str(wav)

    return {
        'speech_segments': [
            {'start': round(s['start'], 3), 'end': round(s['end'], 3)}
            for s in speech
        ],
        'pause_segments': pauses,
        'stats': stats,
        'wav_path': wav_path,
    }


def _overlapping_speech(start: float, end: float, speech: list[dict]) -> list[dict]:
    out = []
    for s in speech:
        if s['end'] <= start or s['start'] >= end:
            continue
        out.append({
            'start': max(start, s['start']),
            'end': min(end, s['end']),
        })
    return out


def _long_pause_in_range(start: float, end: float, pauses: list[dict]) -> list[dict]:
    return [
        p for p in pauses
        if p['kind'] == 'long' and p['start'] >= start and p['end'] <= end
    ]


def compute_window_params(
    duration: float,
    cfg: dict | None = None,
) -> dict:
    """计算 Gemini / 本地字数约束。"""
    cfg = speech_activity_config(cfg)
    cps = float(cfg['speech_activity_chars_per_sec'])
    fill = float(cfg['speech_activity_target_fill'])
    dur = max(0.3, duration)
    end_buf = float(cfg.get('speech_activity_end_buffer_sec', 0.25))
    speak_dur = max(0.25, dur - end_buf)
    target = max(2, int(speak_dur * cps * fill))
    max_chars = max(target + 1, int(speak_dur * cps * 1.05))
    return {
        'duration': round(dur, 3),
        'speak_duration': round(speak_dur, 3),
        'end_buffer_sec': round(end_buf, 3),
        'target_chars': target,
        'max_chars': max_chars,
    }


def apply_speech_windows_to_cues(
    cues: list,
    vad: dict,
    cfg: dict | None = None,
) -> tuple[list, dict]:
    """
    为每条 cue 写入口播窗口与字数约束（供 Gemini / TTS 后校准）。

    speech_window_soft=True（V3 默认）：
    - 保留分组后的完整时间轴，不截断语义
    - 窗口仅作参考时长与 max_chars，不要求说满
    """
    cfg = speech_activity_config(cfg)
    soft = bool(cfg.get('speech_window_soft', True))
    speech = vad.get('speech_segments') or []
    pauses = vad.get('pause_segments') or []
    long_pause = float(cfg.get('long_pause_sec', cfg['speech_activity_long_pause']))

    stats = {
        'total_cues': len(cues),
        'clipped_at_pause': 0,
        'no_speech_overlap': 0,
        'adjusted': 0,
        'soft_window': soft,
    }

    for cue in cues:
        orig_start = cue['start']
        orig_end = cue['end']

        if soft:
            duration = max(0.3, orig_end - orig_start)
            params = compute_window_params(duration, cfg)
            overlaps = _overlapping_speech(orig_start, orig_end, speech)
            if overlaps:
                vad_dur = overlaps[-1]['end'] - overlaps[0]['start']
            else:
                vad_dur = duration
                stats['no_speech_overlap'] += 1
            cue['speech_window'] = {
                'start': round(orig_start, 3),
                'end': round(orig_end, 3),
                'duration': round(duration, 3),
                'vad_span_sec': round(max(0.0, vad_dur), 3),
            }
            cue['window_params'] = params
            cue['dub_start'] = orig_start
            cue['dub_end'] = orig_end
            continue

        overlaps = _overlapping_speech(orig_start, orig_end, speech)

        if not overlaps:
            win_start, win_end = orig_start, orig_end
            stats['no_speech_overlap'] += 1
        else:
            win_start = overlaps[0]['start']
            win_end = overlaps[-1]['end']
            for p in pauses:
                if p['kind'] != 'long':
                    continue
                if p['start'] > win_start and p['end'] < orig_end:
                    if p['start'] < win_end:
                        win_end = p['start']
                        stats['clipped_at_pause'] += 1
                    break

        duration = max(0.3, win_end - win_start)
        params = compute_window_params(duration, cfg)
        cue['speech_window'] = {
            'start': round(win_start, 3),
            'end': round(win_end, 3),
            'duration': params['duration'],
        }
        cue['window_params'] = params
        cue['dub_start'] = win_start
        cue['dub_end'] = win_end
        cue['start'] = win_start
        cue['end'] = win_end
        if abs(win_end - orig_end) > 0.05 or abs(win_start - orig_start) > 0.05:
            stats['adjusted'] += 1

    return cues, stats


def merge_cues_respecting_pauses(
    cues: list,
    vad: dict,
    cfg: dict | None = None,
    *,
    max_gap: float = 0.8,
) -> tuple[list, dict]:
    """
    相邻 cue 合并时跳过长停顿（>2s）。
    短停顿（<0.8s）可忽略继续合并。
    """
    cfg = speech_activity_config(cfg)
    long_pause = float(cfg['speech_activity_long_pause'])
    short_pause = float(cfg['speech_activity_short_pause'])
    pauses = vad.get('pause_segments') or []

    if not cues:
        return [], {'merged': 0, 'before': 0, 'after': 0}

    def _gap_has_long_pause(a_end: float, b_start: float) -> bool:
        for p in pauses:
            if p['kind'] != 'long':
                continue
            if p['start'] >= a_end - 0.05 and p['end'] <= b_start + 0.05:
                return True
            if p['start'] < b_start and p['end'] > a_end:
                return True
        return (b_start - a_end) >= long_pause

    from ai_shorten import combine_bilingual_text, split_bilingual_text, zh_ends_sentence

    out: list[dict] = []
    buf = dict(cues[0])
    merged = 0

    for cur in cues[1:]:
        gap = cur['start'] - buf['end']
        should_merge = (
            gap <= max_gap
            and gap < long_pause
            and not _gap_has_long_pause(buf['end'], cur['start'])
            and not zh_ends_sentence(buf.get('text', ''))
        )
        if should_merge:
            en_a, zh_a = split_bilingual_text(buf['text'])
            en_b, zh_b = split_bilingual_text(cur['text'])
            buf = {
                **buf,
                'end': cur['end'],
                'text': combine_bilingual_text(en_a, zh_a, en_b, zh_b),
            }
            merged += 1
        else:
            out.append(buf)
            buf = dict(cur)
    out.append(buf)
    return out, {'before': len(cues), 'after': len(out), 'merged': merged}


def report_path_for_video(video: str | Path) -> Path:
    video = Path(video)
    return video.parent / f'{video.stem}.speech_activity_report.json'


def build_speech_activity_report(
    vad: dict,
    cue_stats: dict,
    *,
    dub_results: list | None = None,
    dub_cues: list | None = None,
    cfg: dict | None = None,
) -> dict:
    """生成 speech_activity_report.json 内容。"""
    cfg = speech_activity_config(cfg)
    stats = dict(vad.get('stats') or {})
    long_pauses = [
        p for p in (vad.get('pause_segments') or []) if p['kind'] == 'long'
    ]
    preserved = 0
    if dub_results and dub_cues:
        preserved = _count_long_pause_preserved(
            dub_results, dub_cues, long_pauses)

    speech_sec = float(stats.get('total_speech_sec') or 0)
    dub_sec = _total_dub_duration(dub_results)
    coverage = dub_sec / max(speech_sec, 0.001)

    report = {
        'version': 2,
        'summary': {
            'total_speech_sec': stats.get('total_speech_sec'),
            'total_silence_sec': stats.get('total_silence_sec'),
            'avg_pause_sec': stats.get('avg_pause_sec'),
            'long_pause_count': stats.get('long_pause_count'),
            'dub_coverage_ratio': round(coverage, 4),
            'long_pause_preserve_ratio': round(
                preserved / max(len(long_pauses), 1), 4),
        },
        'vad': {
            'speech_segments': vad.get('speech_segments'),
            'pause_segments': vad.get('pause_segments'),
        },
        'cue_processing': cue_stats,
        'thresholds': {
            'short_pause_sec': cfg['speech_activity_short_pause'],
            'long_pause_sec': cfg['speech_activity_long_pause'],
            'tts_ok_ratio': cfg['speech_activity_tts_ok_ratio'],
            'tts_fail_ratio': cfg['speech_activity_tts_fail_ratio'],
        },
    }
    return report


def _total_dub_duration(results: list | None) -> float:
    if not results:
        return 0.0
    from step1_subtitle_to_audio import get_audio_duration
    total = 0.0
    for _i, _s, _e, mp3 in results:
        if mp3:
            d = get_audio_duration(mp3)
            if d > 0:
                total += d
    return total


def _count_long_pause_preserved(
    results: list,
    cues: list,
    long_pauses: list[dict],
) -> int:
    """长停顿处配音未跨越 → 计为保留。"""
    from step1_subtitle_to_audio import get_audio_duration
    by_idx = {i: (i, s, e, p) for i, s, e, p in results}
    preserved = 0
    for p in long_pauses:
        mid = (p['start'] + p['end']) / 2
        crossing = False
        for i, cue in enumerate(cues):
            r = by_idx.get(i)
            if not r or not r[3]:
                continue
            ps, pe = r[1], r[2]
            dur = get_audio_duration(r[3])
            pe = ps + dur
            if ps < mid < pe:
                crossing = True
                break
        if not crossing:
            preserved += 1
    return preserved


def save_speech_activity_report(video: str | Path, report: dict) -> Path:
    path = report_path_for_video(video)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path


def fit_tts_to_speech_window(
    mp3_path: str,
    cue: dict,
    cfg: dict | None = None,
) -> dict:
    """
    TTS 后校准：加速对齐（≤1.5×，必要时 ≤2.0×），绝不截断。
    仍明显超窗（> fail_ratio）且 1.5× 不够 → 标记送 Gemini 压缩后重合成。
    """
    from step1_subtitle_to_audio import (
        fit_audio_strict_window,
        get_audio_duration,
        zh_has_continuation_mark,
    )
    from ai_shorten import split_bilingual_text

    cfg = speech_activity_config(cfg)
    ok_ratio = float(cfg['speech_activity_tts_ok_ratio'])
    fail_ratio = float(cfg['speech_activity_tts_fail_ratio'])
    max_tempo = float(cfg.get('speech_activity_max_tempo', 1.5))
    hard_tempo = float(cfg.get('speech_activity_hard_max_tempo', 2.0))
    slack_cfg = float(cfg.get('speech_activity_continuation_slack_sec', 1.5))
    soft = bool(cfg.get('speech_window_soft', True))

    _, zh = split_bilingual_text(cue.get('text', ''))
    cont = zh_has_continuation_mark(zh)

    wp = cue.get('window_params') or {}
    window = float(wp.get('duration') or (cue['end'] - cue['start']))
    window = max(0.3, window)

    actual_before = get_audio_duration(mp3_path) or 0.0
    if actual_before <= 0:
        return {
            'actual_sec': 0, 'window_sec': round(window, 3),
            'ratio': 0, 'action': 'none', 'status': 'fail',
        }

    ok_limit = window * ok_ratio
    fail_limit = window * fail_ratio
    if cont:
        ok_limit += slack_cfg
        fail_limit += slack_cfg

    end_buf = float(cfg.get('speech_activity_end_buffer_sec', 0.0))
    speak = window if cont else max(0.25, window - end_buf)
    slack = slack_cfg if cont else 0.0

    if soft:
        target_speak = speak + slack
        if actual_before > target_speak + 0.08:
            fit = fit_audio_strict_window(
                mp3_path, window,
                speak_duration=speak,
                max_tempo=max_tempo,
                hard_max_tempo=hard_tempo,
                allow_slack_sec=slack,
            )
            actual = float(fit.get('actual_sec') or actual_before)
            action = fit.get('action') or 'soft_speedup'
        else:
            actual = actual_before
            fit = {
                'action': 'soft_unchanged',
                'actual_sec': round(actual, 3),
                'window_sec': round(window, 3),
                'over_window': False,
            }
            action = 'soft_unchanged'
    else:
        fit = fit_audio_strict_window(
            mp3_path, window,
            speak_duration=speak,
            max_tempo=max_tempo,
            hard_max_tempo=hard_tempo,
            allow_slack_sec=slack,
        )
        actual = float(fit.get('actual_sec') or 0.0)
    ratio = actual / window if window > 0 else 0.0
    action = fit.get('action') or 'unchanged'

    if actual <= ok_limit + 0.06:
        status = 'ok'
    elif actual_before > fail_limit + 0.05 and actual > fail_limit + 0.05:
        status = 'fail'
        action = 'needs_regenerate'
    else:
        status = 'ok'
        if fit.get('over_window'):
            action = action or 'over_window_kept'

    if soft:
        action = fit.get('action') or 'soft_unchanged'
        if status == 'ok' and actual < window * 0.5:
            action = 'short_ok_silence'
    elif status == 'ok' and actual < window * 0.5:
        action = 'short_ok_silence'

    return {
        'actual_sec': round(actual, 3),
        'window_sec': round(window, 3),
        'ratio': round(ratio, 3),
        'ok_limit_sec': round(ok_limit, 3),
        'fail_limit_sec': round(fail_limit, 3),
        'action': action if status == 'ok' else 'needs_regenerate',
        'status': status,
        'continuation': cont,
        'over_window': bool(fit.get('over_window')),
    }


def split_cues_for_speech_window(
    cues: list,
    cfg: dict | None = None,
) -> tuple[list, dict]:
    """
    中文超出时间窗字数上限时，在自然词组边界拆成多条 cue；
    非末段句尾加 ——，供 TTS 拖音表示话未说完。
    """
    from ai_shorten import split_bilingual_text
    from step1_subtitle_to_audio import (
        mark_incomplete_zh_parts,
        _split_long_text,
    )

    cfg = speech_activity_config(cfg)
    out: list[dict] = []
    split_cues = 0

    for cue in cues:
        wp = cue.get('window_params') or {}
        max_c = int(wp.get('max_chars') or 0)
        if max_c <= 0:
            out.append(cue)
            continue
        en, zh = split_bilingual_text(cue.get('text', ''))
        if not zh or len(zh) <= max_c:
            out.append(cue)
            continue

        zh_parts = mark_incomplete_zh_parts(_split_long_text(zh, max_c))
        n = len(zh_parts)
        dur = max(0.5, cue['end'] - cue['start'])
        total_len = sum(len(p) for p in zh_parts) or 1
        t = cue['start']
        for i, zp in enumerate(zh_parts):
            end_t = cue['end'] if i == n - 1 else min(
                cue['end'], t + max(0.45, dur * len(zp) / total_len))
            sub_dur = end_t - t
            sub_wp = compute_window_params(sub_dur, cfg)
            if i == 0 and en:
                text = f'{en}\n{zp}'
            else:
                text = zp
            out.append({
                **cue,
                'start': t,
                'end': end_t,
                'text': text.strip(),
                'window_params': sub_wp,
                'speech_window': {
                    'start': round(t, 3),
                    'end': round(end_t, 3),
                    'duration': sub_wp['duration'],
                },
                'dub_start': t,
                'dub_end': end_t,
                'speech_continuation': i < n - 1,
            })
            t = end_t
        split_cues += 1

    stats = {
        'before': len(cues),
        'after': len(out),
        'split_cues': split_cues,
    }
    return out, stats


async def post_tts_speech_adjust_batch(
    cues: list,
    results: list,
    *,
    gemini_key: str = '',
    gemini_model: str = '',
    cfg: dict | None = None,
) -> tuple[list, dict]:
    """
    批量 TTS 后处理：测量、加速、超长则压缩文本并重合成标记。
    返回 (updated_results, stats)。
    """
    from ai_shorten import shorten_zh_gemini, split_bilingual_text

    cfg = speech_activity_config(cfg)
    soft = bool(cfg.get('speech_window_soft', True))
    by_i = {i: (i, s, e, p) for i, s, e, p in results}
    stats = {'ok': 0, 'speedup': 0, 'fail': 0, 'regenerate': [], 'fit_actions': {}}
    to_fit = sum(
        1 for i in range(len(cues))
        if (r := by_i.get(i)) and r[3]
    )
    if to_fit:
        if soft:
            print(
                '  [Speech Activity] 软窗口：轻度加速（≤1.3×）；'
                '超窗不调 Gemini（translate_once 策略）',
                flush=True,
            )
        else:
            print(
                f'  [进行中] Speech Activity 逐条卡时间窗（{to_fit} 条）…',
                flush=True,
            )

    done = 0
    for i, cue in enumerate(cues):
        r = by_i.get(i)
        if not r or not r[3]:
            continue
        mp3 = r[3]
        done += 1
        if to_fit and (done % 15 == 0 or done == to_fit):
            print(f'\r    窗口处理 {done}/{to_fit}', end='', flush=True)
        fit = fit_tts_to_speech_window(mp3, cue, cfg)
        stats['fit_actions'][i] = fit.get('action') or fit.get('status')
        if fit['status'] == 'ok':
            stats['ok'] += 1
            if fit.get('action') in (
                'speedup', 'speedup_extended', 'slight_speedup',
                'compress_speed', 'over_window_kept',
            ):
                stats['speedup'] += 1
            continue
        stats['fail'] += 1
        stats['regenerate'].append(i)
        _, zh = split_bilingual_text(cue.get('text', ''))
        wp = cue.get('window_params') or {}
        max_c = int(wp.get('max_chars') or 0)
        if not max_c:
            from ai_shorten import cue_zh_max, dub_prep_config
            prep = dub_prep_config(cfg if isinstance(cfg, dict) else None)
            en, _zh = split_bilingual_text(cue.get('text', ''))
            win = float(wp.get('duration') or (cue['end'] - cue['start']))
            max_c = cue_zh_max(
                en, win,
                ratio=float(prep.get('zh_en_ratio', 1.0)),
                cfg=prep,
            )
        if zh and gemini_key and max_c > 0:
            en, _ = split_bilingual_text(cue.get('text', ''))
            short, _ = await shorten_zh_gemini(
                zh, max_c, gemini_key=gemini_key,
                gemini_model=gemini_model, en_ref=en)
            if short and len(short) <= max_c + 2:
                cue['text'] = f'{en}\n{short}' if en else short
                cue['zh_dub'] = short
                cue['_needs_resynth'] = True

    if to_fit:
        print(flush=True)

    new_results = [by_i.get(i, (i, cues[i]['start'], cues[i]['end'], None))
                   for i in range(len(cues))]
    return new_results, stats
