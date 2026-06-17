#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Edge TTS rolling concurrency 调度器
==================================
队列 + 固定数量 worker，信号量限制在飞请求数；全局启动间隔；单 unit 独立超时与退避重试。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from tts_metrics import TtsMetricsCollector, normalize_failure_reason, tts_metrics_path_from_subtitle

HERE = Path(__file__).parent.resolve()
CONFIG_PATH = HERE / 'tubeget.config.json'

RETRY_BACKOFF_SEC = (10, 30, 90)
_SENTINEL = object()


def edge_ipv4_only_enabled(cfg_path: Path | None = None) -> bool:
    """
    Edge WebSocket 是否强制走 IPv4。
    Windows 上 IPv6 到 speech.platform.bing.com 常被重置，而 IPv4 正常。
    """
    path = cfg_path or CONFIG_PATH
    if path.exists():
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            if 'edge_ipv4_only' in data:
                return bool(data['edge_ipv4_only'])
        except (json.JSONDecodeError, OSError):
            pass
    return sys.platform == 'win32'


def make_edge_aiohttp_connector():
    """供 edge_tts.Communicate 使用的 aiohttp 连接器。"""
    if not edge_ipv4_only_enabled():
        return None
    import aiohttp
    return aiohttp.TCPConnector(family=socket.AF_INET, limit=0)


def edge_communicate(text: str, voice_id: str, rate: str = '+0%'):
    """创建 Edge Communicate（默认 Windows 下强制 IPv4）。"""
    import edge_tts
    return edge_tts.Communicate(
        text,
        voice_id,
        rate=rate,
        connector=make_edge_aiohttp_connector(),
    )


def format_edge_connect_error(err: str) -> str:
    """把 aiohttp/Windows 套接字错误翻译为可操作说明。"""
    low = (err or '').lower()
    if any(x in low for x in (
        '10054', '10064', 'connectionreset', 'clientconnectorerror',
        '指定的网络名不再可用', '远程主机强迫关闭',
    )):
        if edge_ipv4_only_enabled():
            return (
                f'{err} '
                '（本机到 speech.platform.bing.com 的 IPv6/TLS 异常；'
                '已启用 edge_ipv4_only，若仍失败请查代理/防火墙）'
            )
        return (
            f'{err} '
            '（常见原因：本机 IPv6 到 Bing 语音 TLS 被重置；'
            '可在 tubeget.config.json 设 "edge_ipv4_only": true）'
        )
    if '403' in low or 'invalid response status' in low:
        return f'{err}（Bing 拒绝 WebSocket，可能需升级 edge-tts 或稍后重试）'
    if 'timeout' in low or '超时' in err:
        return f'{err}（连接/合成超时）'
    return err


@dataclass
class EdgeSchedulerConfig:
    concurrency: int = 2
    launch_interval_sec: float = 4.0
    timeout_sec: float = 45.0
    max_retries: int = 3
    retry_backoff_sec: tuple[float, ...] = RETRY_BACKOFF_SEC


@dataclass
class EdgeRunStats:
    total_units: int = 0
    success: int = 0
    cached: int = 0
    failed: int = 0
    durations_sec: list[float] = field(default_factory=list)
    fail_reasons: Counter = field(default_factory=Counter)

    def record_success(self, elapsed: float, *, cached: bool = False) -> None:
        if cached:
            self.cached += 1
        else:
            self.success += 1
            self.durations_sec.append(elapsed)

    def record_failure(self, reason: str) -> None:
        self.failed += 1
        self.fail_reasons[reason] += 1

    def summary(self) -> dict:
        durs = self.durations_sec
        avg = statistics.mean(durs) if durs else 0.0
        p95 = 0.0
        if len(durs) >= 2:
            qs = statistics.quantiles(durs, n=20)
            p95 = qs[18]
        elif durs:
            p95 = durs[0]
        return {
            'total_units': self.total_units,
            'success': self.success,
            'cached': self.cached,
            'failed': self.failed,
            'avg_duration_sec': round(avg, 2),
            'p95_duration_sec': round(p95, 2),
            'fail_reasons': dict(self.fail_reasons),
        }


