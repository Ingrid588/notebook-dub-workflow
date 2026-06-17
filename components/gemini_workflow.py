#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemini 送优工作流：计划预览 + 记事本可读运行报告。"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from dub_naming import dub_stem
from progress_log import pipeline_step

HERE = Path(__file__).parent.resolve()
PROJECT_ROOT = HERE.parent
DEFAULT_LOG_DIR = PROJECT_ROOT / 'logs' / 'gemini'

KIND_LABEL = {
    'long': '过长',
    'suspicious': '误译嫌疑',
    'merge': '合并句',
    'short_rush': '过短',
    'speech_align': '口播优化',
    'source_all': '全文翻译',
    'ok': '正常',
    'skip': '跳过',
}

GEMINI_ALL_OK_MSG = '所有字幕长度符合标准，无需送 Gemini 优化'


def build_force_source_plan(
    cues: list,
    strategy: dict,
    *,
    speech_align: bool,
    only_problem_cues: bool,
    model: str = '',
) -> dict[str, Any]:
    """全文从原文翻译：跳过逐条筛选计划，仅保留报告所需摘要。"""
    from ai_shorten import effective_gemini_model, split_bilingual_text

    total_zh = 0
    for cue in cues:
        _, zh = split_bilingual_text(cue.get('text', ''))
        if zh:
            total_zh += 1
    gem_model = effective_gemini_model(model)
    mode = '口播优化（Speech Activity）' if speech_align else '原文全文翻译'
    return {
        'total_groups': len(cues),
        'total_zh': total_zh,
        'to_send_count': total_zh,
        'skip_count': 0,
        'scope': '全部中文组（全文从原文一次翻译）',
        'mode': mode,
        'only_problem_cues': only_problem_cues,
        'speech_align': speech_align,
        'kind_counts': {'source_all': total_zh},
        'to_send': [],
        'skipped_sample': [],
        'zero_send_hint': '',
        'long_count': strategy.get('long_count', 0),
        'long_ratio': strategy.get('long_ratio', 0),
        'long_threshold': strategy.get('long_threshold'),
        'universal_threshold': strategy.get('universal_threshold'),
        'force_source_all': True,
        'strategy_reason': strategy.get('reason', ''),
        'translate_mode': 'source_translate',
        'gemini_model': gem_model,
        'skip_detail_plan': True,
    }


def gemini_run_txt_path(video: Path) -> Path:
    return video.parent / f'{dub_stem(video.stem)}.gemini-run.txt'


def build_gemini_send_plan(
    cues: list,
    *,
    speech_align: bool,
    only_problem_cues: bool,
    zh_en_ratio: float,
    prep_cfg: dict | None = None,
) -> dict[str, Any]:
    """与 rewrite 相同规则，列出将送 / 将跳过的组。"""
    from ai_shorten import (
        assess_gemini_translate_strategy,
        classify_cue,
        cue_source_text,
        dub_prep_config,
        gemini_target_kind,
        split_bilingual_text,
    )

    cfg = dict(prep_cfg or dub_prep_config())
    cfg['only_problem_cues'] = only_problem_cues

    strategy = assess_gemini_translate_strategy(
        cues, zh_en_ratio,
        only_problem_cues=only_problem_cues, cfg=cfg,
    )
    if strategy['translate_mode'] == 'skip':
        return {
            'total_groups': len(cues),
            'total_zh': strategy.get('total_zh', 0),
            'to_send_count': 0,
            'scope': '跳过 Gemini',
            'mode': strategy['reason'],
            'translate_mode': 'skip',
            'strategy_reason': strategy['reason'],
            'skip_detail_plan': True,
            'zero_send_hint': f'Gemini 跳过：{strategy["reason"]}',
        }

    force_source_all = strategy['force_source_all']
    translate_mode = strategy['translate_mode']

    to_send: list[dict] = []
    skipped: list[dict] = []
    total_zh = 0

    for i, cue in enumerate(cues):
        en, zh = split_bilingual_text(cue.get('text', ''))
        source = cue_source_text(cue)
        if not zh:
            continue
        total_zh += 1
        win = float(cue.get('end', 0)) - float(cue.get('start', 0))
        row_base = {
            'index': i,
            'cue_no': i + 1,
            'start': float(cue.get('start', 0)),
            'end': float(cue.get('end', 0)),
            'window_sec': round(win, 2),
            'zh_preview': zh.replace('\n', ' ')[:72],
            'zh_len': len(zh.replace(' ', '')),
        }
        if not source.strip():
            skipped.append({**row_base, 'reason': 'no_foreign_source'})
            continue

        if force_source_all:
            kind = 'source_all'
            to_send.append({**row_base, 'kind': kind})
        elif only_problem_cues:
            kind = gemini_target_kind(cue, zh_en_ratio, cfg)
            if not kind:
                skipped.append({**row_base, 'reason': 'normal_or_filtered'})
                continue
            to_send.append({**row_base, 'kind': kind})
        else:
            kind = 'speech_align' if speech_align else 'all'
            to_send.append({**row_base, 'kind': kind})

    from ai_shorten import effective_gemini_model

    gem_model = effective_gemini_model()
    kind_counts = Counter(r['kind'] for r in to_send)
    scope = '仅问题句' if only_problem_cues else '全部中文组'
    if force_source_all:
        scope = f'全文从原文翻译（{strategy.get("with_source", "?")} 条有 en，API 不含中文）'
    mode = '口播优化（Speech Activity）' if speech_align else '字幕翻译优化'
    skip_count = len(skipped)

    zero_hint = (
        GEMINI_ALL_OK_MSG
        if only_problem_cues and total_zh and not to_send
        else (
            f'Gemini 无中文组可送（字幕 {total_zh} 组为空或无可处理项）'
            if not to_send
            else ''
        )
    )

    return {
        'total_groups': len(cues),
        'total_zh': total_zh,
        'to_send_count': len(to_send),
        'scope': scope,
        'mode': mode,
        'only_problem_cues': only_problem_cues,
        'speech_align': speech_align,
        'kind_counts': dict(kind_counts),
        'to_send': to_send,
        'skipped_sample': skipped[:8],
        'zero_send_hint': zero_hint,
        'skip_count': skip_count,
        'long_count': strategy['long_count'],
        'long_ratio': strategy['long_ratio'],
        'long_threshold': strategy['long_threshold'],
        'universal_threshold': strategy['universal_threshold'],
        'force_source_all': force_source_all,
        'strategy_reason': strategy['reason'],
        'translate_mode': translate_mode,
        'gemini_model': gem_model,
    }


