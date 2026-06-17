#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基于字幕间隙的语义块（Chunk）翻译：按 <2s 空白合并后送 Gemini，
动态注入 Max_Chinese_Chars，返回保持原时间轴的中文 SRT 行。
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from ai_shorten import (
    _gemini_generate,
    split_bilingual_text,
    zh_char_count,
)

GEMINI_CHUNK_TRANSLATE_INSTRUCTION = """你是一位顶级的「影视配音译配专家」。我会给你一组连续的原语言 SRT 字幕（非中文，可能是英语、俄语、韩语、日语等；它们属于同一个连贯的说话片段）。请将其翻译为优雅、地道的中文。

由于这些译文将直接用于 AI 语音合成（配音），你必须在翻译时严格遵守以下【动态字数与气口铁律】：

0. 【前期进框】：每条译文必须在对应时间行内自然念完；禁止写长句再指望后期加速。译法是口播摘要，不是完整字幕逐句译满。
1. 【严格字数限制】：当前这一整段字幕的总播放时间非常有限。你翻译出来的【所有中文总字数】（不含标点）绝对不能超过 {{Max_Chinese_Chars}} 个字！如果原文信息量太大，请在保持核心原意的前提下，尽量使用更凝练、文笔更干净、更 proactive 的中文进行压缩和删减。
2. 【语序重组】：请通读这组字幕的所有行，结合完整上下文，按照中文习惯重新排列组合语序。绝对不能一行行死板硬翻。
3. 【跨行断句与破折号换气】：
   - 尽量在某一行把一句话完整说完，让多余的行留空或仅留短词；
   - 如果因为意思太长，实在无法在一行内说完，必须拆分句尾并过渡到下一个时间行，请务必在前半句的中文末尾加上破折号「——」（例如：「我们今天聚在这里——\\n是为了庆祝这个时刻」）。破折号能帮助配音演员/TTS软件在此处自然换气并保持朗读的连贯性。
4. 【格式铁律】：必须保持输入的 SRT 序号和时间轴格式完全不变。只返回纯中文译文字幕，不要夹带原语言原文、批注或任何解释。

输入 SRT（原语言）如下，请按相同序号与时间轴输出中文 SRT：
"""


_ZH_SUBTITLE_RE = re.compile(
    r'\.(zh-hans|zh-cn|zh-hant|zh-tw|zh)\.(srt|vtt)$', re.I)