def edge_config_for_small_batch(
    pending_count: int,
    total: int,
    base: EdgeSchedulerConfig | None = None,
) -> EdgeSchedulerConfig:
    """补跑少量缺句：缩短超时与重试，避免长时间卡住。"""
    b = base or load_edge_scheduler_config()
    if pending_count <= 0:
        return b
    ratio = pending_count / max(total, 1)
    if pending_count > 5 and ratio > 0.01:
        return b
    return EdgeSchedulerConfig(
        concurrency=max(1, min(b.concurrency, pending_count)),
        launch_interval_sec=0.0,
        timeout_sec=min(15.0, b.timeout_sec),
        max_retries=1,
        retry_backoff_sec=(2.0,),
    )


def should_skip_edge_for_remainder(
    remaining: int, total: int, cached: int,
) -> bool:
    """绝大多数已成功、仅少量缺句时跳过 Edge，直接走 Kokoro/本地。"""
    if remaining <= 0:
        return True
    if remaining <= max(3, int(total * 0.005)):
        return True
    if cached > 0 and remaining / max(total, 1) <= 0.01:
        return True
    return False


def edge_connectivity_probe_enabled(cfg_path: Path | None = None) -> bool:
    path = cfg_path or CONFIG_PATH
    if path.exists():
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            if 'edge_connectivity_probe' in data:
                return bool(data['edge_connectivity_probe'])
        except (json.JSONDecodeError, OSError):
            pass
    return True


def load_edge_scheduler_config(cfg_path: Path | None = None) -> EdgeSchedulerConfig:
    path = cfg_path or CONFIG_PATH
    data: dict = {}
    if path.exists():
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    conc = int(data.get('edge_concurrency') or data.get('tts_concurrency') or 2)
    return EdgeSchedulerConfig(
        concurrency=max(1, min(conc, 8)),
        launch_interval_sec=float(
            data.get('edge_launch_interval_sec', 4.0)),
        timeout_sec=float(
            data.get('edge_unit_timeout_sec')
            or data.get('edge_timeout_sec', 45.0)),
        max_retries=max(1, int(data.get('edge_max_retries', 3))),
        retry_backoff_sec=tuple(
            float(x) for x in data.get(
                'edge_retry_backoff_sec', RETRY_BACKOFF_SEC)),
    )


def mp3_is_valid(path: str) -> bool:
    return bool(path) and os.path.exists(path) and os.path.getsize(path) > 500


def cue_mp3_path(work_dir: str, index: int) -> str:
    return os.path.join(work_dir, f'cue_{index:04d}.mp3')


async def probe_edge_tts(
    voice_id: str,
    rate: str,
    timeout_sec: float = 12.0,
) -> tuple[bool, str]:
    """启动前探测 Edge 是否可达，避免长时间无输出假死。"""
    import edge_tts
    import tempfile

    tmp = tempfile.mktemp(suffix='.mp3')
    try:
        comm = edge_communicate('测试', voice_id, rate=rate)
        save_task = asyncio.create_task(comm.save(tmp))
        try:
            await asyncio.wait_for(save_task, timeout=timeout_sec)
        except asyncio.TimeoutError:
            save_task.cancel()
            try:
                await save_task
            except (asyncio.CancelledError, Exception):
                pass
            return False, format_edge_connect_error(
                f'超时 {timeout_sec:g}s，speech.platform.bing.com 无响应')
        if mp3_is_valid(tmp):
            hint = ''
            if edge_ipv4_only_enabled():
                hint = '（IPv4）'
            return True, hint
        return False, 'Edge 返回空音频'
    except Exception as e:
        msg = str(e).strip() or type(e).__name__
        return False, format_edge_connect_error(msg[:200])
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