def print_gemini_workflow_plan(
    plan: dict[str, Any],
    *,
    model: str = '',
    step: int = 2,
    total: int = 4,
    quiet: bool = False,
) -> None:
    """终端显示送优计划（milestone 时 quiet 也输出）。"""
    n = plan.get('to_send_count', 0)
    total_zh = plan.get('total_zh', 0)
    scope = plan.get('scope', '')
    mode = plan.get('mode', '')
    from ai_shorten import effective_gemini_model

    gem_model = effective_gemini_model(model or plan.get('gemini_model') or '')
    model_s = f'，模型 {gem_model}'

    if plan.get('skip_detail_plan'):
        if plan.get('translate_mode') == 'skip':
            pipeline_step(
                step, total,
                plan.get('strategy_reason') or plan.get('zero_send_hint') or '跳过 Gemini',
                quiet=quiet,
            )
            return
        pipeline_step(
            step, total,
            plan.get('strategy_reason') or '全文从原文翻译',
            quiet=quiet,
        )
        pipeline_step(
            step, total,
            f'Gemini 一次发送 {n}/{total_zh} 组原文{model_s}',
            quiet=quiet,
        )
        return

    if n == 0:
        hint = plan.get('zero_send_hint') or f'Gemini：{scope}，送优 0 组'
        if total_zh and plan.get('only_problem_cues'):
            hint = GEMINI_ALL_OK_MSG
        pipeline_step(step, total, hint, quiet=quiet)
        if total_zh and plan.get('only_problem_cues'):
            pipeline_step(
                step, total,
                f'说明：已检查 {total_zh} 组中文字幕，均未触发过长/过短/误译等规则',
                quiet=quiet,
            )
        return

    tm = plan.get('translate_mode', 'en_rewrite')
    if tm == 'chunk_translate' or plan.get('mode') == 'chunk_translate':
        n_chunks = plan.get('total_chunks', 0)
        cps = plan.get('chars_per_sec', 4.5)
        gap = plan.get('chunk_gap_sec', 2.0)
        pipeline_step(
            step, total,
            f'Chunk 语义块翻译：间隙<{gap}s 合并，{n_chunks} 块 / '
            f'{n} 行，字数上限 {cps} 字/秒{model_s}',
            quiet=quiet,
        )
        for row in (plan.get('chunks') or [])[:5]:
            pipeline_step(
                step, total,
                f'  块 #{row.get("chunk_id", 0) + 1}：'
                f'{row.get("lines", 0)} 行，'
                f'{row.get("total_duration", 0)}s，'
                f'≤{row.get("max_chinese_chars", 0)} 字',
                quiet=quiet,
                milestone=False,
            )
        if n_chunks > 5:
            pipeline_step(
                step, total,
                f'  … 另有 {n_chunks - 5} 块，详见 *.gemini-run.txt',
                quiet=quiet,
                milestone=False,
            )
        return

    if tm == 'source_translate':
        reason = plan.get('strategy_reason') or ''
        if reason:
            pipeline_step(step, total, reason, quiet=quiet)
        elif plan.get('force_source_all'):
            pipeline_step(
                step, total,
                f'超长句普遍超标 → 全部 {n} 组从原文翻译',
                quiet=quiet,
            )
        else:
            lr = float(plan.get('long_ratio', 0)) * 100
            th = float(plan.get('long_threshold', 1 / 3)) * 100
            pipeline_step(
                step, total,
                f'超长句 {lr:.0f}% > {th:.0f}% → 送优 {n} 组用原文翻译',
                quiet=quiet,
            )

    pipeline_step(
        step, total,
        f'Gemini {mode}（{scope}）→ 将送优 {n}/{total_zh} 组{model_s}',
        quiet=quiet,
    )

    kc = plan.get('kind_counts') or {}
    if kc:
        parts = [
            f'{KIND_LABEL.get(k, k)} {v}'
            for k, v in sorted(kc.items(), key=lambda x: -x[1])
        ]
        pipeline_step(
            step, total,
            f'送优类型：{"，".join(parts)}；跳过 {plan.get("skip_count", 0)} 组',
            quiet=quiet,
        )

    for row in (plan.get('to_send') or [])[:5]:
        kind = KIND_LABEL.get(row.get('kind', ''), row.get('kind', ''))
        preview = row.get('zh_preview', '')
        pipeline_step(
            step, total,
            f'  #{row.get("cue_no"):03d} [{kind}] {preview}…',
            quiet=quiet,
            milestone=False,
        )
    if n > 5:
        pipeline_step(
            step, total,
            f'  … 另有 {n - 5} 组，详见 *.gemini-run.txt',
            quiet=quiet,
            milestone=False,
        )


