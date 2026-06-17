#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gemini 送优前自检：拦截误送中文、空原文、错误模式等，避免 token 浪费。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_ZH_RE = re.compile(r'[\u4e00-\u9fff]')
_ZH_SUBTITLE_RE = re.compile(
    r'\.(zh-hans|zh-cn|zh-hant|zh-tw|zh)\.(srt|vtt)$', re.I)

# 送 API 时禁止携带的「现有中文提示」字段
_FORBIDDEN_ZH_HINT_KEYS = frozenset({
    'zh', 'zh_before', 'zh_hint', 'zh_after', 'text', 'existing_zh',
})

_SOURCE_MODES = frozenset({
    'source_translate', 'en_rewrite', 'speech_align', 'chunk_translate',
})


def _cfg_bool(cfg: dict | None, key: str, default: bool = True) -> bool:
    if not cfg:
        return default
    v = cfg.get(key)
    return default if v is None else bool(v)


def payload_has_chinese_hints(payload: list[dict]) -> list[str]:
    """扫描 JSON 载荷是否夹带现有中文（应拒绝送 API）。"""
    errors: list[str] = []
    for row in payload or []:
        idx = row.get('index', '?')
        for k in _FORBIDDEN_ZH_HINT_KEYS:
            val = row.get(k)
            if val and _ZH_RE.search(str(val)):
                errors.append(
                    f'#{idx} 含禁止字段「{k}」（疑似现有中文，会致 Gemini 照抄浪费 token）')
        for seg in row.get('segments') or []:
            if isinstance(seg, dict):
                for k in ('zh', 'zh_before', 'text'):
                    val = seg.get(k)
                    if val and _ZH_RE.search(str(val)):
                        errors.append(
                            f'#{idx} segments 含「{k}」中文字段')
    return errors


def payload_empty_source(payload: list[dict], *, mode: str) -> list[str]:
    errors: list[str] = []
    for row in payload or []:
        idx = row.get('index', '?')
        if mode == 'source_translate':
            src = (row.get('source') or '').strip()
        else:
            src = (row.get('en') or row.get('source') or '').strip()
        if not src:
            errors.append(f'#{idx} 原语言原文为空，不应送 API')
        elif _ZH_RE.search(src) and not re.search(
                r'[A-Za-z\u0400-\u04ff\u3040-\u30ff\uac00-\ud7af]', src):
            errors.append(f'#{idx} 原语言字段疑似纯中文（应走本地字幕，不送 Gemini）')
    return errors


def audit_api_payload(
    payload: list[dict],
    mode: str,
    *,
    block_on_error: bool = True,
) -> tuple[bool, list[str]]:
    """
    送 API 前最后一道校验。返回 (通过, 错误列表)。
    block_on_error=False 时仅报告不阻断（由调用方决定）。
    """
    if mode not in _SOURCE_MODES:
        return True, []
    errors: list[str] = []
    errors.extend(payload_has_chinese_hints(payload))
    errors.extend(payload_empty_source(payload, mode=mode))
    ok = len(errors) == 0
    if not ok and block_on_error:
        return False, errors
    return ok or not block_on_error, errors


def preflight_rewrite_plan(
    items: list[dict],
    strategy: dict[str, Any],
    *,
    subtitle_path: str | Path | None = None,
    cfg: dict | None = None,
    only_problem_cues: bool = True,
) -> dict[str, Any]:
    """
    rewrite / source 模式：构建 items 后、调 API 前自检。
    可能缩减 items 或 blocked=True 完全取消。
    """
    cfg = cfg or {}
    report: dict[str, Any] = {
        'ok': True,
        'blocked': False,
        'warnings': [],
        'errors': [],
        'items_in': len(items),
        'items_out': len(items),
        'strategy': strategy.get('translate_mode'),
        'force_source_all': bool(strategy.get('force_source_all')),
    }

    path = str(subtitle_path or '')
    name = Path(path).name.lower() if path else ''
    if name and _ZH_SUBTITLE_RE.search(name):
        report['warnings'].append(
            f'字幕 {Path(path).name} 为纯中文轨；'
            f'无原语言时不应送 Gemini（当前计划 {len(items)} 条）')

    with_source = int(strategy.get('with_source') or 0)
    total_zh = int(strategy.get('total_zh') or 0)
    if total_zh and with_source < total_zh:
        report['warnings'].append(
            f'{total_zh - with_source} 条无原语言原文，已从送优列表剔除')

    # 全文送优 token 风险提示
    if strategy.get('force_source_all') and len(items) > 80:
        report['warnings'].append(
            f'触发全文从原语言翻译：将一次送 {len(items)} 条，token 消耗大；'
            f'若仅需问题句可在配置中提高 gemini_source_universal_long_ratio')

    if only_problem_cues and not strategy.get('force_source_all'):
        if len(items) > max(120, total_zh * 0.6) and total_zh:
            report['warnings'].append(
                f'「仅问题句」模式却计划送 {len(items)}/{total_zh} 条'
                f'（{len(items) * 100 // total_zh}%），请核对筛选规则')

    # 逐条：必须有原语言
    clean: list[dict] = []
    for item in items:
        src = (item.get('en') or '').strip()
        if not src:
            report['errors'].append(
                f'#{item.get("index", "?")} 无原语言，已剔除')
            continue
        if _ZH_RE.search(src) and not re.search(
                r'[A-Za-z\u0400-\u04ff\u3040-\u30ff\uac00-\ud7af]', src):
            report['errors'].append(
                f'#{item.get("index", "?")} 原语言字段为中文，已剔除')
            continue
        clean.append(item)

    if len(clean) < len(items):
        report['items_out'] = len(clean)
        items = clean

    if not items:
        report['ok'] = False
        report['blocked'] = True
        report['errors'].append('送优列表为空：无有效原语言条目，取消 API 调用')
        return report

    # 模拟载荷审计（与真实 batch 结构一致）
    mode = strategy.get('translate_mode') or 'en_rewrite'
    sim_payload = [
        {'index': it['index'], 'en': it.get('en', ''), 'kind': it.get('kind', '')}
        for it in items
    ]
    _, audit_errs = audit_api_payload(
        sim_payload, 'en_rewrite' if mode == 'en_rewrite' else 'source_translate',
        block_on_error=False,
    )
    report['errors'].extend(audit_errs)

    if report['errors'] and _cfg_bool(cfg, 'gemini_preflight_block', True):
        report['ok'] = False
        report['blocked'] = True

    report['items_out'] = len(items)
    report['_items'] = items
    return report


