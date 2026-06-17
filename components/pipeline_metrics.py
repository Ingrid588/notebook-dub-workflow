#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配音管线阶段耗时与 TTS 统计（pipeline_metrics.json）。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class PipelineTimer:
    """记录各阶段 wall-clock 耗时。"""

    def __init__(self) -> None:
        self._starts: dict[str, float] = {}
        self.stages: dict[str, float] = {}
        self._total_start: float | None = None

    def start_total(self) -> None:
        self._total_start = time.perf_counter()

    def end_total(self) -> float:
        if self._total_start is None:
            return 0.0
        sec = time.perf_counter() - self._total_start
        self.stages['total_sec'] = round(sec, 3)
        return sec

    def __enter__(self) -> 'StageContext':
        return StageContext(self)

    def begin(self, name: str) -> None:
        self._starts[name] = time.perf_counter()

    def end(self, name: str) -> float:
        t0 = self._starts.pop(name, None)
        if t0 is None:
            return 0.0
        sec = time.perf_counter() - t0
        self.stages[name] = round(sec, 3)
        return sec


class StageContext:
    def __init__(self, timer: PipelineTimer) -> None:
        self._timer = timer
        self._name = ''

    def stage(self, name: str) -> 'StageContext':
        self._name = name
        self._timer.begin(name)
        return self

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_) -> None:
        if self._name:
            self._timer.end(self._name)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(round((len(s) - 1) * p))
    return round(s[max(0, min(idx, len(s) - 1))], 3)


def summarize_tts_times(times: list[float]) -> dict[str, Any]:
    if not times:
        return {
            'count': 0, 'avg_sec': 0.0,
            'p50_sec': 0.0, 'p95_sec': 0.0, 'p99_sec': 0.0,
            'max_sec': 0.0,
        }
    return {
        'count': len(times),
        'avg_sec': round(sum(times) / len(times), 3),
        'p50_sec': _percentile(times, 0.50),
        'p95_sec': _percentile(times, 0.95),
        'p99_sec': _percentile(times, 0.99),
        'max_sec': round(max(times), 3),
    }


def build_pipeline_metrics(
    timer: PipelineTimer,
    *,
    tts_total: int = 0,
    tts_ok: int = 0,
    tts_fail: int = 0,
    tts_times: list[float] | None = None,
    extra: dict | None = None,
) -> dict:
    """组装 pipeline_metrics.json 结构。"""
    tts_times = tts_times or []
    rate = tts_ok / max(tts_total, 1)
    stage_labels = {
        'subtitle_prepare_sec': 'Subtitle Prepare',
        'gemini_sec': 'Gemini Translation',
        'edge_tts_sec': 'Edge TTS',
        'audio_merge_sec': 'Audio Merge',
        'video_merge_sec': 'Video Merge',
        'total_sec': 'Total',
    }
    stages_out = {}
    for key, label in stage_labels.items():
        if key in timer.stages:
            stages_out[label] = timer.stages[key]

    return {
        'version': 1,
        'stages_sec': stages_out,
        'tts': {
            'total': tts_total,
            'success': tts_ok,
            'fail': tts_fail,
            'success_rate': round(rate, 4),
            **summarize_tts_times(tts_times),
        },
        'extra': extra or {},
    }


def metrics_path_for_video(video: str | Path) -> Path:
    return Path(video).parent / f'{Path(video).stem}.pipeline_metrics.json'


def save_pipeline_metrics(video: str | Path, metrics: dict) -> Path:
    path = metrics_path_for_video(video)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)
    return path