def _format_plan_section(plan: dict | None, *, model: str = '') -> list[str]:
    if not plan:
        return ['【送优计划】（未生成）']
    from ai_shorten import effective_gemini_model

    gem_model = effective_gemini_model(
        model or plan.get('gemini_model') or '')
    lines = [
        '【送优计划】',
        f'  模型：{gem_model}',
        f'  模式：{plan.get("mode", "?")}',
        f'  范围：{plan.get("scope", "?")}',
        f'  字幕组：{plan.get("total_groups", "?")}  含中文：{plan.get("total_zh", "?")}',
        f'  将送优：{plan.get("to_send_count", 0)}  跳过：{plan.get("skip_count", 0)}',
    ]
    tm = plan.get('translate_mode')
    if tm == 'source_translate':
        reason = plan.get('strategy_reason')
        if reason:
            lines.append(f'  送优策略：原文翻译（{reason}）')
        else:
            lr = float(plan.get('long_ratio', 0)) * 100
            th = float(plan.get('long_threshold', 1 / 3)) * 100
            lines.append(
                f'  送优策略：原文翻译（超长句 {lr:.0f}% > {th:.0f}%）')
    elif tm == 'zh_rewrite':
        lines.append('  送优策略：改现有中文（压缩/润色）')
    kc = plan.get('kind_counts') or {}
    if kc:
        lines.append(
            '  类型：' + '，'.join(
                f'{KIND_LABEL.get(k, k)} {v}' for k, v in kc.items()))
    if plan.get('zero_send_hint'):
        lines.append(f'  说明：{plan["zero_send_hint"]}')
    if plan.get('skip_detail_plan'):
        lines.append('  （全文翻译模式：不逐条列出，一次 API 发送全部原文）')
        return lines
    lines.append('')
    lines.append('【送优明细（前 30 组）】')
    if not plan.get('to_send'):
        lines.append('  （无 — 本次未向 Gemini 发送内容）')
    else:
        for row in plan['to_send'][:30]:
            kind = KIND_LABEL.get(row.get('kind', ''), row.get('kind', ''))
            lines.append(
                f'  #{row["cue_no"]:03d} [{kind}] '
                f'{row.get("start", 0):.1f}-{row.get("end", 0):.1f}s '
                f'{row.get("zh_len", 0)}字  {row.get("zh_preview", "")}')
        if plan['to_send_count'] > 30:
            lines.append(f'  … 共 {plan["to_send_count"]} 组')
    return lines