def preflight_chunk_translate(
    cues: list[dict],
    chunks: list,
    *,
    subtitle_path: str | Path | None = None,
    cfg: dict | None = None,
) -> dict[str, Any]:
    """Chunk 模式：确认输入为原语言而非纯中文。"""
    from gemini_chunk_translate import foreign_text_from_cue

    cfg = cfg or {}
    report: dict[str, Any] = {
        'ok': True,
        'blocked': False,
        'warnings': [],
        'errors': [],
        'chunks': len(chunks),
    }
    path = str(subtitle_path or '')
    name = Path(path).name.lower() if path else ''
    if name and _ZH_SUBTITLE_RE.search(name):
        report['ok'] = False
        report['blocked'] = True
        report['errors'].append(
            f'Chunk 模式不适用于纯中文字幕 {Path(path).name}；'
            f'请用 bilingual.srt 或关闭 gemini_chunk_translate')
        return report

    chinese_as_source = 0
    empty = 0
    for cue in cues:
        src = foreign_text_from_cue(cue)
        if not src.strip():
            empty += 1
        elif _ZH_RE.search(src) and not re.search(
                r'[A-Za-z\u0400-\u04ff\u3040-\u30ff\uac00-\ud7af]', src):
            chinese_as_source += 1

    if chinese_as_source > len(cues) * 0.25:
        report['ok'] = False
        report['blocked'] = True
        report['errors'].append(
            f'{chinese_as_source} 条原语言字段实为中文，Chunk 会误送/照抄')

    if not chunks:
        report['warnings'].append('未形成任何 Chunk（可能无原语言行）')
        if _cfg_bool(cfg, 'gemini_preflight_block', True):
            report['blocked'] = True
            report['ok'] = False

    if report['errors'] and _cfg_bool(cfg, 'gemini_preflight_block', True):
        report['blocked'] = True
        report['ok'] = False

    return report


def print_preflight_report(report: dict[str, Any], *, title: str = 'Gemini 送优自检') -> None:
    """终端输出自检结果（始终打印，便于发现浪费）。"""
    if report.get('blocked'):
        head = '[自检] 已拦截，不调用 API'
    elif report.get('warnings') or report.get('errors'):
        head = '[自检] 有警告'
    else:
        head = '[自检] 通过'

    parts = [head]
    if report.get('items_in') is not None:
        parts.append(
            f'{report.get("items_out", report["items_in"])}/'
            f'{report["items_in"]} 条将送 API')
    if report.get('chunks') is not None:
        parts.append(f'{report["chunks"]} 个 Chunk')
    print(f'  {" | ".join(parts)}')

    for w in report.get('warnings') or []:
        print(f'  [警告] {w}')
    for e in report.get('errors') or []:
        print(f'  [错误] {e}')


def save_preflight_report(
    report: dict[str, Any],
    *,
    video_or_sub: str | Path,
    mirror: bool = True,
) -> Path | None:
    """写入 *_C.gemini-preflight.json。"""
    from dub_naming import dub_stem

    p = Path(video_or_sub)
    if p.suffix.lower() in ('.srt', '.vtt'):
        base = p.parent / dub_stem(p.stem.split('.')[0] if '.' in p.stem else p.stem)
    else:
        base = p.parent / dub_stem(p.stem)
    out = Path(f'{base}.gemini-preflight.json')
    data = {k: v for k, v in report.items() if not k.startswith('_')}
    try:
        out.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        if mirror:
            log_dir = Path(__file__).parent.parent / 'logs' / 'gemini'
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / 'latest_gemini_preflight.json').write_text(
                out.read_text(encoding='utf-8'), encoding='utf-8')
        return out
    except OSError:
        return None
