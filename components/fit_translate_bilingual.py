#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量「进框配音翻译」：读 bilingual SRT，用 Gemini API 按时间窗改写中文行，
写出 *.fit-dub.srt（前期进框，不靠后期 TTS 加速补救）。

NotebookLM 免费但无公开 API → 本脚本是同思路的 API 自动化替代。

用法：
  py -3.12 fit_translate_bilingual.py 文件夹
  py -3.12 fit_translate_bilingual.py 某课.en_US.bilingual.srt
环境：Windows 用户变量 GEMINI_API_KEY
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from functools import partial
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = Path(__file__).parent.resolve()

from ai_shorten import (
    DEFAULT_GEMINI_MODEL,
    _gemini_generate,
    _parse_json_array,
    cue_source_text,
    effective_gemini_model,
    ensure_gemini_api,
    split_bilingual_text,
)
from dub_naming import find_bilingual_sub_for_video, write_bilingual_srt
from dub_translate_prompts import (
    DEFAULT_CHARS_PER_SEC,
    DEFAULT_MAX_FILL,
    DEFAULT_MIN_FILL,
    DEFAULT_TARGET_FILL,
    GEMINI_FIT_DUB_INSTRUCTION,
    GEMINI_FIT_DUB_RETRY_NOTE,
    GEMINI_FIT_DUB_UNDERFLOW_NOTE,
    build_gemini_fit_payload_item,
    window_chars_for_duration,
    zh_len,
)
from step1_subtitle_to_audio import parse_srt

_SENTENCE_END_RE = re.compile(r'[.!?…]["\']?\s*$')