def _format_result_section(
    *,
    executed: bool,
    skip_reason: str,
    gem_stats: dict | None,
    model: str,
    log_json: str | None,
    log_md: str | None,
    compare_txt: str | None = None,
    zh_txt: str | None = None,
) -> list[str]:
    from ai_shorten import effective_gemini_model

    st = gem_stats or {}
    gem_model = effective_gemini_model(
        model or st.get('model') or '')
    lines = ['', '【执行结果】', f'  模型：{gem_model}']
    if skip_reason and not executed:
        lines.append('  状态：未执行')
        lines.append(f'  原因：{skip_reason}')
        return lines
    if not executed:
        lines.append('  状态：未启用 Gemini')
        return lines

    sent = st.get('sent', 0)
    changed = st.get('changed', 0)
    total = st.get('total', st.get('total_zh', ''))
    lines.extend([
        f'  状态：{"已调用 API" if sent else "已连接但未送优（0 组需优化）"}',
        f'  送优：{sent} 组' + (f' / 共 {total} 组中文' if total else ''),
        f'  有改写：{changed} 组',
    ])
    if log_json and os.path.exists(log_json):
        lines.append(f'  详细 JSON：{Path(log_json).name}')
    if log_md and os.path.exists(log_md):
        lines.append(f'  详细表格：{Path(log_md).name}')
    if compare_txt and os.path.exists(compare_txt):
        lines.append(f'  改前/改后对照：{Path(compare_txt).name}')
    if zh_txt and os.path.exists(zh_txt):
        lines.append(f'  仅新字幕：{Path(zh_txt).name}')
    return lines


def _format_rewrite_section(
    log_json: str | None,
    gem_stats: dict | None,
    *,
    compare_txt: str | Path | None = None,
    zh_txt: str | Path | None = None,
) -> list[str]:
    """从 gemini_log.json 或 compare 文件提取改前/改后，写入 gemini-run.txt。"""
    from gemini_log import cues_data_from_log_json, cues_data_from_stats

    lines = ['', '【优化结果（改前 → 改后）】']
    st = gem_stats or {}
    sent = int(st.get('sent', 0) or 0)
    changed = int(st.get('changed', 0) or 0)
    cues_data = cues_data_from_log_json(log_json)
    if not cues_data:
        cues_data = cues_data_from_stats(st)

    if compare_txt and os.path.exists(compare_txt):
        cmp_name = Path(compare_txt).name
        zh_name = Path(zh_txt).name if zh_txt and os.path.exists(zh_txt) else '*_C.gemini-zh.txt'
        lines.append(f'  完整对照见：{cmp_name}')
        lines.append(f'  仅新字幕见：{zh_name}')
        if cues_data:
            changed_rows = [c for c in cues_data if c.get('changed')]
            lines.append(f'  有改写：{len(changed_rows)} 组（以下预览前 15 组）')
            for c in changed_rows[:15]:
                no = c.get('cue_no', '?')
                zb = (c.get('zh_before') or '').replace('\n', ' ')[:120]
                za = (c.get('zh_after') or '').replace('\n', ' ')[:120]
                label = f'#{int(no):03d}' if isinstance(no, int) else f'#{no}'
                lines.append(label)
                lines.append(f'    改前：{zb}')
                lines.append(f'    改后：{za}')
            if len(changed_rows) > 15:
                lines.append(f'  … 另有 {len(changed_rows) - 15} 组，见 {Path(compare_txt).name}')
        elif changed > 0:
            lines.append(f'  （已改写 {changed} 组，请打开 {Path(compare_txt).name}）')
        return lines

    if not cues_data:
        if sent <= 0:
            lines.append('  （本次未送优，无改写内容）')
        elif changed <= 0:
            lines.append(
                f'  （已送优 {sent} 组，但 Gemini 回复与原文相同或未记录明细）')
            lines.append('  若已开启 gemini_log，请查看同目录 *_C.gemini_log.md')
        else:
            lines.append(
                f'  （已改写 {changed} 组，明细见同目录 *_C.gemini-compare.txt）')
        return lines

    changed_rows = [c for c in cues_data if c.get('changed')]
    unchanged_rows = [c for c in cues_data if not c.get('changed')]
    if changed_rows:
        lines.append(f'  有改写：{len(changed_rows)} 组（以下最多 50 组）')
        for c in changed_rows[:50]:
            kind = KIND_LABEL.get(c.get('kind', ''), c.get('kind', ''))
            zb = (c.get('zh_before') or '').replace('\n', ' ')
            za = (c.get('zh_after') or '').replace('\n', ' ')
            lines.append(f'  #{c.get("cue_no", "?"):03d} [{kind}]')
            lines.append(f'    改前：{zb[:160]}')
            lines.append(f'    改后：{za[:160]}')
        if len(changed_rows) > 50:
            lines.append(f'  … 另有 {len(changed_rows) - 50} 组，见 *.gemini_log.md')
    else:
        lines.append(
            f'  （已送优 {len(cues_data)} 组，Gemini 均返回与原文相同 — 未改写）')
        for c in cues_data[:10]:
            kind = KIND_LABEL.get(c.get('kind', ''), c.get('kind', ''))
            zh = (c.get('zh_before') or '').replace('\n', ' ')[:100]
            lines.append(f'  #{c.get("cue_no", "?"):03d} [{kind}] {zh}…')
        if len(unchanged_rows) > 10:
            lines.append(f'  … 共 {len(cues_data)} 组')
    return lines


