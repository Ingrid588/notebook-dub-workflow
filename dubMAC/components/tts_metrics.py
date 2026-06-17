#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Edge TTS 性能指标收集、聚合与报告（不介入合成逻辑）。"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def normalize_failure_reason(raw: str) -> str:
    """将原始错误归类为统计桶。"""
    s = (raw or '').strip().lower()
    if not s or s == 'empty_text':
        return 'empty_text'
    if 'timeout' in s:
        return 'timeout'
    if any(k in s for k in (
        'cannot connect', 'connection', 'connect',
        'network', 'ssl', '指定的网络名',
    )):
        return 'connection_error'
    if any(k in s for k in (
        'no audio', 'invalid_output', 'empty_audio', 'empty audio',
    )):
        return 'empty_audio'
    return 'unknown'


def percentile(sorted_vals: list[float], p: float) -> float:
    """线性插值百分位数，p 为 0～100。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def duration_bucket(sec: float) -> str:
    if sec < 5:
        return '<5s'
    if sec < 10:
        return '5-10s'
    if sec < 20:
        return '10-20s'
    if sec < 30:
        return '20-30s'
    return '>30s'


@dataclass
class UnitRecord:
    unit_id: int
    text_length: int
    voice: str
    start_time: str
    end_time: str
    duration_sec: float
    retry_count: int
    status: str  # success | cached | retry | failed | fallback
    failure_reason: str | None = None


@dataclass
class TtsMetricsCollector:
    voice: str = ''
    run_started_at: str = field(default_factory=_utc_now_iso)
    run_ended_at: str = ''
    units: list[UnitRecord] = field(default_factory=list)
    total_retries: int = 0

    def begin_unit(
        self,
        unit_id: int,
        text: str,
        *,
        voice: str = '',
    ) -> str:
        return _utc_now_iso()

    def finish_unit(
        self,
        unit_id: int,
        text: str,
        *,
        voice: str,
        start_time: str,
        duration_sec: float,
        retry_count: int,
        status: str,
        failure_reason: str | None = None,
    ) -> None:
        self.units.append(UnitRecord(
            unit_id=unit_id,
            text_length=len((text or '').strip()),
            voice=voice,
            start_time=start_time,
            end_time=_utc_now_iso(),
            duration_sec=round(max(0.0, duration_sec), 3),
            retry_count=max(0, retry_count),
            status=status,
            failure_reason=normalize_failure_reason(failure_reason)
            if failure_reason else None,
        ))
        if status in ('success', 'failed', 'fallback'):
            self.total_retries += retry_count

    def record_retry_attempt(self) -> None:
        """队列重试入队（最终计入 total_retries 以 finish_unit 为准）。"""
        pass

    def build_summary(self) -> dict[str, Any]:
        total = len(self.units)
        cached_n = sum(1 for u in self.units if u.status == 'cached')
        success_n = sum(1 for u in self.units if u.status == 'success')
        failed_n = sum(1 for u in self.units if u.status == 'failed')
        fallback_n = sum(1 for u in self.units if u.status == 'fallback')
        generated_n = success_n  # 本次新生成（非缓存）
        gen_durations = [
            u.duration_sec for u in self.units
            if u.status == 'success' and u.duration_sec > 0
        ]
        all_durations = [
            u.duration_sec for u in self.units
            if u.status in ('success', 'cached') and u.duration_sec >= 0
        ]
        # 分布仅统计实际合成耗时（success，不含 cached 0）
        dist_source = gen_durations or all_durations
        sorted_d = sorted(dist_source)

        dist: dict[str, int] = {
            '<5s': 0, '5-10s': 0, '10-20s': 0, '20-30s': 0, '>30s': 0,
        }
        for d in dist_source:
            dist[duration_bucket(d)] += 1

        fail_reasons: dict[str, int] = {}
        for u in self.units:
            if u.status != 'failed' or not u.failure_reason:
                continue
            key = u.failure_reason
            fail_reasons[key] = fail_reasons.get(key, 0) + 1

        cache_hit = (cached_n / total * 100.0) if total else 0.0
        total_wall = sum(gen_durations)

        return {
            'run_started_at': self.run_started_at,
            'run_ended_at': self.run_ended_at or _utc_now_iso(),
            'voice': self.voice,
            'total_units': total,
            'success': success_n + cached_n,
            'failed': failed_n,
            'fallback': fallback_n,
            'generated': generated_n,
            'cached': cached_n,
            'cache_hit_rate': round(cache_hit, 1),
            'total_retries': self.total_retries,
            'total_duration_sec': round(total_wall, 2),
            'avg_duration_sec': round(statistics.mean(gen_durations), 2)
            if gen_durations else 0.0,
            'p50': round(percentile(sorted_d, 50), 2) if sorted_d else 0.0,
            'p90': round(percentile(sorted_d, 90), 2) if sorted_d else 0.0,
            'p95': round(percentile(sorted_d, 95), 2) if sorted_d else 0.0,
            'p99': round(percentile(sorted_d, 99), 2) if sorted_d else 0.0,
            'max_duration_sec': round(max(sorted_d), 2) if sorted_d else 0.0,
            'min_duration_sec': round(min(sorted_d), 2) if sorted_d else 0.0,
            'distribution': dist,
            'failure_reasons': fail_reasons,
            'units': [asdict(u) for u in self.units],
        }

    def write_json(self, path: str | Path) -> Path:
        self.run_ended_at = _utc_now_iso()
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = self.build_summary()
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return out

    def print_report(self) -> None:
        s = self.build_summary()
        lines = [
            '================================================',
            'TTS PERFORMANCE REPORT',
            '================================================',
            '',
            f'Total Units: {s["total_units"]}',
            '',
            f'Generated: {s["generated"]}',
            f'Cached: {s["cached"]}',
            '',
            f'Cache Hit Rate: {s["cache_hit_rate"]}%',
            '',
            f'Success: {s["success"]}',
            f'Failed: {s["failed"]}',
            '',
            f'Average: {s["avg_duration_sec"]}s',
            f'P50: {s["p50"]}s',
            f'P90: {s["p90"]}s',
            f'P95: {s["p95"]}s',
            f'P99: {s["p99"]}s',
            '',
            f'Max: {s["max_duration_sec"]}s',
            f'Min: {s["min_duration_sec"]}s',
            '',
            'Distribution:',
            '',
        ]
        for bucket in ('<5s', '5-10s', '10-20s', '20-30s', '>30s'):
            lines.append(f'{bucket:<8} {s["distribution"].get(bucket, 0)}')
        lines.extend(['', 'Failure Reasons:', ''])
        reasons = s.get('failure_reasons') or {}
        if reasons:
            for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
                lines.append(f'{k}: {v}')
        else:
            lines.append('(none)')
        lines.append('================================================')
        print('\n'.join(lines))


def tts_metrics_path_from_subtitle(subtitle_path: str | Path) -> Path:
    """与 dub-status 同目录：{video_base}.tts_metrics.json"""
    from step1_subtitle_to_audio import video_base_from_zh_subtitle
    parent = Path(subtitle_path).parent
    base = video_base_from_zh_subtitle(str(subtitle_path))
    return parent / f'{base}.tts_metrics.json'
