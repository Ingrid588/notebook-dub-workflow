#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Edge TTS 分段批量异步合成
========================
将字幕按字数切成 1～2 段（约 280～550 字）一次发送，asyncio 并发 + Semaphore。
支持 profile：small / medium / large / auto（先小段试跑，成功后再加大段幅）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from edge_tts_scheduler import (
    EdgeRunStats,
    EdgeSchedulerConfig,
    cue_mp3_path,
    edge_save_once,
    format_edge_connect_error,
    load_edge_scheduler_config,
    mp3_is_valid,
    probe_edge_tts,
    edge_connectivity_probe_enabled,
)

HERE = Path(__file__).parent.resolve()
CONFIG_PATH = HERE / 'tubeget.config.json'
_SENTINEL = object()
SENTENCE_END_RE = re.compile(r'[。！？.!?；…]$')


@dataclass
class EdgeSegmentProfile:
    name: str
    max_chars: int
    min_chars: int
    concurrency: int
    launch_interval_sec: float
    timeout_sec: float


def _load_cfg_raw() -> dict:
    for p in (Path.cwd() / 'tubeget.config.json', CONFIG_PATH):
        if p.exists():
            try:
                with open(p, encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
    return {}


def edge_segment_mode_enabled() -> bool:
    if os.environ.get('TUBEGET_QUICK_BILINGUAL', '').strip().lower() in (
            '1', 'true', 'yes'):
        return False
    return bool(_load_cfg_raw().get('edge_segment_mode', True))


def load_segment_profiles() -> dict[str, EdgeSegmentProfile]:
    d = _load_cfg_raw()
    return {
        'small': EdgeSegmentProfile(
            'small',
            int(d.get('edge_segment_chars_small', 280)),
            int(d.get('edge_segment_chars_min', 120)),
            int(d.get('edge_segment_concurrency_small', 3)),
            float(d.get('edge_segment_launch_interval_sec', 1.5)),
            float(d.get('edge_segment_timeout_small', 60)),
        ),
        'medium': EdgeSegmentProfile(
            'medium',
            int(d.get('edge_segment_chars_medium', 420)),
            int(d.get('edge_segment_chars_min', 120)),
            int(d.get('edge_segment_concurrency', 4)),
            float(d.get('edge_segment_launch_interval_sec', 1.5)),
            float(d.get('edge_segment_timeout_sec', 75)),
        ),
        'large': EdgeSegmentProfile(
            'large',
            int(d.get('edge_segment_chars_large', 550)),
            int(d.get('edge_segment_chars_min', 120)),
            int(d.get('edge_segment_concurrency_large', 5)),
            float(d.get('edge_segment_launch_interval_sec', 1.0)),
            float(d.get('edge_segment_timeout_large', 90)),
        ),
    }


def resolve_segment_profile(name: str) -> EdgeSegmentProfile:
    profiles = load_segment_profiles()
    return profiles.get(name, profiles['medium'])


def segment_mp3_path(work_dir: str, seg_id: int) -> str:
    return os.path.join(work_dir, f'seg_{seg_id:04d}.mp3')


def build_segments_from_cues(
    cues: list[dict],
    *,
    max_chars: int,
    min_chars: int = 120,
    text_extractor: Callable[[dict], str],
    only_indices: set[int] | None = None,
    skip_indices: set[int] | frozenset[int] | None = None,
) -> list[dict]:
    """
    按字数切分段落（尽量在句末标点处断开）。
    返回 [{id, cue_indices, text, start, end, chars}, ...]
    """
    skip = skip_indices or frozenset()
    segments: list[dict] = []
    buf_idx: list[int] = []
    buf_parts: list[str] = []
    buf_chars = 0

    def _flush() -> None:
        nonlocal buf_chars
        if not buf_idx:
            return
        text = ''.join(buf_parts)
        segments.append({
            'id': len(segments),
            'cue_indices': list(buf_idx),
            'text': text,
            'start': cues[buf_idx[0]]['start'],
            'end': cues[buf_idx[-1]]['end'],
            'chars': buf_chars,
        })
        buf_idx.clear()
        buf_parts.clear()
        buf_chars = 0

    for i, cue in enumerate(cues):
        if i in skip:
            continue
        if only_indices is not None and i not in only_indices:
            continue
        t = text_extractor(cue).strip()
        if not t:
            continue
        tlen = len(t)

        if tlen > max_chars:
            _flush()
            segments.append({
                'id': len(segments),
                'cue_indices': [i],
                'text': t,
                'start': cue['start'],
                'end': cue['end'],
                'chars': tlen,
            })
            continue

        if buf_chars > 0 and buf_chars + tlen > max_chars:
            _flush()

        buf_idx.append(i)
        buf_parts.append(t)
        buf_chars += tlen

        last = buf_parts[-1] if buf_parts else ''
        if buf_chars >= min_chars and SENTENCE_END_RE.search(last):
            _flush()
        elif buf_chars >= max_chars:
            _flush()

    _flush()
    return segments


async def run_edge_direct_segments(
    cues: list[dict],
    voice_id: str,
    rate: str,
    work_dir: str,
    *,
    max_chars: int = 1200,
    text_extractor: Callable[[dict], str] | None = None,
    on_success: Callable[[int, str, dict], None] | None = None,
) -> tuple[list[tuple[int, float, float, str]], list[int], dict]:
    """
    ≤max_chars 字整段送 Edge TTS，合成后不再切回单句（预览直配专用）。
    避免 split_segment_mp3_to_cues 把「了解」等词从中间切开。
    """
    extract = text_extractor or (lambda c: c.get('text', ''))
    segments = build_segments_from_cues(
        cues,
        max_chars=max_chars,
        min_chars=max_chars + 1,
        text_extractor=extract,
    )
    os.makedirs(work_dir, exist_ok=True)
    stats = EdgeRunStats()
    results: list[tuple[int, float, float, str]] = []
    failed: list[int] = []

    if not segments:
        return [], [], stats.summary()

    chars = [s['chars'] for s in segments]
    cues_n = sum(len(s['cue_indices']) for s in segments)
    print(
        f'  Edge 直送：{len(segments)} 段 / {cues_n} 条字幕，'
        f'每段 ≤{max_chars} 字，整段合成不切回单句',
        flush=True,
    )
    print(
        f'  段长：min={min(chars)} max={max(chars)} '
        f'avg={sum(chars) // len(chars)} 字',
        flush=True,
    )

    for seg in segments:
        mp3 = segment_mp3_path(work_dir, seg['id'])
        if mp3_is_valid(mp3):
            cue_stub = {'start': seg['start'], 'end': seg['end']}
            if on_success:
                on_success(seg['id'], mp3, cue_stub)
            results.append((seg['id'], seg['start'], seg['end'], mp3))
            stats.record_success(0.0, cached=True)
            continue

        timeout = 45.0 + min(120.0, seg['chars'] * 0.06)
        ok, err, elapsed = await edge_save_once(
            seg['text'], voice_id, rate, mp3, timeout,
        )
        if ok and mp3_is_valid(mp3):
            cue_stub = {'start': seg['start'], 'end': seg['end']}
            if on_success:
                on_success(seg['id'], mp3, cue_stub)
            results.append((seg['id'], seg['start'], seg['end'], mp3))
            stats.record_success(elapsed)
            preview = seg['text'][:28].replace('\n', '')
            print(f'    段 {seg["id"] + 1}/{len(segments)} OK '
                  f'({seg["chars"]}字) {preview}…', flush=True)
        else:
            failed.append(seg['id'])
            stats.record_failure(err or 'edge_failed')
            print(f'    段 {seg["id"] + 1}/{len(segments)} 失败：{err}', flush=True)

    return results, failed, stats.summary()


def split_segment_mp3_to_cues(
    segment_mp3: str,
    cue_indices: list[int],
    cues: list[dict],
    work_dir: str,
    text_extractor: Callable[[dict], str],
) -> dict[int, str]:
    """把段音频切成各句 cue_XXXX.mp3（优先按时间轴比例，避免按字数硬切）。"""
    from pydub import AudioSegment

    seg = AudioSegment.from_mp3(segment_mp3)
    seg = seg.set_frame_rate(44100).set_channels(1)
    total_ms = len(seg)
    if len(cue_indices) <= 1:
        idx = cue_indices[0]
        path = cue_mp3_path(work_dir, idx)
        seg.export(path, format='mp3', bitrate='128k')
        return {idx: path}

    spans = [
        max(0.35, float(cues[i]['end']) - float(cues[i]['start']))
        for i in cue_indices
    ]
    span_start = float(cues[cue_indices[0]]['start'])
    span_end = float(cues[cue_indices[-1]]['end'])
    timeline_gap = span_end - span_start
    use_time = (
        timeline_gap > 0.6
        and max(float(cues[i]['start']) for i in cue_indices)
        - min(float(cues[i]['start']) for i in cue_indices) > 0.25
    )
    if use_time:
        weights = spans
    else:
        weights = [
            max(1, len(text_extractor(cues[i]).strip()))
            for i in cue_indices
        ]
    total_w = sum(weights) or 1
    out: dict[int, str] = {}
    pos = 0
    for j, idx in enumerate(cue_indices):
        if j == len(cue_indices) - 1:
            chunk = seg[pos:]
        else:
            ms = max(80, int(total_ms * weights[j] / total_w))
            chunk = seg[pos:pos + ms]
            pos += ms
        path = cue_mp3_path(work_dir, idx)
        chunk.export(path, format='mp3', bitrate='128k')
        out[idx] = path
    return out


def print_segment_plan(
    segments: list[dict], profile: EdgeSegmentProfile,
) -> None:
    if not segments:
        return
    chars = [s['chars'] for s in segments]
    cues_n = sum(len(s['cue_indices']) for s in segments)
    print(
        f'  Edge 分段：{len(segments)} 段 / {cues_n} 句，'
        f'profile={profile.name}，每段 ≤{profile.max_chars} 字，'
        f'并发 {profile.concurrency}',
        flush=True,
    )
    print(
        f'  段长：min={min(chars)} max={max(chars)} avg={sum(chars)//len(chars)} 字',
        flush=True,
    )


async def _synth_one_segment(
    seg: dict,
    profile: EdgeSegmentProfile,
    voice_id: str,
    rate: str,
    work_dir: str,
    slot_sem: asyncio.Semaphore,
    launch_lock: asyncio.Lock,
    last_launch: list[float],
) -> tuple[bool, str, float, str]:
    """合成单段，返回 (ok, err, elapsed, mp3_path)。"""
    mp3 = segment_mp3_path(work_dir, seg['id'])
    timeout = profile.timeout_sec + min(30.0, seg['chars'] * 0.04)
    async with slot_sem:
        async with launch_lock:
            gap = profile.launch_interval_sec - (time.monotonic() - last_launch[0])
            if gap > 0:
                await asyncio.sleep(gap)
            last_launch[0] = time.monotonic()
        return await _edge_save_segment(
            seg['text'], voice_id, rate, mp3, timeout,
        )


async def _edge_save_segment(
    text: str, voice_id: str, rate: str, out_path: str, timeout_sec: float,
) -> tuple[bool, str, float, str]:
    ok, err, elapsed = await edge_save_once(
        text, voice_id, rate, out_path, timeout_sec,
    )
    return ok, err, elapsed, out_path


async def run_edge_segment_scheduler(
    cues: list[dict],
    voice_id: str,
    rate: str,
    work_dir: str,
    *,
    only_indices: set[int] | None = None,
    subtitle_path: str | Path | None = None,
    status_meta: dict | None = None,
    text_extractor: Callable[[dict], str] | None = None,
    on_success: Callable[[int, str, dict], None] | None = None,
    skip_indices: frozenset[int] | set[int] | None = None,
    rate_for_cue: Callable[[dict], str] | None = None,
) -> tuple[list[tuple[int, float, float, str]], list[int], dict]:
    """
    分段 + 批量异步 Edge TTS。失败段可降级为逐句重试。
    返回格式与 run_edge_rolling_scheduler 相同。
    """
    _ = subtitle_path, status_meta  # 状态写入沿用 rolling 时可扩展
    from video_pacing import rate_for_segment
    extract = text_extractor or (lambda c: c.get('text', ''))
    skip = set(skip_indices or ())
    raw = _load_cfg_raw()
    profile_name = str(raw.get('edge_segment_profile', 'auto'))
    warmup_n = max(1, int(raw.get('edge_segment_warmup_count', 3)))
    fallback_per_cue = bool(raw.get('edge_segment_fallback_per_cue', True))
    profiles = load_segment_profiles()

    os.makedirs(work_dir, exist_ok=True)
    stats = EdgeRunStats()
    results: dict[int, tuple[int, float, float, str]] = {}
    failed: set[int] = set()

    # 缓存命中
    for i, cue in enumerate(cues):
        if i in skip:
            continue
        if only_indices is not None and i not in only_indices:
            mp3 = cue_mp3_path(work_dir, i)
            if mp3_is_valid(mp3):
                results[i] = (i, cue['start'], cue['end'], mp3)
                stats.record_success(0.0, cached=True)
            continue
        mp3 = cue_mp3_path(work_dir, i)
        if mp3_is_valid(mp3):
            results[i] = (i, cue['start'], cue['end'], mp3)
            stats.record_success(0.0, cached=True)

    need = [
        i for i in range(len(cues))
        if i not in skip and i not in results
        and (only_indices is None or i in only_indices)
    ]
    if not need:
        print('  Edge 分段：全部已缓存', flush=True)
        return [results[i] for i in sorted(results)], [], stats.summary()

    if edge_connectivity_probe_enabled():
        print('  正在探测 Edge 连通性…', flush=True)
        ok_probe, probe_err = await probe_edge_tts(voice_id, rate, 12.0)
        if not ok_probe:
            print(f'  [Edge 探测失败] {probe_err}', flush=True)
            for i in need:
                failed.add(i)
            summary = stats.summary()
            summary['probe_failed'] = True
            return [results[i] for i in sorted(results)], sorted(failed), summary
        print('  Edge 连通正常', flush=True)

    async def _run_batch(
        indices: list[int], profile: EdgeSegmentProfile, label: str,
    ) -> tuple[set[int], set[int]]:
        """返回 (成功句索引, 失败句索引)。"""
        segments = build_segments_from_cues(
            cues,
            max_chars=profile.max_chars,
            min_chars=profile.min_chars,
            text_extractor=extract,
            only_indices=set(indices),
            skip_indices=skip,
        )
        if not segments:
            return set(), set(indices)

        print_segment_plan(segments, profile)
        if label:
            print(f'  {label}', flush=True)

        slot_sem = asyncio.Semaphore(profile.concurrency)
        launch_lock = asyncio.Lock()
        last_launch = [0.0]
        ok_cues: set[int] = set()
        fail_cues: set[int] = set()
        done_seg = 0
        total_seg = len(segments)

        async def _process_segment(seg: dict) -> None:
            nonlocal done_seg
            preview = seg['text'][:36].replace('\n', '')
            ok, err, elapsed, seg_mp3 = False, '', 0.0, ''
            seg_rate = rate_for_segment(cues, seg['cue_indices'], rate)
            for attempt in range(2):
                if attempt > 0:
                    await asyncio.sleep(3.0 * attempt)
                ok, err, elapsed, seg_mp3 = await _synth_one_segment(
                    seg, profile, voice_id, seg_rate, work_dir,
                    slot_sem, launch_lock, last_launch,
                )
                if ok:
                    break
            done_seg += 1
            pct = done_seg * 100 // max(total_seg, 1)
            if ok:
                try:
                    split = await asyncio.get_event_loop().run_in_executor(
                        None,
                        split_segment_mp3_to_cues,
                        seg_mp3, seg['cue_indices'], cues, work_dir, extract,
                    )
                except Exception as e:
                    ok = False
                    err = f'split_failed:{e}'
                    split = {}

            if ok:
                stats.record_success(elapsed)
                for idx, mp3_path in split.items():
                    cue = cues[idx]
                    results[idx] = (idx, cue['start'], cue['end'], mp3_path)
                    ok_cues.add(idx)
                    if on_success:
                        await asyncio.get_event_loop().run_in_executor(
                            None, on_success, idx, mp3_path, cue,
                        )
                print(
                    f'  段 {seg["id"] + 1}/{total_seg} OK '
                    f'({seg["chars"]}字→{len(seg["cue_indices"])}句) '
                    f'[{pct}%] {preview}…',
                    flush=True,
                )
            else:
                stats.record_failure(err or 'segment_failed')
                for idx in seg['cue_indices']:
                    fail_cues.add(idx)
                print(
                    f'  段 {seg["id"] + 1}/{total_seg} 失败：{err or "?"} '
                    f'({seg["chars"]}字) [{pct}%]',
                    flush=True,
                )

        await asyncio.gather(*[_process_segment(s) for s in segments])
        return ok_cues, fail_cues

    remaining = list(need)
    active_profile = (
        profiles.get(profile_name)
        or profiles['medium']
    )

    if profile_name == 'auto':
        small = profiles['small']
        segs_all = build_segments_from_cues(
            cues, max_chars=small.max_chars, min_chars=small.min_chars,
            text_extractor=extract, only_indices=set(remaining), skip_indices=skip,
        )
        warmup_segs = segs_all[:warmup_n]
        warmup_indices: set[int] = set()
        for s in warmup_segs:
            warmup_indices.update(s['cue_indices'])
        rest_indices = [i for i in remaining if i not in warmup_indices]

        print(
            f'  Edge auto：先 {len(warmup_segs)} 小段试跑（≤{small.max_chars}字），'
            f'再视成功率加大段幅',
            flush=True,
        )
        ok_w, fail_w = await _run_batch(
            sorted(warmup_indices), small,
            f'试跑阶段（small，{len(warmup_segs)} 段）',
        )
        warmup_fail = len(fail_w) / max(len(warmup_indices), 1)
        if warmup_fail == 0 and rest_indices:
            active_profile = profiles['medium']
            print(
                f'  试跑 100% 成功 → 升级到 medium（≤{active_profile.max_chars}字，'
                f'并发 {active_profile.concurrency}）',
                flush=True,
            )
            ok_r, fail_r = await _run_batch(
                rest_indices, active_profile, '正式阶段（medium）',
            )
            failed.update(fail_r)
        elif warmup_fail == 0 and not rest_indices:
            pass
        else:
            print(
                f'  试跑成功率 {(1-warmup_fail)*100:.0f}% → 剩余句继续用 small',
                flush=True,
            )
            if rest_indices:
                ok_r, fail_r = await _run_batch(
                    rest_indices, small, '续跑阶段（small）',
                )
                failed.update(fail_r)
            failed.update(fail_w)
    else:
        ok_all, fail_all = await _run_batch(
            remaining, active_profile,
            f'Edge 分段合成（{active_profile.name}）',
        )
        failed.update(fail_all)

    # 失败段降级：逐句 Edge
    if fallback_per_cue and failed:
        still = sorted(failed)
        print(f'  分段失败 {len(still)} 句 → 逐句降级重试…', flush=True)
        base = load_edge_scheduler_config()
        fb_sem = asyncio.Semaphore(max(2, base.concurrency))
        recovered: set[int] = set()

        async def _fallback_one(idx: int) -> None:
            cue = cues[idx]
            text = extract(cue).strip()
            mp3 = cue_mp3_path(work_dir, idx)
            if not text:
                return
            async with fb_sem:
                ok, err, elapsed = await edge_save_once(
                    text, voice_id, rate, mp3, base.timeout_sec,
                )
            if ok:
                results[idx] = (idx, cue['start'], cue['end'], mp3)
                recovered.add(idx)
                stats.record_success(elapsed)
                if on_success:
                    await asyncio.get_event_loop().run_in_executor(
                        None, on_success, idx, mp3, cue,
                    )
            else:
                stats.record_failure(err or 'fallback_failed')

        await asyncio.gather(*[_fallback_one(i) for i in still])
        failed -= recovered

    summary = stats.summary()
    summary['segment_mode'] = True
    summary['profile'] = profile_name
    summary['segments_used'] = active_profile.name
    print(
        f'  Edge 分段完成：成功 {len(results)} 句，失败 {len(failed)} 句',
        flush=True,
    )
    if results:
        print(
            '  [准备] 汇总分段结果，写入各句 MP3 并统计成功率…',
            flush=True,
        )
    return [results[i] for i in sorted(results)], sorted(failed), summary


def print_edge_segment_config() -> None:
    raw = _load_cfg_raw()
    profiles = load_segment_profiles()
    p = raw.get('edge_segment_profile', 'auto')
    print(
        f'  Edge 分段模式：profile={p}  '
        f'small≤{profiles["small"].max_chars}  '
        f'medium≤{profiles["medium"].max_chars}  '
        f'large≤{profiles["large"].max_chars}  '
        f'并发={profiles["medium"].concurrency}',
        flush=True,
    )