def _format_prompt_reference(*, speech_align: bool = False, translate_mode: str = 'zh_rewrite') -> list[str]:
    """报告末尾附提示词名称与源码位置（完整正文见 ai_shorten.py）。"""
    from ai_shorten import (
        GEMINI_MERGE_NOTE,
        GEMINI_REWRITE_INSTRUCTION,
        GEMINI_SOURCE_MERGE_NOTE,
        GEMINI_SOURCE_TRANSLATE_INSTRUCTION,
        GEMINI_SPEECH_ALIGN_INSTRUCTION,
        GEMINI_SUSPICIOUS_NOTE,
    )

    lines = ['', '【本次使用的 Gemini 提示词】']
    if speech_align:
        lines.append('  模式：口播优化 → GEMINI_SPEECH_ALIGN_INSTRUCTION')
        body = GEMINI_SPEECH_ALIGN_INSTRUCTION.strip()
    elif translate_mode == 'source_translate':
        lines.append('  模式：原文翻译 → GEMINI_SOURCE_TRANSLATE_INSTRUCTION')
        body = GEMINI_SOURCE_TRANSLATE_INSTRUCTION.strip()
        lines.append('  源码：components/ai_shorten.py')
        lines.append('')
        for ln in body.split('\n'):
            lines.append(f'  {ln}')
        lines.append('')
        lines.append('  --- GEMINI_SOURCE_MERGE_NOTE ---')
        for ln in GEMINI_SOURCE_MERGE_NOTE.strip().split('\n'):
            lines.append(f'  {ln}')
        lines.append('')
        lines.append('  实际请求 = 上述指令 + 「\\n\\n输入：\\n」+ JSON（含 source / source_lang）')
        return lines
    else:
        lines.append('  模式：字幕翻译优化 → GEMINI_REWRITE_INSTRUCTION')
        lines.append('  （含 merge / suspicious 时追加 GEMINI_MERGE_NOTE / GEMINI_SUSPICIOUS_NOTE）')
        body = GEMINI_REWRITE_INSTRUCTION.strip()
    lines.append('  源码：components/ai_shorten.py')
    lines.append('')
    for ln in body.split('\n'):
        lines.append(f'  {ln}')
    if not speech_align:
        lines.append('')
        lines.append('  --- GEMINI_MERGE_NOTE ---')
        for ln in GEMINI_MERGE_NOTE.strip().split('\n'):
            lines.append(f'  {ln}')
        lines.append('')
        lines.append('  --- GEMINI_SUSPICIOUS_NOTE ---')
        for ln in GEMINI_SUSPICIOUS_NOTE.strip().split('\n'):
            lines.append(f'  {ln}')
    lines.append('')
    lines.append('  实际请求 = 上述指令 + 「\\n\\n输入：\\n」+ JSON 字幕批次')
    return lines


def render_gemini_run_text(
    video: Path,
    subtitle: Path,
    *,
    plan: dict | None,
    gem_stats: dict | None = None,
    skip_reason: str = '',
    executed: bool = False,
    model: str = '',
    log_json: str | Path | None = None,
    log_md: str | Path | None = None,
    compare_txt: str | Path | None = None,
    zh_txt: str | Path | None = None,
) -> str:
    from ai_shorten import effective_gemini_model

    gem_model = effective_gemini_model(
        model or (plan or {}).get('gemini_model')
        or (gem_stats or {}).get('model') or '')
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    lines = [
        '=' * 54,
        '  Gemini 运行报告（记事本可读）',
        '=' * 54,
        f'生成时间：{ts}',
        f'模型：{gem_model}',
        f'视频：{video.name}',
        f'字幕：{subtitle.name}',
        '',
    ]
    lines.extend(_format_plan_section(plan, model=gem_model))
    lines.extend(_format_result_section(
        executed=executed,
        skip_reason=skip_reason,
        gem_stats=gem_stats,
        model=gem_model,
        log_json=str(log_json) if log_json else None,
        log_md=str(log_md) if log_md else None,
        compare_txt=str(compare_txt) if compare_txt else None,
        zh_txt=str(zh_txt) if zh_txt else None,
    ))
    lines.extend(_format_rewrite_section(
        str(log_json) if log_json else None,
        gem_stats,
        compare_txt=str(compare_txt) if compare_txt else None,
        zh_txt=str(zh_txt) if zh_txt else None,
    ))
    speech_align = bool((plan or {}).get('speech_align'))
    translate_mode = (gem_stats or {}).get('translate_mode') or (plan or {}).get('translate_mode', 'zh_rewrite')
    if executed and (gem_stats or {}).get('sent', 0):
        lines.extend(_format_prompt_reference(
            speech_align=speech_align,
            translate_mode=translate_mode,
        ))
    lines.extend([
        '',
        '【说明】',
        '  若「将送优」为 0：配置为「仅问题句」且本地判定均正常，属预期。',
        '  若要每组都优化：运行 配置.bat → 选「全部中文组」。',
        '  logs/gemini/latest_gemini_run.txt 与本次视频报告内容相同（项目副本）。',
        '  完整逐句对照：同目录 *_C.gemini-compare.txt（改前/改后）',
        '  仅新字幕：*_C.gemini-zh.txt',
        '  详细 JSON/表格：*_C.gemini_log.json / *_C.gemini_log.md',
        '=' * 54,
        '',
    ])
    return '\n'.join(lines)