def should_use_chunk_translate(
    cfg: dict | None,
    *,
    subtitle_path: str | Path | None = None,
    cues: list | None = None,
) -> bool:
    """
    Chunk 翻译仅用于「原语言（非中文）→ 中文」流程（如 bilingual.srt）。
    已是 .zh-Hans / .zh-CN 等纯中文字幕时改走 rewrite 压缩，避免误把中文当原语言重译。
    """
    if not cfg or not bool(cfg.get('gemini_chunk_translate', True)):
        return False
    path = str(subtitle_path or '')
    name = Path(path).name.lower() if path else ''
    if _ZH_SUBTITLE_RE.search(name):
        return False
    if 'bilingual' in name:
        return True
    if cues:
        foreign_n = 0
        sample = cues[:min(40, len(cues))]
        for c in sample:
            en, _zh = split_bilingual_text(c.get('text', ''))
            src = (en or '').strip() or foreign_text_from_cue(c)
            if not src.strip():
                continue
            if re.search(r'[A-Za-z\u0400-\u04ff]', src) and not re.search(
                    r'[\u4e00-\u9fff]', src):
                foreign_n += 1
        if sample and foreign_n < max(1, len(sample) // 4):
            return False
    return True


def chunk_translate_config(cfg: dict | None = None) -> dict:
    defaults = {
        'chunk_gap_sec': 2.0,
        'chunk_chars_per_sec': 4.5,
        'chunk_min_chars': 1,
    }
    if cfg:
        for k in defaults:
            if k in cfg and cfg[k] is not None:
                defaults[k] = cfg[k]
    return defaults


def foreign_text_from_cue(cue: dict) -> str:
    """提取原语言原文（非中文行；双语块取 en 行，否则取无汉字行）。"""
    en, zh = split_bilingual_text(cue.get('text', ''))
    if en.strip():
        return en.strip()
    text = (cue.get('text') or '').strip()
    lines = []
    for line in text.split('\n'):
        line = line.strip()
        if line and not re.search(r'[\u4e00-\u9fff]', line):
            lines.append(line)
    if lines:
        return ' '.join(lines)
    return text


def format_srt_time(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',')


def parse_srt_blocks(content: str) -> list[dict]:
    """解析 SRT 文本为 [{start, end, text, seq?}, ...]。"""
    blocks = re.split(r'\n\s*\n', (content or '').strip())
    out: list[dict] = []
    for block in blocks:
        lines = [ln.rstrip() for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        seq = None
        time_idx = 0
        if re.match(r'^\d+$', lines[0].strip()):
            seq = int(lines[0].strip())
            time_idx = 1
        if time_idx >= len(lines):
            continue
        m = re.search(
            r'(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})',
            lines[time_idx],
        )
        if not m:
            continue
        start = _srt_time_to_sec(m.group(1))
        end = _srt_time_to_sec(m.group(2))
        text = '\n'.join(lines[time_idx + 1:]).strip()
        if text:
            out.append({
                'seq': seq,
                'start': start,
                'end': end,
                'text': text,
            })
    return out


def _srt_time_to_sec(t: str) -> float:
    t = t.replace(',', '.')
    parts = t.split(':')
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


@dataclass
class ChunkLine:
    cue_index: int
    seq: int
    start: float
    end: float
    foreign: str


@dataclass
class SubtitleChunk:
    chunk_id: int
    lines: list[ChunkLine] = field(default_factory=list)

    @property
    def total_duration(self) -> float:
        if not self.lines:
            return 0.0
        return max(float(self.lines[-1].end) - float(self.lines[0].start), 0.1)

    def max_chinese_chars(self, chars_per_sec: float) -> int:
        return max(1, int(math.floor(self.total_duration * chars_per_sec)))


def build_semantic_chunks(
    cues: list[dict],
    *,
    gap_sec: float = 2.0,
    chars_per_sec: float = 4.5,
) -> list[SubtitleChunk]:
    """
    按相邻行间隙 < gap_sec 合并为 Chunk；间隙 >= gap_sec 则切断。
    """
    if not cues:
        return []

    indexed: list[tuple[int, dict]] = [
        (i, c) for i, c in enumerate(cues)
        if foreign_text_from_cue(c).strip()
    ]
    if not indexed:
        return []

    chunks: list[SubtitleChunk] = []
    cur = SubtitleChunk(chunk_id=0)
    seq = 1

    for j, (ci, cue) in enumerate(indexed):
        foreign = foreign_text_from_cue(cue)
        line = ChunkLine(
            cue_index=ci,
            seq=seq,
            start=float(cue['start']),
            end=float(cue['end']),
            foreign=foreign,
        )
        seq += 1

        if cur.lines:
            gap = line.start - cur.lines[-1].end
            if gap >= gap_sec - 1e-6:
                chunks.append(cur)
                cur = SubtitleChunk(chunk_id=len(chunks))
        cur.lines.append(line)

    if cur.lines:
        chunks.append(cur)

    for ch in chunks:
        ch._chars_per_sec = chars_per_sec  # noqa: attach for reporting
    return chunks


def chunk_to_input_srt(chunk: SubtitleChunk) -> str:
    parts: list[str] = []
    for ln in chunk.lines:
        parts.append(str(ln.seq))
        parts.append(
            f'{format_srt_time(ln.start)} --> {format_srt_time(ln.end)}')
        parts.append(ln.foreign)
        parts.append('')
    return '\n'.join(parts).strip() + '\n'


def build_chunk_prompt(max_chinese_chars: int) -> str:
    return GEMINI_CHUNK_TRANSLATE_INSTRUCTION.replace(
        '{{Max_Chinese_Chars}}', str(max_chinese_chars))


def _match_times(a: float, b: float, tol: float = 0.05) -> bool:
    return abs(a - b) <= tol


def parse_chunk_response(
    raw: str,
    chunk: SubtitleChunk,
    *,
    chars_per_sec: float = 4.5,
) -> dict[int, str]:
    """
    解析 Gemini 返回的中文 SRT，按时间轴或顺序对齐到 cue_index。
    返回 {cue_index: zh_text}。
    """
    blocks = parse_srt_blocks(raw)
    out: dict[int, str] = {}

    if len(blocks) == len(chunk.lines):
        for blk, ln in zip(blocks, chunk.lines):
            zh = re.sub(r'\s+', '', blk['text'].replace('\n', ''))
            if zh:
                out[ln.cue_index] = blk['text'].strip()
        if out:
            return out

    for blk in blocks:
        for ln in chunk.lines:
            if ln.cue_index in out:
                continue
            if _match_times(blk['start'], ln.start) and _match_times(
                    blk['end'], ln.end):
                out[ln.cue_index] = blk['text'].strip()
                break

    if not out and blocks:
        for ln, blk in zip(chunk.lines, blocks):
            out[ln.cue_index] = blk['text'].strip()

    max_total = chunk.max_chinese_chars(chars_per_sec)
    total = sum(zh_char_count(v) for v in out.values())
    if total > max_total and out:
        ratio = max_total / max(total, 1)
        if ratio < 0.85:
            pass
    return out


def apply_chunk_translations(
    cues: list[dict],
    translations: dict[int, str],
) -> tuple[list[dict], int]:
    """将 chunk 译文写回 cues（保留 en\\nzh 双语格式）。"""
    out = [dict(c) for c in cues]
    changed = 0
    for idx, zh in translations.items():
        if idx < 0 or idx >= len(out):
            continue
        zh = (zh or '').strip()
        if not zh:
            continue
        en = foreign_text_from_cue(out[idx])
        old_en, old_zh = split_bilingual_text(out[idx].get('text', ''))
        new_body = f'{en}\n{zh}' if en else zh
        if new_body != out[idx].get('text', ''):
            changed += 1
        out[idx]['text'] = new_body
        out[idx]['zh_dub'] = re.sub(r'\s+', '', zh.replace('\n', ''))
    return out, changed


def chunks_to_plan(chunks: list[SubtitleChunk], cfg: dict) -> dict[str, Any]:
    ccfg = chunk_translate_config(cfg)
    cps = float(ccfg['chunk_chars_per_sec'])
    return {
        'mode': 'chunk_translate',
        'scope': f'语义块翻译（间隙<{ccfg["chunk_gap_sec"]}s 合并）',
        'total_chunks': len(chunks),
        'total_lines': sum(len(c.lines) for c in chunks),
        'chunk_gap_sec': ccfg['chunk_gap_sec'],
        'chars_per_sec': cps,
        'chunks': [
            {
                'chunk_id': c.chunk_id,
                'lines': len(c.lines),
                'total_duration': round(c.total_duration, 2),
                'max_chinese_chars': c.max_chinese_chars(cps),
                'cue_indices': [ln.cue_index for ln in c.lines],
            }
            for c in chunks
        ],
    }


def _translate_chunk_sync(
    chunk: SubtitleChunk,
    api_key: str,
    model: str,
    cfg: dict,
) -> tuple[dict[int, str], str, str, str]:
    ccfg = chunk_translate_config(cfg)
    cps = float(ccfg['chunk_chars_per_sec'])
    max_chars = chunk.max_chinese_chars(cps)
    prompt = build_chunk_prompt(max_chars)
    srt_in = chunk_to_input_srt(chunk)
    user_msg = (
        prompt
        + f'\n\n【本段 Total_Duration={chunk.total_duration:.2f}s，'
        f'Max_Chinese_Chars={max_chars}】\n\n'
        + srt_in
    )
    raw = _gemini_generate(user_msg, api_key, model)
    mapping = parse_chunk_response(raw, chunk, chars_per_sec=cps)
    return mapping, raw, user_msg, prompt


def build_chunk_compare_rows(
    cues_before: list[dict],
    cues_after: list[dict],
) -> list[dict]:
    rows: list[dict] = []
    for i, cue in enumerate(cues_after):
        if i >= len(cues_before):
            break
        en, zh_before = split_bilingual_text(cues_before[i].get('text', ''))
        _, zh_after = split_bilingual_text(cue.get('text', ''))
        if not zh_before and not zh_after:
            continue
        rows.append({
            'index': i,
            'cue_no': i + 1,
            'start': float(cue.get('start', 0)),
            'end': float(cue.get('end', 0)),
            'en': en or foreign_text_from_cue(cue),
            'zh_before': zh_before,
            'zh_after': zh_after,
            'changed': zh_after != zh_before,
            'kind': 'chunk_translate',
        })
    return rows


async def translate_cues_via_semantic_chunks(
    cues: list[dict],
    *,
    api_key: str,
    model: str,
    cfg: dict | None = None,
    on_chunk_error=None,
    log_session=None,
    subtitle_path: str | Path | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """
    按 <2s 间隙分 Chunk，逐块调用 Gemini，拼接为完整双语 cues。
    """
    ccfg = chunk_translate_config(cfg or {})
    gap = float(ccfg['chunk_gap_sec'])
    cps = float(ccfg['chunk_chars_per_sec'])

    chunks = build_semantic_chunks(cues, gap_sec=gap, chars_per_sec=cps)

    if (cfg or {}).get('gemini_preflight', True):
        from gemini_preflight import (
            preflight_chunk_translate,
            print_preflight_report,
            save_preflight_report,
        )
        pf = preflight_chunk_translate(
            cues, chunks, subtitle_path=subtitle_path, cfg=cfg or {})
        print_preflight_report(pf, title='Chunk 翻译自检')
        if subtitle_path:
            save_preflight_report(pf, video_or_sub=subtitle_path)
        if pf.get('blocked'):
            err = '; '.join((pf.get('errors') or ['自检拦截'])[:3])
            print(f'  Chunk 翻译已取消：{err}')
            return cues, {
                'mode': 'chunk_translate',
                'chunks': 0,
                'sent': 0,
                'changed': 0,
                'preflight': pf,
                'skip_reason': err,
            }

    if not chunks:
        return cues, {'chunks': 0, 'sent': 0, 'changed': 0}

    print(
        f'  [Chunk 翻译] {sum(len(c.lines) for c in chunks)} 行 → '
        f'{len(chunks)} 个语义块（间隙<{gap}s 合并，'
        f'字数上限={cps}字/秒）')

    loop = asyncio.get_event_loop()
    cues_before = [dict(c) for c in cues]
    out_cues = [dict(c) for c in cues]
    total_changed = 0
    failed: list[int] = []
    raw_responses: list[str] = []

    for ch in chunks:
        max_c = ch.max_chinese_chars(cps)
        print(
            f'  Chunk {ch.chunk_id + 1}/{len(chunks)}：'
            f'{len(ch.lines)} 行，{ch.total_duration:.1f}s，'
            f'≤{max_c} 字…',
            flush=True,
        )
        t0 = time.perf_counter()
        mapping: dict[int, str] = {}
        raw = ''
        user_msg = ''
        prompt = ''
        err: str | None = None
        try:
            mapping, raw, user_msg, prompt = await loop.run_in_executor(
                None,
                partial(
                    _translate_chunk_sync, ch, api_key, model, cfg or {}),
            )
        except Exception as e:
            err = str(e)
            print(f'  [警告] Chunk {ch.chunk_id} 失败：{err[:200]}')
            failed.append(ch.chunk_id)
            if on_chunk_error:
                fixed = await on_chunk_error(err)
                if fixed:
                    api_key, model = fixed
                    try:
                        mapping, raw, user_msg, prompt = await loop.run_in_executor(
                            None,
                            partial(
                                _translate_chunk_sync,
                                ch, api_key, model, cfg or {}),
                        )
                        err = None
                    except Exception as e2:
                        err = str(e2)
                        if log_session is not None:
                            log_session.record_chunk_batch(
                                chunk_id=ch.chunk_id,
                                lines=ch.lines,
                                instruction=prompt,
                                input_srt=chunk_to_input_srt(ch),
                                user_message=user_msg,
                                raw_response=raw,
                                mapping={},
                                cues=out_cues,
                                duration_ms=(time.perf_counter() - t0) * 1000,
                                total_duration=ch.total_duration,
                                max_chars=max_c,
                                error=err,
                            )
                        continue
                else:
                    continue
            else:
                continue

        duration_ms = (time.perf_counter() - t0) * 1000
        if raw:
            raw_responses.append(raw.strip())
        if log_session is not None:
            log_session.record_chunk_batch(
                chunk_id=ch.chunk_id,
                lines=ch.lines,
                instruction=prompt,
                input_srt=chunk_to_input_srt(ch),
                user_message=user_msg,
                raw_response=raw,
                mapping=mapping,
                cues=out_cues,
                duration_ms=duration_ms,
                total_duration=ch.total_duration,
                max_chars=max_c,
                error=err,
            )

        out_cues, n = apply_chunk_translations(out_cues, mapping)
        total_changed += n
        got = len(mapping)
        total_zh = sum(zh_char_count(v) for v in mapping.values())
        print(
            f'    → {got}/{len(ch.lines)} 行，'
            f'中文共 {total_zh} 字（限额 {max_c}）',
            flush=True,
        )

    compare_rows = build_chunk_compare_rows(cues_before, out_cues)
    stats = {
        'mode': 'chunk_translate',
        'chunks': len(chunks),
        'sent': sum(len(c.lines) for c in chunks),
        'changed': total_changed,
        'failed_chunks': failed,
        'chunk_gap_sec': gap,
        'chars_per_sec': cps,
        'plan': chunks_to_plan(chunks, cfg or {}),
        'compare_rows': compare_rows,
        'raw_responses': raw_responses,
    }
    return out_cues, stats


def write_translated_zh_srt(cues: list[dict], path: str) -> None:
    """导出纯中文 SRT（时间轴与原文一致）。"""
    from pathlib import Path
    p = Path(path)
    parts: list[str] = []
    n = 1
    for cue in cues:
        _, zh = split_bilingual_text(cue.get('text', ''))
        if not zh:
            continue
        parts.append(str(n))
        parts.append(
            f'{format_srt_time(float(cue["start"]))} --> '
            f'{format_srt_time(float(cue["end"]))}')
        parts.append(zh)
        parts.append('')
        n += 1
    p.write_text('\n'.join(parts).strip() + '\n', encoding='utf-8')
