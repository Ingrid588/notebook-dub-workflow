#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemini 用量估算：按字符累加，按月归档，供费用参考。"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gemini_log import GeminiLogSession

HERE = Path(__file__).parent.resolve()
PROJECT_ROOT = HERE.parent
DEFAULT_USAGE_DIR = PROJECT_ROOT / 'logs' / 'gemini'
USAGE_JSON = DEFAULT_USAGE_DIR / 'gemini_usage.json'
USAGE_TXT = DEFAULT_USAGE_DIR / 'gemini_usage.txt'
LOCK_PATH = DEFAULT_USAGE_DIR / 'gemini_usage.lock'

USAGE_RULE = (
    '每批 API 请求字符 = 系统指令字符 + 用户消息字符 + 模型回复字符；'
    '同一视频多批次求和。仅为估算，实际计费以 Google 账单 token 为准。'
)


def usage_paths() -> tuple[Path, Path]:
    root = os.environ.get('YOUTUBE_DUB_LOG_DIR', '').strip()
    if root:
        d = Path(root) / 'gemini'
    else:
        d = DEFAULT_USAGE_DIR
    return d / 'gemini_usage.json', d / 'gemini_usage.txt'


def _lock_paths() -> tuple[Path, Path]:
    json_path, txt_path = usage_paths()
    return json_path, json_path.with_suffix('.lock')


def _acquire_lock(lock_path: Path, timeout: float = 60.0) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            time.sleep(0.05)
        except OSError:
            time.sleep(0.05)
    return False


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def estimate_chars_from_session(session: GeminiLogSession) -> dict[str, int]:
    """从 GeminiLogSession 估算本视频 Gemini 处理字符数。"""
    input_chars = 0
    output_chars = 0
    batch_count = 0
    for batch in session.batches:
        if batch.get('error') and not batch.get('response_raw'):
            continue
        batch_count += 1
        input_chars += int(batch.get('instruction_chars') or 0)
        input_chars += int(batch.get('user_message_chars') or 0)
        output_chars += len(batch.get('response_raw') or '')
    return {
        'input_chars': input_chars,
        'output_chars': output_chars,
        'total_chars': input_chars + output_chars,
        'batch_count': batch_count,
        'cue_count': len(session.cues),
    }


def _empty_store() -> dict[str, Any]:
    return {
        'version': 1,
        'rule': USAGE_RULE,
        'months': {},
        'grand_total_chars': 0,
        'updated_at': '',
    }


def _load_store(json_path: Path) -> dict[str, Any]:
    if not json_path.exists():
        return _empty_store()
    try:
        data = json.loads(json_path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return _empty_store()
        data.setdefault('months', {})
        data.setdefault('grand_total_chars', 0)
        data.setdefault('rule', USAGE_RULE)
        return data
    except (OSError, json.JSONDecodeError):
        return _empty_store()


def _save_store(json_path: Path, data: dict[str, Any]) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = json_path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(json_path)


def _fmt_num(n: int) -> str:
    return f'{int(n):,}'


def render_usage_text(data: dict[str, Any]) -> str:
    lines = [
        'Gemini 用量估算（字符，仅供费用参考）',
        f'规则：{data.get("rule", USAGE_RULE)}',
        '',
    ]
    months: dict = data.get('months') or {}
    for month_key in sorted(months.keys(), reverse=True):
        block = months[month_key]
        total = int(block.get('total_chars') or 0)
        lines.append(f'━━ {month_key} ━━  本月累计：{_fmt_num(total)} 字符')
        for run in block.get('runs') or []:
            at = str(run.get('at', ''))[:16].replace('T', ' ')
            delta = int(run.get('delta_chars') or 0)
            month_after = int(run.get('month_total_after') or 0)
            video = run.get('video') or ''
            batches = run.get('batch_count') or 0
            cues = run.get('cue_count') or 0
            model = run.get('model') or ''
            tail = f'{video} ' if video else ''
            tail += f'({batches} 批/{cues} 句'
            if model:
                tail += f', {model}'
            tail += ')'
            lines.append(
                f'  {at}  +{_fmt_num(delta)}  {tail}  '
                f'本月 {_fmt_num(month_after)}')
        lines.append('')
    lines.extend([
        '═' * 42,
        f'全部运行累计：{_fmt_num(int(data.get("grand_total_chars") or 0))} 字符',
        '',
    ])
    return '\n'.join(lines)


def _write_text_report(txt_path: Path, data: dict[str, Any]) -> None:
    txt_path.write_text(render_usage_text(data), encoding='utf-8')


def record_video_gemini_usage(
    session: GeminiLogSession,
    *,
    video: str | Path,
    model: str = '',
) -> dict[str, Any]:
    """
    单视频 Gemini 完成后累加用量（进程安全，只增不减）。
    返回本次 delta 与更新后的汇总。
    """
    est = estimate_chars_from_session(session)
    delta = int(est['total_chars'])
    if delta <= 0:
        return {'recorded': False, 'delta_chars': 0, **est}

    json_path, lock_path = _lock_paths()
    _, txt_path = usage_paths()
    if not _acquire_lock(lock_path):
        return {'recorded': False, 'delta_chars': delta, 'error': 'lock_timeout', **est}

    try:
        data = _load_store(json_path)
        now = datetime.now()
        month_key = now.strftime('%Y-%m')
        months = data.setdefault('months', {})
        block = months.setdefault(month_key, {
            'month_key': month_key,
            'total_chars': 0,
            'runs': [],
        })
        block['total_chars'] = int(block.get('total_chars') or 0) + delta
        data['grand_total_chars'] = int(data.get('grand_total_chars') or 0) + delta
        data['updated_at'] = now.isoformat(timespec='seconds')

        run_row = {
            'at': now.isoformat(timespec='seconds'),
            'delta_chars': delta,
            'input_chars': est['input_chars'],
            'output_chars': est['output_chars'],
            'batch_count': est['batch_count'],
            'cue_count': est['cue_count'],
            'model': model or session.model,
            'video': Path(video).name,
            'month_total_after': block['total_chars'],
            'grand_total_after': data['grand_total_chars'],
        }
        block.setdefault('runs', []).append(run_row)

        _save_store(json_path, data)
        _write_text_report(txt_path, data)
        return {
            'recorded': True,
            'delta_chars': delta,
            'month_key': month_key,
            'month_total': block['total_chars'],
            'grand_total': data['grand_total_chars'],
            **est,
        }
    finally:
        _release_lock(lock_path)


def format_usage_summary(data: dict[str, Any] | None = None) -> str:
    json_path, _ = usage_paths()
    store = data if data is not None else _load_store(json_path)
    month_key = datetime.now().strftime('%Y-%m')
    month_total = int((store.get('months') or {}).get(month_key, {}).get('total_chars') or 0)
    grand = int(store.get('grand_total_chars') or 0)
    return (
        f'Gemini 用量估算：本月 {_fmt_num(month_total)} 字符，'
        f'历史累计 {_fmt_num(grand)} 字符'
        f'（详见 logs/gemini/gemini_usage.txt）'
    )


def print_usage_footer() -> None:
    json_path, _ = usage_paths()
    if not json_path.exists():
        return
    print(f'  {format_usage_summary()}')