def save_gemini_run_report(
    video: Path,
    subtitle: Path,
    *,
    plan: dict | None = None,
    gem_stats: dict | None = None,
    skip_reason: str = '',
    executed: bool = False,
    model: str = '',
    log_json: str | Path | None = None,
    log_md: str | Path | None = None,
    compare_txt: str | Path | None = None,
    zh_txt: str | Path | None = None,
    mirror: bool = True,
    quiet: bool = False,
) -> Path:
    """写入 {stem}_C.gemini-run.txt，并镜像到 logs/gemini/。"""
    from gemini_log import (
        cues_data_from_log_json,
        cues_data_from_stats,
        save_gemini_rewrite_texts,
    )

    if not compare_txt and (gem_stats or log_json):
        cues_data = cues_data_from_log_json(log_json)
        if not cues_data:
            cues_data = cues_data_from_stats(gem_stats)
        if cues_data:
            compare_txt, zh_txt = save_gemini_rewrite_texts(
                video, cues_data, mirror=mirror)

    out = gemini_run_txt_path(video)
    text = render_gemini_run_text(
        video, subtitle,
        plan=plan, gem_stats=gem_stats, skip_reason=skip_reason,
        executed=executed, model=model,
        log_json=log_json, log_md=log_md,
        compare_txt=compare_txt, zh_txt=zh_txt,
    )
    out.write_text(text, encoding='utf-8')

    if mirror:
        try:
            DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
            latest = DEFAULT_LOG_DIR / 'latest_gemini_run.txt'
            latest.write_text(text, encoding='utf-8')
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            archive = DEFAULT_LOG_DIR / f'{video.stem}_{ts}.gemini-run.txt'
            archive.write_text(text, encoding='utf-8')
        except OSError:
            pass

    pipeline_step(
        4, 4,
        f'Gemini 报告已保存：{out.name}（记事本可开）',
        quiet=quiet,
    )
    if mirror and not quiet:
        print(f'             副本：logs/gemini/latest_gemini_run.txt', flush=True)
    return out


def load_gemini_dub_settings() -> dict[str, Any]:
    """读取 config + secrets，供 step1 等独立入口使用（不依赖 run_batch_course）。"""
    from ai_shorten import load_secrets, resolve_gemini_model

    cfg: dict = {}
    cfg_path = HERE / 'tubeget.config.json'
    if cfg_path.is_file():
        try:
            with open(cfg_path, encoding='utf-8') as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    sec = load_secrets()
    optimize = sec.get('optimize_subtitle')
    if optimize is None:
        optimize = cfg.get('optimize_subtitle', cfg.get('gemini_rewrite', False))

    provider = str(sec.get('ai_provider') or cfg.get('ai_provider', 'self')).lower()
    self_key = str(sec.get('gemini_api_key', '')).strip()
    platform_key = str(cfg.get('platform_gemini_key', '')).strip()
    if not bool(optimize):
        eff_key = ''
    elif provider == 'platform':
        eff_key = platform_key
    else:
        eff_key = self_key

    only_problem = sec.get('gemini_only_problem_cues')
    if only_problem is None:
        only_problem = cfg.get('gemini_only_problem_cues', True)

    return {
        'optimize_subtitle': bool(optimize),
        'gemini_key': eff_key,
        'gemini_model': resolve_gemini_model(sec, cfg),
        'gemini_batch': int(cfg.get('gemini_rewrite_batch', 20)),
        'gemini_only_problem_cues': bool(only_problem),
        'zh_en_ratio': float(cfg.get('zh_en_char_ratio', 1.0)),
        'cfg': cfg,
    }