async def edge_save_once(
    text: str,
    voice_id: str,
    rate: str,
    out_path: str,
    timeout_sec: float,
) -> tuple[bool, str, float]:
    """单次 Edge 合成。返回 (成功, 失败原因, 耗时秒)。"""
    text = text.strip()
    if not text:
        return False, 'empty_text', 0.0

    t0 = time.monotonic()
    try:
        comm = edge_communicate(text, voice_id, rate=rate)
        save_task = asyncio.create_task(comm.save(out_path))
        try:
            await asyncio.wait_for(save_task, timeout=timeout_sec)
        except asyncio.TimeoutError:
            save_task.cancel()
            try:
                await save_task
            except (asyncio.CancelledError, Exception):
                pass
            _remove_invalid_mp3(out_path)
            return False, 'timeout', time.monotonic() - t0
        elapsed = time.monotonic() - t0
        if mp3_is_valid(out_path):
            return True, '', elapsed
        _remove_invalid_mp3(out_path)
        return False, 'invalid_output', elapsed
    except asyncio.TimeoutError:
        _remove_invalid_mp3(out_path)
        return False, 'timeout', time.monotonic() - t0
    except Exception as e:
        _remove_invalid_mp3(out_path)
        msg = format_edge_connect_error(str(e).strip() or type(e).__name__)
        return False, msg[:160], time.monotonic() - t0