def _trim_zh_to_max(zh: str, max_c: int) -> str:
    """超 max_chars 时优先在标点处收句，避免硬切出半词。"""
    zh = re.sub(r'\s+', '', (zh or '').strip())
    if zh_len(zh) <= max_c:
        return zh
    best = ''
    for i, ch in enumerate(zh):
        if zh_len(zh[: i + 1]) > max_c:
            break
        if ch in '。！？，、；':
            best = zh[: i + 1]
    if best and zh_len(best) >= max(2, max_c * 2 // 3):
        return best
    return zh[:max_c]


def merge_en_fragments_for_fit_dub(
    cues: list[dict],
    *,
    max_gap: float = 1.2,
    max_duration: float = 10.0,
) -> tuple[list[dict], dict]:
    """
    合并连续 EN 分片为整句时间窗（en-zh-merge 常见）。
    避免对半句英文逐条进框导致中文只剩 3～5 字。
    """
    out: list[dict] = []
    buf_en: list[str] = []
    zh_hints: list[str] = []
    t_start: float | None = None
    t_end: float | None = None
    prev_end: float | None = None

    def flush() -> None:
        nonlocal buf_en, zh_hints, t_start, t_end
        if not buf_en or t_start is None or t_end is None:
            buf_en = []
            zh_hints = []
            t_start = t_end = None
            return
        full_en = ' '.join(buf_en).strip()
        zh_ref = ' '.join(zh_hints).strip()
        if full_en:
            text = f'{full_en}\n{zh_ref}' if zh_ref else full_en
            out.append({
                'start': t_start,
                'end': t_end,
                'text': text,
                'zh_ref': zh_ref,
            })
        buf_en = []
        zh_hints = []
        t_start = t_end = None

    for cue in cues:
        en, zh = split_bilingual_text(cue.get('text', ''))
        gap = (float(cue['start']) - prev_end) if prev_end is not None else 0.0
        if buf_en and gap > max_gap:
            flush()
        if buf_en and t_start is not None and float(cue['end']) - t_start > max_duration:
            flush()
        if t_start is None:
            t_start = float(cue['start'])
        t_end = float(cue['end'])
        if en.strip():
            buf_en.append(en.strip())
        if zh.strip():
            zh_hints.append(zh.strip())
        full = ' '.join(buf_en)
        if full and _SENTENCE_END_RE.search(full):
            flush()
        prev_end = float(cue['end'])
    flush()

    stats = {
        'before': len(cues),
        'after': len(out),
        'merged': max(0, len(cues) - len(out)),
    }
    return out, stats


def _should_merge_en_fragments(cues: list[dict]) -> bool:
    """短窗 EN 分片过多时先合并再进框翻译。"""
    if len(cues) < 8:
        return False
    short_en = 0
    for cue in cues:
        en, _zh = split_bilingual_text(cue.get('text', ''))
        if not en.strip():
            continue
        dur = max(0.1, float(cue['end']) - float(cue['start']))
        if dur < 3.0 and not _SENTENCE_END_RE.search(en.strip()):
            short_en += 1
    return short_en >= max(5, int(len(cues) * 0.15))


def _load_cfg() -> dict:
    p = HERE / 'tubeget.config.json'
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _parse_json_rows(raw: str) -> dict[int, str]:
    rows = _parse_json_array(raw)
    out: dict[int, str] = {}
    for row in rows:
        try:
            idx = int(row['index'])
            zh = re.sub(r'\s+', '', str(row.get('zh', '')).strip())
            if zh:
                out[idx] = zh
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _fit_window_params(
    start: float,
    end: float,
    *,
    chars_per_sec: float,
    target_fill: float,
    max_fill: float,
    min_fill: float,
) -> dict:
    return window_chars_for_duration(
        end - start,
        chars_per_sec=chars_per_sec,
        target_fill=target_fill,
        max_fill=max_fill,
        min_fill=min_fill,
    )


def _fit_batch_sync(
    batch: list[dict],
    api_key: str,
    model: str,
    *,
    strict: bool = False,
    underflow: bool = False,
) -> dict[int, str]:
    payload = []
    max_by_idx: dict[int, int] = {}
    min_by_idx: dict[int, int] = {}
    for item in batch:
        wp = item.get('_wp') or _fit_window_params(
            item['start'], item['end'],
            chars_per_sec=item.get('chars_per_sec', DEFAULT_CHARS_PER_SEC),
            target_fill=item.get('target_fill', DEFAULT_TARGET_FILL),
            max_fill=item.get('max_fill', DEFAULT_MAX_FILL),
            min_fill=item.get('min_fill', DEFAULT_MIN_FILL),
        )
        row = {
            'index': item['index'],
            'en': item['en'],
            'duration': wp['duration'],
            'target_chars': wp['target_chars'],
            'min_chars': wp['min_chars'],
            'max_chars': wp['max_chars'],
        }
        zh_ref = (item.get('zh_ref') or '').strip()
        if zh_ref:
            row['zh_ref'] = zh_ref
        payload.append(row)
        max_by_idx[item['index']] = wp['max_chars']
        min_by_idx[item['index']] = wp['min_chars']

    instr = GEMINI_FIT_DUB_INSTRUCTION
    if strict:
        instr += '\n' + GEMINI_FIT_DUB_RETRY_NOTE
    elif underflow:
        instr += '\n' + GEMINI_FIT_DUB_UNDERFLOW_NOTE
    user_msg = instr + '\n\n输入：\n' + json.dumps(
        payload, ensure_ascii=False, indent=2)
    raw = _gemini_generate(user_msg, api_key, model)
    mapping = _parse_json_rows(raw)

    for idx, max_c in max_by_idx.items():
        zh = mapping.get(idx, '')
        if zh and zh_len(zh) > max_c:
            mapping[idx] = _trim_zh_to_max(zh, max_c)
    return mapping


async def translate_cues_fit_window(
    cues: list[dict],
    *,
    api_key: str,
    model: str,
    batch_size: int = 18,
    chars_per_sec: float = DEFAULT_CHARS_PER_SEC,
    target_fill: float = DEFAULT_TARGET_FILL,
    max_fill: float = DEFAULT_MAX_FILL,
    min_fill: float = DEFAULT_MIN_FILL,
) -> tuple[list[dict], dict]:
    """全量按 en + 时间窗改写中文（不跳过「看起来正常」的句）。"""
    fill_kw = {
        'chars_per_sec': chars_per_sec,
        'target_fill': target_fill,
        'max_fill': max_fill,
        'min_fill': min_fill,
    }
    items: list[dict] = []
    for i, cue in enumerate(cues):
        en_line, zh_line = split_bilingual_text(cue.get('text', ''))
        source = cue_source_text(cue) or en_line
        if not source.strip():
            continue
        zh_ref = (cue.get('zh_ref') or zh_line or '').strip()
        wp = _fit_window_params(
            cue['start'], cue['end'], **fill_kw)
        items.append({
            'index': i,
            'en': source.strip(),
            'start': cue['start'],
            'end': cue['end'],
            'zh_ref': zh_ref,
            '_wp': wp,
            **fill_kw,
        })

    if not items:
        return cues, {'sent': 0, 'changed': 0}

    out = [dict(c) for c in cues]
    changed = 0
    overflow: list[int] = []
    underflow: list[int] = []
    loop = asyncio.get_event_loop()

    for b in range(0, len(items), batch_size):
        batch = items[b:b + batch_size]
        print(
            f'  Gemini 进框翻译 {b + 1}-{b + len(batch)}/{len(items)}…',
            flush=True,
        )
        mapping = await loop.run_in_executor(
            None,
            partial(_fit_batch_sync, batch, api_key, model, strict=False),
        )
        for item in batch:
            i = item['index']
            new_zh = mapping.get(i, '')
            if not new_zh:
                continue
            wp = item.get('_wp') or _fit_window_params(
                item['start'], item['end'], **fill_kw)
            if zh_len(new_zh) > wp['max_chars']:
                overflow.append(i)
            elif wp['duration'] >= 2.0 and zh_len(new_zh) < wp['min_chars']:
                underflow.append(i)
            en_old, _ = split_bilingual_text(out[i].get('text', ''))
            body = f'{en_old}\n{new_zh}' if en_old else new_zh
            if body != out[i].get('text', ''):
                changed += 1
            out[i]['text'] = body
            out[i]['tts_text'] = new_zh
            out[i]['fit_dub'] = True

    if overflow:
        retry_items = [it for it in items if it['index'] in overflow]
        print(f'  重试压缩 {len(retry_items)} 条仍偏长…', flush=True)
        for b in range(0, len(retry_items), batch_size):
            batch = retry_items[b:b + batch_size]
            mapping = await loop.run_in_executor(
                None,
                partial(_fit_batch_sync, batch, api_key, model, strict=True),
            )
            for item in batch:
                i = item['index']
                new_zh = mapping.get(i, '')
                if not new_zh:
                    continue
                wp = item.get('_wp') or _fit_window_params(
                    item['start'], item['end'], **fill_kw)
                if zh_len(new_zh) > wp['max_chars']:
                    new_zh = _trim_zh_to_max(new_zh, wp['max_chars'])
                en_old, _ = split_bilingual_text(out[i].get('text', ''))
                out[i]['text'] = f'{en_old}\n{new_zh}' if en_old else new_zh
                out[i]['tts_text'] = new_zh

    if underflow:
        retry_items = [it for it in items if it['index'] in underflow]
        print(f'  重试加长 {len(retry_items)} 条过短…', flush=True)
        for b in range(0, len(retry_items), batch_size):
            batch = retry_items[b:b + batch_size]
            mapping = await loop.run_in_executor(
                None,
                partial(
                    _fit_batch_sync, batch, api_key, model,
                    strict=False, underflow=True),
            )
            for item in batch:
                i = item['index']
                new_zh = mapping.get(i, '')
                if not new_zh:
                    continue
                wp = item.get('_wp') or _fit_window_params(
                    item['start'], item['end'], **fill_kw)
                if zh_len(new_zh) > wp['max_chars']:
                    new_zh = _trim_zh_to_max(new_zh, wp['max_chars'])
                en_old, _ = split_bilingual_text(out[i].get('text', ''))
                out[i]['text'] = f'{en_old}\n{new_zh}' if en_old else new_zh
                out[i]['tts_text'] = new_zh

    return out, {
        'sent': len(items),
        'changed': changed,
        'overflow': len(overflow),
        'underflow': len(underflow),
    }


def fit_dub_sub_path(sub: Path) -> Path:
    stem = sub.stem
    if stem.endswith('.quick-fit'):
        stem = stem[: -len('.quick-fit')]
    return sub.parent / f'{stem}.fit-dub.srt'


def _resolve_fit_source_sub(sub: Path) -> Path:
    """fit-dub 是产物；翻译输入应回到 en-zh-merge / bilingual 原字幕。"""
    stem = sub.stem
    if '.fit-dub' in stem:
        src = sub.parent / f'{stem.replace(".fit-dub", "")}{sub.suffix}'
        if src.is_file():
            return src
    return sub


def find_bilingual_subs(folder: Path) -> list[Path]:
    from dub_naming import is_bilingual_sub_dub_copy, is_bilingual_subtitle_file, is_dub_output
    subs = [
        f for f in folder.iterdir()
        if is_bilingual_subtitle_file(f) and not is_bilingual_sub_dub_copy(f)
        and '.fit-dub' not in f.stem
    ]
    if subs:
        return sorted(subs, key=lambda p: p.name.lower())
    videos = [
        f for f in folder.iterdir()
        if f.suffix.lower() == '.mp4' and not is_dub_output(f)
    ]
    found: list[Path] = []
    for v in videos:
        hit = find_bilingual_sub_for_video(v, folder)
        if hit:
            hit = _resolve_fit_source_sub(hit)
        if not hit:
            for cand in folder.glob(f'{v.stem}*.srt'):
                if '.fit-dub' in cand.stem or is_bilingual_sub_dub_copy(cand):
                    continue
                if 'en-zh-merge' in cand.name.lower() or 'bilingual' in cand.name.lower():
                    hit = cand
                    break
        if hit and hit not in found:
            found.append(hit)
    return sorted(found, key=lambda p: p.name.lower())


async def translate_subtitle_file(sub: Path, cfg: dict) -> Path | None:
    cues = parse_srt(sub.read_text(encoding='utf-8-sig'))
    if not cues:
        print(f'  [跳过] 空字幕：{sub.name}')
        return None

    cps = float(cfg.get('speech_activity_chars_per_sec', DEFAULT_CHARS_PER_SEC))
    target_fill = float(cfg.get('fit_dub_target_fill', cfg.get(
        'speech_activity_target_fill', DEFAULT_TARGET_FILL)))
    max_fill = float(cfg.get('fit_dub_max_fill', DEFAULT_MAX_FILL))
    min_fill = float(cfg.get('fit_dub_min_fill', DEFAULT_MIN_FILL))
    model = effective_gemini_model(
        cfg.get('ai_shorten_model_gemini') or DEFAULT_GEMINI_MODEL)
    auth = await ensure_gemini_api(
        '', model, interactive=False, prompt_if_missing_key=False)
    if not auth:
        print('  [错误] 未配置 GEMINI_API_KEY')
        print('         见 components\\GEMINI_API_KEY_SETUP.txt')
        return None
    api_key, use_model = auth

    print(f'\n── {sub.name} ({len(cues)} 条) ──')
    work_cues = cues
    if _should_merge_en_fragments(cues):
        work_cues, mstats = merge_en_fragments_for_fit_dub(cues)
        if mstats['merged'] > 0:
            print(
                f'  [合并] EN 分片 {mstats["before"]} → {mstats["after"]} 句'
                f'（整句进框，避免「生活没意」式截断）')
    t0 = time.perf_counter()
    new_cues, stats = await translate_cues_fit_window(
        work_cues, api_key=api_key, model=use_model,
        chars_per_sec=cps, target_fill=target_fill,
        max_fill=max_fill, min_fill=min_fill)
    out = fit_dub_sub_path(sub)
    write_bilingual_srt(new_cues, out)
    elapsed = time.perf_counter() - t0
    print(
        f'  完成：改写 {stats.get("changed", 0)}/{stats.get("sent", 0)} 条'
        f'，耗时 {elapsed:.1f}s')
    print(f'  输出：{out.name}')
    return out


async def translate_folder(folder: Path) -> int:
    subs = find_bilingual_subs(folder)
    if not subs:
        print(f'[错误] 未找到 bilingual 字幕：{folder}')
        return 1
    cfg = _load_cfg()
    print('=' * 50)
    print('  进框配音翻译（Gemini API，前期进框）')
    print(f'  文件夹：{folder}')
    print(f'  字幕数：{len(subs)}')
    print('  说明：NotebookLM 无 API；本脚本为自动化替代')
    print('  下一步：quick_bilingual_dub.bat 本文件夹（默认即可，无需 gemini）')
    print('=' * 50)

    ok = 0
    for sub in subs:
        if await translate_subtitle_file(sub, cfg):
            ok += 1
    print(f'\n完成：{ok}/{len(subs)}')
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) < 2:
        print('用法：py -3.12 fit_translate_bilingual.py 文件夹或.srt')
        return 1
    target = Path(sys.argv[1]).expanduser().resolve()
    if target.is_file():
        async def _one() -> int:
            cfg = _load_cfg()
            out = await translate_subtitle_file(target, cfg)
            return 0 if out else 1
        return asyncio.run(_one())
    if not target.is_dir():
        print(f'[错误] 路径不存在：{target}')
        return 1
    return asyncio.run(translate_folder(target))


if __name__ == '__main__':
    raise SystemExit(main())