async def run_gemini_screen_and_rewrite(
    cues: list,
    *,
    subtitle_path: str | Path | None = None,
    speech_align: bool = False,
    quiet: bool = False,
) -> tuple[list, dict[str, Any]]:
    """
    配音前 Gemini 质检：终端显示送优计划；有问题则调用 API 改写。
    返回 (cues, summary)。
    """
    from ai_shorten import (
        assess_gemini_translate_strategy,
        dub_prep_config,
        ensure_gemini_api,
        rewrite_cues_with_gemini,
    )
    from gemini_chunk_translate import (
        build_semantic_chunks,
        chunks_to_plan,
        should_use_chunk_translate,
        translate_cues_via_semantic_chunks,
    )

    settings = load_gemini_dub_settings()
    prep_cfg = dub_prep_config(settings['cfg'])
    aic = settings
    cfg = settings['cfg']
    chunk_translate = (
        should_use_chunk_translate(cfg, subtitle_path=subtitle_path, cues=cues)
        and bool(aic['optimize_subtitle'])
    )
    if bool(cfg.get('gemini_chunk_translate', True)) and not chunk_translate:
        if not quiet and subtitle_path:
            print(
                f'  [Chunk] 已跳过：{Path(subtitle_path).name} 为中文字幕，'
                f'改走 Gemini 改写/压缩（Chunk 仅用于原语言→中文）',
                flush=True,
            )
    use_gemini = bool(aic['gemini_key']) and bool(aic['optimize_subtitle'])
    summary: dict[str, Any] = {
        'plan': None,
        'stats': {},
        'skip_reason': '',
        'executed': False,
    }

    if not aic['optimize_subtitle']:
        summary['skip_reason'] = '未开启字幕优化'
        pipeline_step(
            2, 3,
            f'Gemini 已跳过：{summary["skip_reason"]}（直接配音）',
            quiet=quiet,
        )
        return cues, summary

    if not aic['gemini_key']:
        summary['skip_reason'] = '未配置 Gemini API Key'
        pipeline_step(
            2, 3,
            f'Gemini 已跳过：{summary["skip_reason"]}（运行 配置.bat 可开启）',
            quiet=quiet,
        )
        return cues, summary

    strategy = assess_gemini_translate_strategy(
        cues, aic['zh_en_ratio'],
        only_problem_cues=aic['gemini_only_problem_cues'],
        cfg=prep_cfg,
    )
    if chunk_translate:
        chunks = build_semantic_chunks(
            cues,
            gap_sec=float(cfg.get('chunk_gap_sec', 2.0)),
            chars_per_sec=float(cfg.get('chunk_chars_per_sec', 4.5)),
        )
        plan = chunks_to_plan(chunks, cfg)
        plan['total_zh'] = len(cues)
        plan['to_send_count'] = plan.get('total_lines', 0)
        plan['translate_mode'] = 'chunk_translate'
    elif strategy['force_source_all']:
        plan = build_force_source_plan(
            cues, strategy,
            speech_align=speech_align,
            only_problem_cues=aic['gemini_only_problem_cues'],
            model=aic['gemini_model'],
        )
    else:
        plan = build_gemini_send_plan(
            cues,
            speech_align=speech_align,
            only_problem_cues=aic['gemini_only_problem_cues'],
            zh_en_ratio=aic['zh_en_ratio'],
            prep_cfg=prep_cfg,
        )
    summary['plan'] = plan
    print_gemini_workflow_plan(
        plan,
        model=aic['gemini_model'],
        step=2,
        total=3,
        quiet=quiet,
    )

    if not plan.get('to_send_count'):
        summary['executed'] = True
        return cues, summary

    verified = await ensure_gemini_api(
        aic['gemini_key'], aic['gemini_model'],
        interactive=not quiet,
        prompt_if_missing_key=not quiet,
    )
    if not verified:
        summary['skip_reason'] = 'Gemini API 验证失败'
        pipeline_step(
            2, 3,
            f'Gemini 已跳过：{summary["skip_reason"]}（使用原字幕配音）',
            quiet=quiet,
        )
        return cues, summary

    gkey, gmodel = verified
    pipeline_step(
        2, 3,
        f'Gemini API 已连接（{gmodel}），'
        f'{"Chunk 语义块翻译" if chunk_translate else "一次翻译" if plan.get("force_source_all") else "正在优化"} '
        f'{plan["to_send_count"]} {"行" if chunk_translate else "组"}字幕…',
        quiet=quiet,
    )

    async def _retry_gemini_key(err: str):
        return await ensure_gemini_api(
            gkey, gmodel, interactive=False, error_hint=err,
            prompt_if_missing_key=False,
        )

    from gemini_log import GeminiLogSession, gemini_log_enabled

    gem_log = None
    if gemini_log_enabled(settings['cfg']):
        gem_log = GeminiLogSession(
            subtitle=subtitle_path,
            model=gmodel,
            mode=(
                'speech_align' if speech_align
                else 'chunk_translate' if chunk_translate
                else 'source_translate' if plan.get('force_source_all')
                else 'rewrite'),
        )

    if chunk_translate:
        cues, gem_stats = await translate_cues_via_semantic_chunks(
            cues,
            api_key=gkey,
            model=gmodel,
            cfg=cfg,
            on_chunk_error=_retry_gemini_key,
            log_session=gem_log,
            subtitle_path=str(subtitle_path) if subtitle_path else None,
        )
    else:
        cues, gem_stats = await rewrite_cues_with_gemini(
            cues,
            api_key=gkey,
            model=gmodel,
            batch_size=aic['gemini_batch'],
            on_batch_error=_retry_gemini_key,
            zh_en_ratio=aic['zh_en_ratio'],
            only_problem_cues=aic['gemini_only_problem_cues'],
            prep_cfg=prep_cfg,
            log_session=gem_log,
            subtitle_path=str(subtitle_path) if subtitle_path else None,
        )
    summary['stats'] = gem_stats
    summary['executed'] = True

    sent = gem_stats.get('sent', gem_stats.get('gemini_targets', 0))
    changed = gem_stats.get('changed', 0)
    if sent:
        if gem_stats.get('mode') == 'chunk_translate':
            pipeline_step(
                2, 3,
                f'Gemini 完成：{gem_stats.get("chunks", 0)} 块 / '
                f'{sent} 行，改写 {changed} 行',
                quiet=quiet,
            )
        else:
            pipeline_step(
                2, 3,
                f'Gemini 完成：API 送优 {sent} 组，改写 {changed} 组',
                quiet=quiet,
            )
    else:
        pipeline_step(2, 3, GEMINI_ALL_OK_MSG, quiet=quiet)

    if subtitle_path:
        try:
            sub_p = Path(subtitle_path)
            video_guess = sub_p.parent / f'{sub_p.stem.split(".")[0]}.mp4'
            if not video_guess.is_file():
                for ext in ('.mkv', '.webm', '.mov'):
                    alt = sub_p.parent / f'{sub_p.stem.split(".")[0]}{ext}'
                    if alt.is_file():
                        video_guess = alt
                        break
            report_video = video_guess if video_guess.is_file() else sub_p
            if gem_log is not None:
                gem_log.video = str(report_video)
            log_json = log_md = compare_txt = zh_txt = None
            if gem_log is not None:
                paths = gem_log.save(
                    report_video,
                    mirror_to_project_logs=bool(
                        settings['cfg'].get('gemini_log_mirror', True)),
                )
                log_json, log_md, compare_txt, zh_txt = paths
            elif gem_stats.get('compare_rows'):
                from gemini_log import save_gemini_rewrite_texts
                compare_txt, zh_txt = save_gemini_rewrite_texts(
                    report_video, gem_stats['compare_rows'],
                    mirror=bool(settings['cfg'].get('gemini_log_mirror', True)),
                )
            save_gemini_run_report(
                report_video,
                sub_p,
                plan=plan,
                gem_stats=gem_stats,
                executed=True,
                model=gmodel,
                log_json=log_json,
                log_md=log_md,
                compare_txt=compare_txt,
                zh_txt=zh_txt,
                mirror=bool(settings['cfg'].get('gemini_log_mirror', True)),
                quiet=quiet,
            )
            if not quiet and compare_txt:
                print(f'  Gemini 对照：{Path(compare_txt).name}', flush=True)
        except OSError:
            pass

    return cues, summary


