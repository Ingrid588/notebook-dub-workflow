#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量配音/合并完成统计报告。"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field


def probe_media_duration(path: str | None) -> float:
    if not path:
        return 0.0
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
        )
        return max(0.0, float(r.stdout.strip()))
    except (ValueError, OSError):
        return 0.0


def format_content_duration(seconds: float) -> str:
    """成片/音轨内容时长。"""
    if seconds <= 0:
        return '—'
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f'{hours}小时')
    if minutes or hours:
        parts.append(f'{minutes}分')
    parts.append(f'{secs}秒')
    return ''.join(parts)


def format_work_duration(seconds: float) -> str:
    """实际花费的墙钟时间。"""
    if seconds <= 0:
        return '—'
    if seconds < 90:
        return f'{int(round(seconds))}秒'
    total_min = int(round(seconds / 60))
    if total_min < 60:
        return f'{total_min}分'
    hours, minutes = divmod(total_min, 60)
    if minutes:
        return f'{hours}小时{minutes}分'
    return f'{hours}小时'


def format_ratio_hours_per_hour(ratio: float) -> str:
    """平均每小时成片需要多少小时制作。"""
    if ratio <= 0:
        return '—'
    wall_min = int(round(ratio * 60))
    if wall_min < 60:
        return f'{wall_min}分'
    hours, minutes = divmod(wall_min, 60)
    if minutes:
        return f'{hours}小时{minutes}分'
    return f'{hours}小时'


@dataclass
class VideoJobStats:
    key: str
    display_name: str
    video_sec: float = 0.0
    dub_mp3_sec: float = 0.0
    dub_wall_sec: float = 0.0
    merge_wall_sec: float = 0.0
    subtitle_prepare_sec: float = 0.0
    tts_sec: float = 0.0
    audio_merge_sec: float = 0.0
    dub_skipped: bool = False
    merge_skipped: bool = False
    status: str = '—'  # ok / 跳过 / 失败 / 挂起

    @property
    def total_wall_sec(self) -> float:
        return self.dub_wall_sec + self.merge_wall_sec


@dataclass
class BatchReport:
    jobs: dict[str, VideoJobStats] = field(default_factory=dict)
    batch_wall_sec: float = 0.0
    prep_wall_sec: float = 0.0

    def job(self, key: str, display_name: str | None = None) -> VideoJobStats:
        k = key.strip()
        if k not in self.jobs:
            self.jobs[k] = VideoJobStats(key=k, display_name=display_name or k)
        elif display_name and len(display_name) < len(self.jobs[k].display_name):
            self.jobs[k].display_name = display_name
        return self.jobs[k]

    def totals(self) -> dict[str, float]:
        ok_jobs = [j for j in self.jobs.values() if j.status == 'ok']
        return {
            'video_sec': sum(j.video_sec for j in ok_jobs),
            'dub_mp3_sec': sum(j.dub_mp3_sec for j in ok_jobs),
            'dub_wall_sec': sum(j.dub_wall_sec for j in self.jobs.values()),
            'merge_wall_sec': sum(j.merge_wall_sec for j in self.jobs.values()),
            'work_wall_sec': sum(j.total_wall_sec for j in self.jobs.values()),
            'ok_count': len(ok_jobs),
        }

    def pipeline_totals(self) -> dict[str, float]:
        jobs = self.jobs.values()
        return {
            'subtitle_prepare': (
                self.prep_wall_sec
                + sum(j.subtitle_prepare_sec for j in jobs)
            ),
            'tts': sum(j.tts_sec for j in jobs),
            'audio_merge': sum(j.audio_merge_sec for j in jobs),
            'video_merge': sum(j.merge_wall_sec for j in jobs),
            'total': self.batch_wall_sec,
        }


def apply_pipeline_perf_from_status(job: VideoJobStats, status: dict | None) -> None:
    """从 dub-status.json 的 pipeline_perf 填入作业统计。"""
    if not status:
        return
    perf = status.get('pipeline_perf') or {}
    job.subtitle_prepare_sec = float(perf.get('subtitle_prepare_sec') or 0)
    job.tts_sec = float(perf.get('tts_sec') or 0)
    job.audio_merge_sec = float(perf.get('audio_merge_sec') or 0)


def print_pipeline_perf_report(report: BatchReport) -> None:
    """各阶段墙钟耗时（Translation 预留，暂无数据）。"""
    p = report.pipeline_totals()
    print('\nPIPELINE PERFORMANCE REPORT\n')
    print(f'Subtitle prepare: {format_work_duration(p["subtitle_prepare"])}')
    print('Translation:')
    print(f'TTS generation: {format_work_duration(p["tts"])}')
    print(f'Audio merge: {format_work_duration(p["audio_merge"])}')
    print(f'Video merge: {format_work_duration(p["video_merge"])}')
    print(f'Total: {format_work_duration(p["total"])}')


def print_batch_report(report: BatchReport) -> None:
    totals = report.totals()
    ok_jobs = [j for j in report.jobs.values() if j.status == 'ok']
    other_sec = max(
        0.0,
        report.batch_wall_sec - report.prep_wall_sec
        - totals['dub_wall_sec'] - totals['merge_wall_sec'],
    )

    print('\n' + '=' * 50)
    print('【完成统计】')
    if not report.jobs:
        print('  （无处理记录）')
        print('=' * 50)
        return

    rows = sorted(report.jobs.values(), key=lambda j: j.display_name.lower())
    for i, j in enumerate(rows, 1):
        name = j.display_name
        if len(name) > 52:
            name = name[:49] + '…'
        dub_note = '（跳过）' if j.dub_skipped and j.dub_wall_sec <= 0 else ''
        merge_note = '（跳过）' if j.merge_skipped and j.merge_wall_sec <= 0 else ''
        print(f'\n  {i}. {name}')
        print(f'     成片时长 {format_content_duration(j.video_sec)}'
              f'  |  配音轨 {format_content_duration(j.dub_mp3_sec)}')
        print(f'     配音耗时 {format_work_duration(j.dub_wall_sec)}{dub_note}'
              f'  |  合并耗时 {format_work_duration(j.merge_wall_sec)}{merge_note}'
              f'  |  状态 {j.status}')

    print('\n' + '-' * 50)
    print('【合计】')
    print(f'  完成视频：{totals["ok_count"]} 个')
    print(f'  成片总时长：{format_content_duration(totals["video_sec"])}')
    print(f'  配音轨总时长：{format_content_duration(totals["dub_mp3_sec"])}')
    print(f'  配音总耗时：{format_work_duration(totals["dub_wall_sec"])}')
    print(f'  合并总耗时：{format_work_duration(totals["merge_wall_sec"])}')
    print(f'  其它耗时：{format_work_duration(other_sec)}'
          f'（扫描/配置/文件整理等）')
    print(f'  本次总耗时：{format_work_duration(report.batch_wall_sec)}')

    if totals['video_sec'] > 0 and totals['ok_count'] > 0:
        completed_wall = sum(j.total_wall_sec for j in ok_jobs) + report.prep_wall_sec
        ratio = completed_wall / totals['video_sec']
        print('\n【效率参考】')
        print(f'  已完成部分：平均每小时成片约需 '
              f'{format_ratio_hours_per_hour(ratio)}'
              f'（从配音到合并，含准备）')
        full_ratio = report.batch_wall_sec / totals['video_sec']
        print(f'  含等待/跳过：本批次总墙钟 '
              f'{format_ratio_hours_per_hour(full_ratio)} / 小时成片')

    print_pipeline_perf_report(report)
    print('=' * 50)