def _remove_invalid_mp3(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    try:
        if os.path.getsize(path) <= 500:
            os.remove(path)
    except OSError:
        pass


def _load_status_writer(subtitle_path: str | Path | None):
    if not subtitle_path:
        return None, None, None
    from step1_subtitle_to_audio import load_dub_status, write_dub_status, dub_status_path
    return load_dub_status, write_dub_status, dub_status_path


def _cue_status_ok(prev: dict | None, index: int, mp3_path: str) -> bool:
    if not mp3_is_valid(mp3_path):
        return False
    if not prev:
        return False
    cues = prev.get('cues') or []
    if index >= len(cues):
        return False
    entry = cues[index]
    return entry.get('status') in ('success', 'cached')


def _build_status_state(
    status_meta: dict | None,
    prev: dict | None,
    total: int,
) -> dict:
    base = dict(status_meta or {})
    if prev:
        for k, v in prev.items():
            if k not in base and k != 'complete':
                base[k] = v
    prev_cues = prev.get('cues') if prev else None
    cues = list(base.get('cues') or prev_cues or [])
    if len(cues) < total:
        cues.extend({} for _ in range(total - len(cues)))
    base['cues'] = cues[:total]
    base['complete'] = False
    base['scheduler'] = 'edge_rolling'
    return base


async def run_edge_rolling_scheduler(
    cues: list[dict],
    voice_id: str,
    rate: str,
    work_dir: str,
    *,
    indices: list[int] | None = None,
    only_indices: set[int] | None = None,
    subtitle_path: str | Path | None = None,
    status_meta: dict | None = None,
    config: EdgeSchedulerConfig | None = None,
    text_extractor: Callable[[dict], str] | None = None,
    on_success: Callable[[int, str, dict], None] | None = None,
    metrics_path: str | Path | None = None,
    skip_indices: frozenset[int] | set[int] | None = None,
    rate_for_cue: Callable[[dict], str] | None = None,
) -> tuple[list[tuple[int, float, float, str]], list[int], dict]:
    """
    Rolling concurrency Edge TTS。

    返回 (results, failed_indices, stats_dict)。
    results: [(index, start, end, mp3_path), ...]
    """
    base_cfg = load_edge_scheduler_config()
    cfg = config or base_cfg
    extract = text_extractor or (lambda c: c.get('text', ''))
    os.makedirs(work_dir, exist_ok=True)

    load_status, write_status, _status_path_fn = _load_status_writer(subtitle_path)
    prev_status = load_status(str(subtitle_path)) if load_status and subtitle_path else None
    status_state = _build_status_state(status_meta, prev_status, len(cues))
    status_lock = asyncio.Lock()
    last_flush_at = [0.0]
    flush_interval_sec = 2.0

    all_indices = indices if indices is not None else list(range(len(cues)))
    explicit_indices = indices is not None
    if config is None and len(all_indices) <= max(5, len(cues) // 200 + 1):
        cfg = edge_config_for_small_batch(
            len(all_indices), len(cues), base_cfg)
    stats = EdgeRunStats(total_units=len(all_indices))
    metrics = TtsMetricsCollector(voice=voice_id)
    if metrics_path is None and subtitle_path:
        metrics_path = tts_metrics_path_from_subtitle(subtitle_path)

    results: dict[int, tuple[int, float, float, str]] = {}
    failed: set[int] = set()
    progress_lock = asyncio.Lock()
    in_flight = 0
    in_flight_lock = asyncio.Lock()
    work_started_at = time.monotonic()
    queue: asyncio.Queue = asyncio.Queue()
    slot_sem = asyncio.Semaphore(cfg.concurrency)
    launch_lock = asyncio.Lock()
    last_launch = [0.0]

    async def flush_status(
        extra: dict | None = None, *, force: bool = False,
    ) -> None:
        if not write_status or not subtitle_path:
            return
        now = time.monotonic()
        if not force and now - last_flush_at[0] < flush_interval_sec:
            return
        async with status_lock:
            if extra:
                status_state.update(extra)
            status_state['edge_stats'] = stats.summary()
            write_status(str(subtitle_path), status_state)
            last_flush_at[0] = time.monotonic()

    async def mark_unit(
        index: int,
        *,
        status: str,
        engine: str = 'edge',
        attempts: int = 1,
        elapsed: float = 0.0,
        error: str = '',
        cached: bool = False,
    ) -> None:
        entry = {
            'index': index,
            'status': status,
            'engine': engine,
            'attempts': attempts,
            'duration_sec': round(elapsed, 2),
            'mp3': cue_mp3_path(work_dir, index),
        }
        if error:
            entry['last_error'] = error
        if cached:
            entry['cached'] = True
        status_state['cues'][index] = entry
        succ = sum(
            1 for c in status_state['cues']
            if c.get('status') in ('success', 'cached'))
        status_state['success'] = succ
        status_state['failed_indices'] = sorted(
            i for i, c in enumerate(status_state['cues'])
            if c.get('status') == 'failed')
        await flush_status()

    def _record_cached_unit(i: int, *, from_status: str) -> None:
        cue = cues[i]
        text = extract(cue).strip()
        ts = metrics.begin_unit(i, text, voice=voice_id)
        metrics.finish_unit(
            i, text, voice=voice_id, start_time=ts,
            duration_sec=0.0, retry_count=0,
            status='cached' if from_status == 'cached' else 'cached',
        )

    skip = skip_indices or frozenset()

    # 续跑：跳过已成功 mp3 / dub-status
    to_synth: list[int] = []
    skipped_unsynth = 0
    for i in all_indices:
        if i in skip:
            skipped_unsynth += 1
            continue
        if only_indices is not None and i not in only_indices:
            mp3 = cue_mp3_path(work_dir, i)
            if mp3_is_valid(mp3):
                cue = cues[i]
                results[i] = (i, cue['start'], cue['end'], mp3)
                stats.record_success(0.0, cached=True)
                _record_cached_unit(i, from_status='cached')
            continue

        mp3 = cue_mp3_path(work_dir, i)
        if mp3_is_valid(mp3) and _cue_status_ok(prev_status, i, mp3):
            cue = cues[i]
            results[i] = (i, cue['start'], cue['end'], mp3)
            stats.record_success(0.0, cached=True)
            _record_cached_unit(i, from_status='cached')
            await mark_unit(i, status='cached', cached=True)
            continue
        if mp3_is_valid(mp3) and not explicit_indices:
            cue = cues[i]
            results[i] = (i, cue['start'], cue['end'], mp3)
            stats.record_success(0.0, cached=True)
            _record_cached_unit(i, from_status='cached')
            await mark_unit(i, status='success', cached=True)
            continue
        if mp3_is_valid(mp3):
            _remove_invalid_mp3(mp3)

        to_synth.append(i)

    if skipped_unsynth:
        print(f'  Edge：跳过 {skipped_unsynth} 条不可合成句', flush=True)

    await flush_status(force=True)

    pending = len(to_synth)
    done_count = stats.cached
    total_work = pending + stats.cached
    probe_failed = False
    if pending:
        print(
            f'  Edge rolling：{pending} 条待合成，'
            f'并发 {cfg.concurrency}，启动间隔 {cfg.launch_interval_sec:g}s，'
            f'超时 {cfg.timeout_sec:g}s',
            flush=True)
        ok_probe = True
        probe_err = ''
        if edge_connectivity_probe_enabled():
            probe_timeout = min(12.0, cfg.timeout_sec)
            print(f'  正在探测 Edge 连通性（最多 {probe_timeout:g}s）…', flush=True)
            ok_probe, probe_err = await probe_edge_tts(
                voice_id, rate, probe_timeout)
        if not ok_probe:
            probe_failed = True
            print(f'  [Edge 探测失败] {probe_err}', flush=True)
            print(
                f'  跳过 Edge（{pending} 条），接下来由 Kokoro 本地配音',
                flush=True)
            for i in to_synth:
                failed.add(i)
                await mark_unit(
                    i, status='failed', attempts=1, error=probe_err)
            done_count += len(to_synth)
            await flush_status(force=True)
        else:
            print('  Edge 连通正常，开始合成…', flush=True)
            est_first = cfg.launch_interval_sec + min(15.0, cfg.timeout_sec)
            print(
                f'  首条结果约 {est_first:.0f}s 内出现，每 12s 有心跳',
                flush=True)
            if done_count < total_work:
                print(
                    f'  [{"░" * 20}] '
                    f'{done_count * 100 // max(total_work, 1)}%  '
                    f'{done_count}/{total_work}  排队中…',
                    flush=True)
            for i in to_synth:
                await queue.put((i, 0))
    elif stats.cached:
        print(f'  Edge rolling：全部 {stats.cached} 条已缓存，跳过合成')

    async def bump_progress() -> None:
        nonlocal done_count
        async with progress_lock:
            done_count += 1
            if total_work <= 0:
                return
            pct = done_count * 100 // max(total_work, 1)
            filled = min(20, pct // 5)
            bar = '█' * filled + '░' * (20 - filled)
            line = f'  [{bar}] {pct}%  {done_count}/{total_work}'
            # Windows cmd 对 \r 单行刷新支持差，大批量定期换行
            if total_work > 30 and (
                    done_count % max(5, total_work // 40) == 0
                    or done_count == total_work):
                print(line, flush=True)
            else:
                print(line, end='\r', flush=True)

    async def heartbeat_loop() -> None:
        while done_count < total_work:
            await asyncio.sleep(12)
            if done_count >= total_work:
                break
            elapsed = int(time.monotonic() - work_started_at)
            async with in_flight_lock:
                flying = in_flight
            pct = done_count * 100 // max(total_work, 1)
            print(
                f'  … Edge {elapsed}s：{done_count}/{total_work} ({pct}%)'
                f'，在飞 {flying} 条',
                flush=True)
            if elapsed >= 30 and done_count <= stats.cached and flying == 0:
                print(
                    '  [警告] 30s 仍无合成进展，可能网络阻塞；'
                    '可 Ctrl+C 后重试或检查代理',
                    flush=True)

    async def worker_loop(worker_id: int) -> None:
        nonlocal in_flight
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                queue.task_done()
                break

            index, attempt = item
            async with in_flight_lock:
                in_flight += 1
            cue = cues[index]
            text = extract(cue).strip()
            mp3_path = cue_mp3_path(work_dir, index)

            try:
                unit_start = metrics.begin_unit(index, text, voice=voice_id)
                if not text:
                    stats.record_failure('empty_text')
                    failed.add(index)
                    metrics.finish_unit(
                        index, text, voice=voice_id, start_time=unit_start,
                        duration_sec=0.0, retry_count=attempt,
                        status='failed', failure_reason='empty_text')
                    await mark_unit(index, status='failed', attempts=attempt + 1,
                                    error='empty_text')
                    await bump_progress()
                    continue

                if attempt > 0:
                    bo_idx = min(attempt - 1, len(cfg.retry_backoff_sec) - 1)
                    await asyncio.sleep(cfg.retry_backoff_sec[bo_idx])

                if attempt == 0 and pending <= 5:
                    preview = text[:24].replace('\n', ' ')
                    print(
                        f'\n  Edge 第 {index + 1} 条'
                        f'（尝试 {attempt + 1}/{cfg.max_retries}，'
                        f'超时 {cfg.timeout_sec:g}s）… {preview}',
                        flush=True)

                async with slot_sem:
                    async with launch_lock:
                        now = time.monotonic()
                        gap = cfg.launch_interval_sec - (now - last_launch[0])
                        if gap > 0:
                            await asyncio.sleep(gap)
                        last_launch[0] = time.monotonic()

                    cue_rate = cue.get('tts_rate') or (rate_for_cue(cue) if rate_for_cue else rate)
                    ok, err, elapsed = await edge_save_once(
                        text, voice_id, cue_rate, mp3_path, cfg.timeout_sec)

                if not ok and pending <= 5:
                    print(f'  Edge 第 {index + 1} 条失败：{err or "unknown"}', flush=True)

                if ok:
                    stats.record_success(elapsed)
                    results[index] = (index, cue['start'], cue['end'], mp3_path)
                    failed.discard(index)
                    metrics.finish_unit(
                        index, text, voice=voice_id, start_time=unit_start,
                        duration_sec=elapsed, retry_count=attempt,
                        status='success')
                    await mark_unit(
                        index, status='success', attempts=attempt + 1, elapsed=elapsed)
                    if on_success:
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(
                            None, on_success, index, mp3_path, cue)
                    await bump_progress()
                    continue

                if attempt + 1 < cfg.max_retries:
                    await queue.put((index, attempt + 1))
                    continue

                norm_err = normalize_failure_reason(err or 'unknown')
                stats.record_failure(norm_err)
                failed.add(index)
                metrics.finish_unit(
                    index, text, voice=voice_id, start_time=unit_start,
                    duration_sec=elapsed, retry_count=attempt,
                    status='failed', failure_reason=err or 'unknown')
                await mark_unit(
                    index, status='failed', attempts=attempt + 1,
                    elapsed=elapsed, error=err or 'unknown')
                await bump_progress()

            finally:
                async with in_flight_lock:
                    in_flight = max(0, in_flight - 1)
                queue.task_done()

    if pending > 0 and queue.qsize() > 0:
        workers = [
            asyncio.create_task(worker_loop(w))
            for w in range(cfg.concurrency)
        ]
        heartbeat = asyncio.create_task(heartbeat_loop())
        try:
            await queue.join()
            for _ in workers:
                await queue.put(_SENTINEL)
            await queue.join()
            for w in workers:
                await w
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        if total_work > 0:
            pct = done_count * 100 // max(total_work, 1)
            filled = min(20, pct // 5)
            bar = '█' * filled + '░' * (20 - filled)
            print(f'  [{bar}] {pct}%  {done_count}/{total_work}', flush=True)

    summary = stats.summary()
    if probe_failed:
        summary['probe_failed'] = True
    metrics_summary = metrics.build_summary()
    if not probe_failed:
        if metrics_path:
            metrics.write_json(metrics_path)
            print(f'  指标已写入：{metrics_path}')
        metrics.print_report()
    else:
        print('  （Edge 未实际合成，跳过性能报告）', flush=True)

    # 保持 dub-status 内 edge_stats 与全局指标顶层字段同步（轻量摘要）
    summary.update({
        k: metrics_summary[k] for k in (
            'cache_hit_rate', 'generated', 'p50', 'p90', 'p95', 'p99',
            'distribution', 'total_retries',
        ) if k in metrics_summary
    })

    await flush_status(force=True)

    out = [results[i] for i in sorted(results)]
    return out, sorted(failed), summary


def print_edge_scheduler_config(cfg: EdgeSchedulerConfig | None = None) -> None:
    try:
        from edge_segment_scheduler import edge_segment_mode_enabled, print_edge_segment_config
        if edge_segment_mode_enabled():
            print_edge_segment_config()
            return
    except ImportError:
        pass
    c = cfg or load_edge_scheduler_config()
    print(
        f'  Edge 调度：并发={c.concurrency} 间隔={c.launch_interval_sec:g}s '
        f'超时={c.timeout_sec:g}s 重试={c.max_retries} '
        f'退避={list(c.retry_backoff_sec)}')


async def run_edge_scheduler(
    cues: list[dict],
    voice_id: str,
    rate: str,
    work_dir: str,
    **kwargs,
) -> tuple[list[tuple[int, float, float, str]], list[int], dict]:
    """按配置选择分段批量或逐句 rolling 调度。"""
    try:
        from edge_segment_scheduler import (
            edge_segment_mode_enabled,
            run_edge_segment_scheduler,
        )
        if edge_segment_mode_enabled():
            return await run_edge_segment_scheduler(
                cues, voice_id, rate, work_dir, **kwargs,
            )
    except ImportError:
        pass
    return await run_edge_rolling_scheduler(
        cues, voice_id, rate, work_dir, **kwargs,
    )