def save_batch_run_index(
    video_results: list[dict],
    *,
    success: int,
    fail: int,
) -> Path:
    """批量结束后写入 logs/gemini/batch_latest.txt。"""
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEFAULT_LOG_DIR / 'batch_latest.txt'
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    lines = [
        '=' * 54,
        '  批量配音完成 — 日志索引（记事本可读）',
        '=' * 54,
        f'完成时间：{ts}',
        f'成功：{success}  失败/跳过：{fail}',
        '',
        '每个视频目录下（与 _C.mp4 同文件夹）：',
        '  *_C.gemini-run.txt   Gemini 送优计划与结果（必看）',
        '  *_C.dub-run.txt      本次配音运行摘要',
        '  *_C.qc-zh.srt        质检字幕（查缺句/截断）',
        '  *_C.gemini-compare.txt  改前/改后对照（推荐）',
        '  *_C.gemini-zh.txt       仅 Gemini 新字幕',
        '  *_C.gemini_log.md       Gemini 逐句详表（可选）',
        '',
        '项目汇总副本：',
        '  logs/gemini/latest_gemini_run.txt   最近一次 Gemini 报告',
        '  logs/gemini/latest_dub_run.txt      最近一次配音摘要',
        '  logs/gemini/gemini_usage.txt        Gemini 字符用量估算',
        '',
        '【本次处理的视频】',
    ]
    for r in video_results:
        folder = r.get('folder', '')
        name = r.get('video', '')
        tag = 'OK' if r.get('ok') else '失败'
        lines.append(f'  [{tag}] {folder}/{name}')
        if r.get('gemini_run'):
            lines.append(f'         Gemini：{r["gemini_run"]}')
        if r.get('dub_run'):
            lines.append(f'         摘要：{r["dub_run"]}')
    lines.extend(['', '=' * 54, ''])
    path.write_text('\n'.join(lines), encoding='utf-8')
    return path
