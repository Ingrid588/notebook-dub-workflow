#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemini 配音字幕改写：中文汉字数与英文字符数同比例，超长则压缩。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from functools import partial
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors

DEFAULT_GEMINI_MODEL = 'gemini-2.0-flash-lite'

# 与 Google AI Studio 模型列表对齐（显示名 → API model id）
GEMINI_MODEL_CATALOG: list[tuple[str, str]] = [
    ('gemini-3-flash-preview', 'Gemini 3 Flash'),
    ('gemini-2.5-pro-preview-tts', 'Gemini 2.5 Pro TTS'),
    ('gemini-2.5-pro', 'Gemini 2.5 Pro'),
    ('gemini-2.5-flash-preview-tts', 'Gemini 2.5 Flash TTS'),
    (
        'gemini-2.5-flash-native-audio-preview-09-2025',
        'Gemini 2.5 Flash Native Audio Dialog',
    ),
    ('gemini-2.5-flash-lite', 'Gemini 2.5 Flash Lite'),
    ('gemini-2.5-flash', 'Gemini 2.5 Flash'),
    ('gemini-2.0-flash-lite', 'Gemini 2 Flash Lite'),
]

# 弹窗下拉用 API id 列表（校验 / secrets 存储）
GEMINI_MODEL_CHOICES = [mid for mid, _ in GEMINI_MODEL_CATALOG]

GEMINI_MODEL_LABELS: dict[str, str] = dict(GEMINI_MODEL_CATALOG)
GEMINI_MODEL_IDS: dict[str, str] = {
    label: mid for mid, label in GEMINI_MODEL_CATALOG
}

# 字幕改写 / JSON 输出适用（TTS、Native Audio 仅出音频，勿用于配音字幕）
GEMINI_SUBTITLE_MODEL_IDS = frozenset({
    'gemini-3-flash-preview',
    'gemini-2.5-pro',
    'gemini-2.5-flash-lite',
    'gemini-2.5-flash',
    'gemini-2.0-flash-lite',
    'gemini-3.1-flash-lite',
})

_client_cache: dict[str, genai.Client] = {}

HERE = Path(__file__).parent.resolve()
SECRETS_PATH = HERE / 'tubeget.secrets.json'

GEMINI_ONE_SENTENCE_RULE = """
【一条一句，只压缩不拆句】
- 每条输入对应一条输出 zh_out，必须是**一句语义完整**的中文（口语自然，可含逗号，但须能独立念完）。
- 字数超 zh_max / max_chars 时：**同义改写、删冗余修饰**，把整句压进限额内；禁止拆成两个半句、禁止输出「……」未完待续。
- 禁止指望相邻条目的下一条来补全本句；每条都是独立配音单元。
- 宁可略短、留静音，也不要为凑字数拆句或堆字赶读。"""

GEMINI_REWRITE_INSTRUCTION = """你是专业课程配音字幕编辑。输入为 JSON 数组，每条含：
index（序号）、en（原文，唯一翻译依据）、en_chars（原文字母数字个数）、zh_max（本条中文汉字上限）、kind。

请**仅根据 en** 为每条输出配音用中文 zh_out（输入中不包含现有中文，请勿臆测或照抄外部译文）。

要求：
1. 准确传达 en 的含义；口语自然，适合旁白朗读，不要书面腔或机翻腔。
2. 一条一句：zh_out 必须是**一句完整中文**，同含义可改写得更短，但**不要拆成多句、不要半句话**。
3. 字数：汉字数必须 ≤ zh_max。超出时优先删冗余、换短说法，整句压进限额；禁止把一句拆成两条字幕来分担字数。
4. kind=suspicious：请按 en 的真实含义重写为一句自然口语，≤ zh_max。
5. 只输出中文汉字与常用标点（，。）；不要英文、不要括号说明、不要序号。
""" + GEMINI_ONE_SENTENCE_RULE + """
严格只输出 JSON 数组，无 markdown、无解释：
[{"index":0,"zh":"..."},{"index":1,"zh":"..."}]"""

GEMINI_SUSPICIOUS_NOTE = """
kind=suspicious 的条目：规则检测到「明显误译/字面翻译」嫌疑。
请按 en 真实含义重写为**一句**自然口语，≤ zh_max；同义压缩，不要拆句。
"""

GEMINI_MERGE_NOTE = """
kind=merge 的条目：原为两条相邻字幕，因时长过短无法自然朗读而合并时间轴。
segments 含各段 en、zh 及起止秒数。请合并为**一句**配音中文 zh_out：
- 准确覆盖全部 segments 的英文含义，口语自然，语义完整；
- 汉字数 ≤ zh_max；同义压缩，不要拆成两句。"""

from dub_translate_prompts import GEMINI_FIT_DUB_INSTRUCTION

GEMINI_SPEECH_ALIGN_INSTRUCTION = GEMINI_FIT_DUB_INSTRUCTION

GEMINI_SOURCE_TRANSLATE_INSTRUCTION = """你是精通影视译配的「配音台词翻译专家」。输入为 JSON 数组，每条含：
index、source_lang（en / ru / other）、source（原文台词，可能是英文、俄文等）、
window_sec（该条可用秒数）、zh_max（本条中文汉字上限）、
context_prev / context_next（相邻条原文片段，用于理解语境，可能为空）、kind。

请将 source 译为适合 AI 语音合成的中文 zh_out，遵守「配音专用铁律」：

1. **一条一句**：每条 index 只输出**一句完整中文**；同含义可改写得更短，**禁止拆成多句、禁止半句话**。
2. 语序：勿按 source 字面语序硬翻；口语自然。context 仅帮助理解，**不要把本句拆到下一条**。
3. 超长时：删冗余、换短词、整句压进 zh_max；**禁止**「智能分配」到相邻条目、禁止指望下一条补全。
4. 字数：zh_out 汉字数必须 ≤ zh_max；须在 window_sec 内从容念完这一句。
5. 语言：source_lang=en 从英文译；source_lang=ru 从俄文译；专名前后一致。
6. 输出：仅中文与常用标点（，。！？）；不要 source 原文、不要批注、不要 markdown。
7. kind=merge：segments 含多段原文，请**合并译为一段** zh_out（仍是一句完整中文），≤ zh_max。
""" + GEMINI_ONE_SENTENCE_RULE + """
严格只输出 JSON 数组：
[{"index":0,"zh":"..."},{"index":1,"zh":"..."}]"""

GEMINI_SOURCE_MERGE_NOTE = """
kind=merge：segments 为相邻多条原文（仅 en + 时间），请**合并译为一句**完整中文口播 zh_out，≤ zh_max；不要拆成多句。"""

# 中文句末标点（以中文为准决定是否与下一条合并）
ZH_SENTENCE_END_RE = re.compile(r'[。！？…!?]$')

# 英文俗语 → 常见字面误译（本地 0 token 直接修正，或标记送 Gemini）
_IDIOM_LITERAL_CHECKS: list[tuple[re.Pattern, re.Pattern, str]] = [
    (re.compile(r'\bmouthful\b', re.I), re.compile(r'一口'),
     '这名头可真够长的。'),
    (re.compile(r"\bthat's a mouthful\b|\bthat is a mouthful\b", re.I),
     re.compile(r'一口'), '这名头可真够长的。'),
]

# 整句已知坏译文（精确匹配，0 token 替换）
_KNOWN_BAD_ZH_FIXES: dict[str, str] = {
    '那是一口。': '这名头可真够长的。',
    '那是一口': '这名头可真够长的。',
}


def try_rule_fix_mistranslation(en: str, zh: str) -> tuple[str, bool]:
    """本地词典修正明显误译；成功返回 (新中文, True)。"""
    zh_stripped = zh.strip()
    if zh_stripped in _KNOWN_BAD_ZH_FIXES:
        if re.search(r'mouthful', en, re.I):
            return _KNOWN_BAD_ZH_FIXES[zh_stripped], True

    for en_pat, zh_pat, replacement in _IDIOM_LITERAL_CHECKS:
        if en_pat.search(en) and zh_pat.search(zh):
            return replacement, True
    return zh, False


def is_suspicious_mistranslation(en: str, zh: str, cfg: dict | None = None) -> bool:
    """
    规则检测「明显误译」嫌疑（0 token，不调用 API）。
    用于决定哪些句子需要送 Gemini 优化。
    """
    if not cfg or not cfg.get('detect_mistranslation', True):
        return False
    if not en or not zh:
        return False

    zh_stripped = zh.strip()
    if zh_stripped in _KNOWN_BAD_ZH_FIXES and re.search(r'mouthful', en, re.I):
        return True

    for en_pat, zh_pat, _ in _IDIOM_LITERAL_CHECKS:
        if en_pat.search(en) and zh_pat.search(zh):
            return True

    ec = en_char_count(en)
    zn = zh_char_count(zh)
    # 英文信息量明显大于中文，且中文极短（排除「好的。」「是的。」等）
    if ec >= 15 and zn <= 6 and ec / max(zn, 1) > 4:
        if not re.search(r'^(好的|是的|当然|没错|谢谢|你好)[。！？]?$', zh_stripped):
            return True

    return False


def apply_rule_mistranslation_fixes(cues: list) -> tuple[list, int]:
    """配音前：用本地词典直接修正已知误译（0 token）。"""
    fixed = 0
    for cue in cues:
        en, zh = split_bilingual_text(cue['text'])
        if not zh:
            continue
        new_zh, ok = try_rule_fix_mistranslation(en, zh)
        if ok and new_zh != zh:
            cue['text'] = f'{en}\n{new_zh}' if en else new_zh
            cue['rule_fixed'] = True
            fixed += 1
    return cues, fixed


def zh_ends_sentence(text: str) -> bool:
    """中文是否已到句末（未到句末则可能与下一条合并成一句配音）。"""
    _, zh = split_bilingual_text(text)
    if not zh:
        return True
    return bool(ZH_SENTENCE_END_RE.search(zh.strip()))


def combine_bilingual_text(en_a: str, zh_a: str, en_b: str, zh_b: str) -> str:
    """合并相邻双语块：英文空格拼接，中文直接拼接。"""
    en = ' '.join(p for p in (en_a.strip(), en_b.strip()) if p).strip()
    zh = (zh_a + zh_b).strip()
    if en and zh:
        return f'{en}\n{zh}'
    return zh or en


def _cue_gaps_sec(cues: list) -> list[float]:
    gaps: list[float] = []
    for i in range(len(cues) - 1):
        g = float(cues[i + 1]['start']) - float(cues[i]['end'])
        gaps.append(max(0.0, g))
    return gaps


def estimate_en_pause_threshold(
    cues: list,
    cfg: dict | None = None,
    *,
    fallback: float = 1.5,
) -> tuple[float, dict]:
    """
    从字幕间隔统计「外语真停顿」阈值：小于阈值视为外语时间轴连贯（翻译拆行），
    大于阈值视为说话人停顿（配音也应停）。
    """
    c = cfg or {}
    manual = c.get('merge_pause_threshold_sec')
    if manual is not None and str(manual).strip() != '':
        return float(manual), {'mode': 'manual', 'value': float(manual)}

    gaps = _cue_gaps_sec(cues)
    if not gaps:
        return fallback, {'mode': 'fallback', 'value': fallback}

    gs = sorted(gaps)
    n = len(gs)
    p50 = gs[n // 2]
    p75 = gs[int(n * 0.75)]
    p90 = gs[min(n - 1, int(n * 0.90))]
    # 翻译拆行常见 0~0.8s；真人思考/卡壳常 ≥1.5s
    auto = min(p90, max(p75 * 1.12, p50 + 0.35))
    lo = float(c.get('merge_pause_threshold_min', 0.45))
    hi = float(c.get('merge_pause_threshold_max', 2.5))
    value = max(lo, min(hi, auto))
    return value, {
        'mode': 'auto',
        'value': round(value, 3),
        'gap_p50': round(p50, 3),
        'gap_p75': round(p75, 3),
        'gap_p90': round(p90, 3),
        'samples': n,
    }


def _en_timeline_continuous(
    gap: float,
    en_buf: str,
    en_cur: str,
    *,
    pause_threshold: float,
) -> tuple[bool, str]:
    """外语时间轴是否连贯（非卡壳/思考停顿）。"""
    if gap > pause_threshold:
        return False, 'en_gap_pause'
    try:
        from speech_grouping import en_sentence_incomplete
    except ImportError:
        en_sentence_incomplete = lambda e: False  # noqa: E731

    en_b = (en_buf or '').strip()
    en_c = (en_cur or '').strip()
    if not en_b:
        return True, 'en_empty_buf'
    if en_sentence_incomplete(en_b):
        return True, 'en_incomplete'
    if en_c and en_c[0].islower():
        return True, 'en_continues_lower'
    if not re.search(r'[.!?…"\')\]]\s*$', en_b):
        return True, 'en_no_sentence_end'
    return True, 'en_gap_ok'


def should_merge_bilingual_cues(
    buf: dict,
    cur: dict,
    *,
    pause_threshold: float,
    max_duration: float,
    hard_max_duration: float,
) -> tuple[bool, str]:
    """
    合并判定（用户规则）：
    1. 中文未到句末标点 → 可能被翻译拆碎
    2. 外语时间轴连贯（间隔 ≤ 统计阈值）→ 非卡壳/思考
    同时满足则合并中文（不看字幕是否分行）；外语明显停顿则不合并。
    """
    if zh_ends_sentence(buf.get('text', '')):
        return False, 'zh_sentence_end'

    _, zh_next = split_bilingual_text(cur.get('text', ''))
    if not zh_next.strip():
        return False, 'no_zh_next'

    gap = float(cur['start']) - float(buf['end'])
    duration = float(cur['end']) - float(buf['start'])
    if duration > hard_max_duration:
        return False, 'group_too_long'
    if duration > max_duration:
        return False, 'group_max_duration'

    en_buf, _ = split_bilingual_text(buf.get('text', ''))
    en_cur, _ = split_bilingual_text(cur.get('text', ''))
    en_ok, en_reason = _en_timeline_continuous(
        gap, en_buf, en_cur, pause_threshold=pause_threshold,
    )
    if not en_ok:
        return False, en_reason

    return True, f'zh_incomplete+{en_reason}'


def merge_bilingual_cues_by_zh(
    cues: list,
    *,
    max_gap: float = 1.5,
    max_duration: float = 14.0,
    hard_max_duration: float = 20.0,
    cfg: dict | None = None,
) -> tuple[list, dict]:
    """
    双语合并：中文以句末标点为准；外语时间轴连贯则合并（翻译拆行），
    外语明显停顿则保留间隔（配音也停）。
    """
    if not cues:
        return cues, {'before': 0, 'after': 0, 'merged_pairs': 0}

    c = dict(cfg or {})
    if max_gap != 1.5:
        c.setdefault('merge_pause_threshold_sec', max_gap)
    pause_threshold, gap_meta = estimate_en_pause_threshold(cues, c, fallback=max_gap)

    before = len(cues)
    out: list[dict] = []
    buf = dict(cues[0])
    merged_pairs = 0
    reasons: dict[str, int] = {}
    skipped_pause = 0

    for cur in cues[1:]:
        do_merge, reason = should_merge_bilingual_cues(
            buf, cur,
            pause_threshold=pause_threshold,
            max_duration=max_duration,
            hard_max_duration=hard_max_duration,
        )
        if not do_merge and reason == 'en_gap_pause':
            skipped_pause += 1

        if do_merge:
            en_b, zh_b = split_bilingual_text(buf['text'])
            en_c, zh_c = split_bilingual_text(cur['text'])
            buf = {
                'start': buf['start'],
                'end': cur['end'],
                'text': combine_bilingual_text(en_b, zh_b, en_c, zh_c),
            }
            merged_pairs += 1
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            out.append(buf)
            buf = dict(cur)

    out.append(buf)
    return out, {
        'before': before,
        'after': len(out),
        'merged_pairs': merged_pairs,
        'kind': 'zh_sentence+en_timeline',
        'pause_threshold_sec': pause_threshold,
        'gap_stats': gap_meta,
        'kept_pause_gaps': skipped_pause,
        'merge_reasons': reasons,
    }


def dub_prep_config(cfg: dict | None = None) -> dict:
    """配音预处理 / Gemini 筛选用的参数字典。"""
    c = cfg or {}
    return {
        'chars_per_sec': float(c.get('zh_chars_per_sec', 4.0)),
        'max_speed': float(c.get('max_playback_speed', 1.5)),
        'min_cue_sec': float(c.get('min_cue_sec', 1.0)),
        'short_zh_merge_max_chars': int(c.get('short_zh_merge_max_chars', 8)),
        'only_problem_cues': bool(c.get('gemini_only_problem_cues', True)),
        'merge_zh_sentences': bool(c.get('merge_zh_sentences', True)),
        'merge_max_gap': float(c.get('merge_max_gap', 1.5)),
        'merge_max_duration': float(c.get('merge_max_duration', 14.0)),
        'merge_hard_max_duration': float(c.get('merge_hard_max_duration', 20.0)),
        'detect_mistranslation': bool(c.get('detect_mistranslation', True)),
        'gemini_min_overflow_ratio': float(c.get('gemini_min_overflow_ratio', 1.0)),
        'gemini_skip_merge': bool(c.get('gemini_skip_merge', False)),
        'gemini_skip_suspicious': bool(c.get('gemini_skip_suspicious', False)),
        'gemini_source_if_long_ratio': float(
            c.get('gemini_source_if_long_ratio', 1.0 / 3.0)),
        'gemini_source_universal_long_ratio': float(
            c.get('gemini_source_universal_long_ratio', 2.0 / 3.0)),
        'gemini_preflight': bool(c.get('gemini_preflight', True)),
        'gemini_preflight_block': bool(c.get('gemini_preflight_block', True)),
        'gemini_min_source_ratio': float(c.get('gemini_min_source_ratio', 0.5)),
        'use_zh_compress_strategy': bool(c.get('use_zh_compress_strategy', True)),
        'zh_strategy_path': c.get('zh_strategy_path') or '',
    }


def cue_zh_max(
    en: str,
    window_sec: float,
    ratio: float,
    cfg: dict,
) -> int:
    ec = en_char_count(en)
    zmax = zh_max_from_en(ec, ratio)
    return min(
        zmax,
        max_zh_chars_for_window(
            window_sec,
            chars_per_sec=cfg.get('chars_per_sec', 4.0),
            max_speed=cfg.get('max_speed', 1.5),
        ),
    )


def is_long_cue(
    en: str, zh: str, window_sec: float, ratio: float, cfg: dict,
) -> bool:
    if not zh:
        return False
    margin = float(cfg.get('gemini_min_overflow_ratio', 1.0))
    return zh_char_count(zh) > cue_zh_max(en, window_sec, ratio, cfg) * margin


def gemini_target_kind(cue: dict, ratio: float, cfg: dict) -> str | None:
    """返回需送 Gemini 的类型；None 表示跳过（含本地规则已修正的句）。"""
    kind = classify_cue(cue, ratio, cfg)
    if kind in ('ok', 'skip'):
        return None
    if cfg.get('gemini_skip_merge') and kind == 'merge':
        return None
    if cfg.get('gemini_skip_suspicious') and kind == 'suspicious':
        return None
    return kind


def is_short_rush_cue(
    en: str, zh: str, window_sec: float, cfg: dict,
) -> bool:
    """时长过短，TTS 容易「一晃而过」。"""
    if not zh:
        return False
    min_sec = float(cfg.get('min_cue_sec', 1.0))
    chars_per_sec = float(cfg.get('chars_per_sec', 4.0))
    max_short = int(cfg.get('short_zh_merge_max_chars', 8))
    zh_n = zh_char_count(zh)
    natural = max(min_sec, zh_n / max(chars_per_sec, 0.1) * 0.85)
    if window_sec < min_sec:
        return True
    if zh_n <= max_short and window_sec < natural:
        return True
    return False


def _cue_segment(cue: dict) -> dict:
    en, zh = split_bilingual_text(cue['text'])
    return {
        'start': cue['start'],
        'end': cue['end'],
        'en': en,
        'zh': zh,
    }


def merge_two_cues(a: dict, b: dict) -> dict:
    segs: list[dict] = []
    if a.get('segments'):
        segs.extend(a['segments'])
    else:
        segs.append(_cue_segment(a))
    segs.append(_cue_segment(b))
    en = ' '.join(s['en'] for s in segs if s['en']).strip()
    zh = ''.join(s['zh'] for s in segs)
    return {
        'start': segs[0]['start'],
        'end': segs[-1]['end'],
        'text': f'{en}\n{zh}' if en else zh,
        'segments': segs,
        'merged': True,
    }


def merge_short_adjacent_cues(
    cues: list, cfg: dict,
) -> tuple[list, dict]:
    """将过短字幕与相邻条合并，争取朗读时间。"""
    stats = {'before': len(cues), 'merged_pairs': 0, 'after': 0}
    if not cues:
        return [], stats

    result: list[dict] = []
    i = 0
    while i < len(cues):
        cue = cues[i]
        en, zh = split_bilingual_text(cue['text'])
        win = cue['end'] - cue['start']
        if is_short_rush_cue(en, zh, win, cfg) and not zh_ends_sentence(cue['text']):
            if i + 1 < len(cues):
                merged = merge_two_cues(cue, cues[i + 1])
                result.append(merged)
                stats['merged_pairs'] += 1
                i += 2
                continue
            if result:
                result[-1] = merge_two_cues(result[-1], cue)
                stats['merged_pairs'] += 1
                i += 1
                continue
        result.append({**cue, 'merged': False, 'segments': None})
        i += 1

    stats['after'] = len(result)
    return result, stats


def classify_cue(
    cue: dict, ratio: float, cfg: dict,
) -> str:
    """ok | long | merge | suspicious | skip"""
    en, zh = split_bilingual_text(cue['text'])
    if not zh:
        return 'skip'
    if cue.get('rule_fixed'):
        return 'ok'
    if cue.get('merged'):
        return 'merge'
    win = cue['end'] - cue['start']
    if is_long_cue(en, zh, win, ratio, cfg):
        return 'long'
    if is_suspicious_mistranslation(en, zh, cfg):
        return 'suspicious'
    return 'ok'


def analyze_cues(
    cues: list, ratio: float, cfg: dict, *, label: str = '',
) -> dict:
    """统计长句 / 误译嫌疑 / 短句 / 需 Gemini 数量（不调用 API）。"""
    long_i: list[int] = []
    short_i: list[int] = []
    merge_i: list[int] = []
    suspicious_i: list[int] = []
    ok_i: list[int] = []
    gemini_i: list[int] = []
    for i, cue in enumerate(cues):
        en, zh = split_bilingual_text(cue['text'])
        if not zh:
            continue
        win = cue['end'] - cue['start']
        if cue.get('merged'):
            merge_i.append(i)
        elif is_long_cue(en, zh, win, ratio, cfg):
            long_i.append(i)
        elif is_suspicious_mistranslation(en, zh, cfg) and not cue.get('rule_fixed'):
            suspicious_i.append(i)
        elif is_short_rush_cue(en, zh, win, cfg):
            short_i.append(i)
        else:
            ok_i.append(i)
        gkind = gemini_target_kind(cue, ratio, cfg)
        if gkind:
            gemini_i.append(i)
    gemini_n = len(gemini_i)
    if not cfg.get('only_problem_cues', True):
        gemini_n = len(long_i) + len(merge_i) + len(suspicious_i) + len(ok_i)
    return {
        'label': label,
        'total_zh': (
            len(long_i) + len(short_i) + len(merge_i)
            + len(suspicious_i) + len(ok_i)
        ),
        'long': len(long_i),
        'suspicious': len(suspicious_i),
        'short_rush': len(short_i),
        'merged': len(merge_i),
        'ok': len(ok_i),
        'gemini_targets': gemini_n,
        'gemini_indices': gemini_i,
        'long_indices': long_i,
        'suspicious_indices': suspicious_i,
        'short_indices': short_i,
        'merge_indices': merge_i,
    }


def print_cue_analysis(stats: dict, prep: dict) -> None:
    """打印预处理与 Gemini 筛选摘要。"""
    if prep.get('zh_sentence_merged'):
        print(
            f'  中文整句合并：{prep.get("before", "?")} → '
            f'{prep.get("after_zh", prep.get("after", "?"))} 条'
            f'（合并 {prep["zh_sentence_merged"]} 处英文断句）')
    if prep.get('before') != prep.get('after') and prep.get('merged_pairs'):
        print(
            f'  短句合并：{prep.get("after_zh", prep["before"])} → {prep["after"]} 条'
            f'（合并 {prep["merged_pairs"]} 对）')
    if prep.get('rule_fixed'):
        print(f'  本地误译修正：{prep["rule_fixed"]} 条（词典替换，0 token）')
    lc = prep.get('local_compress') or {}
    if lc.get('tried'):
        print(
            f'  本地口播压缩：{lc.get("fixed", 0)}/{lc["tried"]} 条'
            f'（策略 {lc.get("strategy_source", "builtin")}，'
            f'仍超长 {lc.get("still_long", 0)} 条 → 可送 Gemini）')
    print(
        f'  字幕分析：长句 {stats["long"]}，误译嫌疑 {stats.get("suspicious", 0)}，'
        f'过短 {stats["short_rush"]}，已合并 {stats["merged"]}，正常 {stats["ok"]}')
    total = max(stats.get('total_zh', 0), 1)
    long_n = stats.get('long', 0)
    lr = long_n / total
    print(
        f'  超长句占比：{long_n}/{stats.get("total_zh", 0)}'
        f'（{lr * 100:.1f}%）')
    ts = stats.get('translate_strategy') or {}
    if ts.get('force_source_all'):
        print(f'  → {ts["reason"]}；全部 {stats.get("total_zh", 0)} 组将送优')
    elif ts.get('translate_mode') == 'source_translate':
        print(f'  → {ts["reason"]}')
    elif stats.get('only_problem'):
        skip = stats['total_zh'] - stats['gemini_targets']
        pct = stats['gemini_targets'] * 100 // max(stats['total_zh'], 1)
        print(
            f'  Gemini 仅处理 {stats["gemini_targets"]}/{stats["total_zh"]} 条'
            f'（约 {pct}%，1.5×速仍放不下；跳过 {skip} 条，省 token）')
    else:
        print(f'  Gemini 将处理全部 {stats["gemini_targets"]} 条')


def prepare_cues_for_dub(
    cues: list, ratio: float, cfg: dict,
) -> tuple[list, dict]:
    """中文整句合并 + 短句合并 + 分析，返回配音用 cues 与统计。"""
    if cfg.get('merge_zh_sentences', True):
        zh_merged, zh_prep = merge_bilingual_cues_by_zh(
            cues,
            max_gap=float(cfg.get('merge_max_gap', 1.5)),
            max_duration=float(cfg.get('merge_max_duration', 14.0)),
            hard_max_duration=float(cfg.get('merge_hard_max_duration', 20.0)),
            cfg=cfg,
        )
    else:
        zh_merged, zh_prep = cues, {
            'before': len(cues), 'after': len(cues),
            'merged_pairs': 0, 'kind': 'zh_sentence',
        }

    merged, short_prep = merge_short_adjacent_cues(zh_merged, cfg)
    merged, rule_fixed_n = apply_rule_mistranslation_fixes(merged)
    compress_stats = {}
    if cfg.get('use_zh_compress_strategy', True):
        from zh_compress import apply_local_compress_to_cues
        merged, compress_stats = apply_local_compress_to_cues(
            merged, ratio, cfg)
    analysis = analyze_cues(merged, ratio, cfg, label='after_merge')
    analysis['only_problem'] = cfg.get('only_problem_cues', True)
    strategy = assess_gemini_translate_strategy(
        merged, ratio,
        only_problem_cues=bool(cfg.get('only_problem_cues', True)),
        cfg=cfg,
    )
    analysis['translate_strategy'] = strategy
    if strategy['force_source_all']:
        analysis['gemini_targets'] = analysis['total_zh']
    analysis['prep'] = {
        'before': zh_prep['before'],
        'after_zh': zh_prep['after'],
        'after': short_prep['after'],
        'zh_sentence_merged': zh_prep['merged_pairs'],
        'merged_pairs': short_prep['merged_pairs'],
        'rule_fixed': rule_fixed_n,
        'local_compress': compress_stats,
    }
    return merged, analysis


def load_secrets() -> dict:
    from gemini_secrets import (
        merge_secrets_with_env,
        strip_legacy_key_from_secrets_file,
    )
    if strip_legacy_key_from_secrets_file(SECRETS_PATH):
        print(
            '  [安全] 已从 tubeget.secrets.json 移除 API Key；'
            '请改用 Windows 用户环境变量 GEMINI_API_KEY（见 components\\GEMINI_API_KEY_SETUP.txt）',
            flush=True,
        )
    if not SECRETS_PATH.exists():
        return merge_secrets_with_env({})
    try:
        with open(SECRETS_PATH, encoding='utf-8') as f:
            return merge_secrets_with_env(json.load(f))
    except Exception:
        return merge_secrets_with_env({})


def save_secrets(data: dict) -> None:
    from gemini_secrets import sanitize_secrets_for_save
    clean = sanitize_secrets_for_save(data)
    with open(SECRETS_PATH, 'w', encoding='utf-8') as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)


def split_bilingual_text(text: str) -> tuple[str, str]:
    """从双语字幕块拆出英文行与中文行。"""
    en_parts: list[str] = []
    zh_parts: list[str] = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        if re.search(r'[\u4e00-\u9fff]', line):
            zh_parts.append(line)
        else:
            en_parts.append(line)
    en = ' '.join(en_parts)
    zh = re.sub(r'\s+', '', ''.join(zh_parts))
    return en, zh


def cue_source_text(cue: dict) -> str:
    """提取可用于 Gemini 的原语言原文（非中文，绝不返回已有中文行）。"""
    en, _zh = split_bilingual_text(cue.get('text', ''))
    if en.strip():
        return en.strip()
    foreign: list[str] = []
    for line in (cue.get('text') or '').split('\n'):
        line = line.strip()
        if line and not re.search(r'[\u4e00-\u9fff]', line):
            foreign.append(line)
    return ' '.join(foreign).strip()


def count_source_cues(cues: list) -> tuple[int, int, int]:
    """返回 (含中文条数, 含原语言原文条数, 超长条数)。"""
    prep = dub_prep_config()
    total_zh = 0
    with_source = 0
    long_n = 0
    for cue in cues:
        en, zh = split_bilingual_text(cue.get('text', ''))
        if not zh:
            continue
        total_zh += 1
        src = cue_source_text(cue)
        if src:
            with_source += 1
        if cue.get('merged'):
            continue
        win = float(cue.get('end', 0)) - float(cue.get('start', 0))
        if is_long_cue(en or src, zh, win, 1.0, prep):
            long_n += 1
    return total_zh, with_source, long_n


def detect_source_lang(text: str) -> str:
    """识别原语言语种：en / ru / ko / ja / other。"""
    t = (text or '').strip()
    if not t:
        return 'other'
    if re.search(r'[\u0400-\u04ff]', t):
        return 'ru'
    if re.search(r'[\u3040-\u30ff]', t):
        return 'ja'
    if re.search(r'[\uac00-\ud7af]', t):
        return 'ko'
    if re.search(r'[A-Za-z]', t):
        return 'en'
    return 'other'


def count_long_zh_cues(
    cues: list,
    ratio: float,
    cfg: dict | None = None,
) -> tuple[int, int]:
    """返回 (含中文条数, 超长条数)。"""
    prep = cfg or dub_prep_config()
    total = 0
    long_n = 0
    for cue in cues:
        en, zh = split_bilingual_text(cue.get('text', ''))
        if not zh:
            continue
        total += 1
        win = float(cue.get('end', 0)) - float(cue.get('start', 0))
        if cue.get('merged'):
            continue
        if is_long_cue(en, zh, win, ratio, prep):
            long_n += 1
    return total, long_n


def assess_gemini_translate_strategy(
    cues: list,
    ratio: float,
    *,
    only_problem_cues: bool,
    cfg: dict | None = None,
) -> dict:
    """
    按「超长句」占比决定送优策略：
    - 无原语言原文 → 不调用 Gemini（避免把中文字幕发回去照抄浪费 token）
    - long_ratio > gemini_source_if_long_ratio → 送优组从原语言翻译（仅问题句，不含中文）
    - long_ratio >= gemini_source_universal_long_ratio → 全部从原语言一次翻译（不含中文）
    - 否则 → 仅问题句，从原语言重新译/压缩（不含中文）
    """
    prep = cfg or dub_prep_config()
    total_zh, with_source, long_count = count_source_cues(cues)
    source_ratio = with_source / max(total_zh, 1)
    long_ratio = long_count / max(total_zh, 1)
    long_th = float(prep.get('gemini_source_if_long_ratio', 1.0 / 3.0))
    universal_th = float(prep.get('gemini_source_universal_long_ratio', 2.0 / 3.0))
    min_source_ratio = float(prep.get('gemini_min_source_ratio', 0.5))

    if total_zh <= 0:
        return {
            'translate_mode': 'skip',
            'force_source_all': False,
            'total_zh': 0,
            'with_source': 0,
            'source_ratio': 0.0,
            'long_count': 0,
            'long_ratio': 0.0,
            'long_threshold': long_th,
            'universal_threshold': universal_th,
            'reason': '字幕无中文组，跳过 Gemini',
        }

    if with_source <= 0 or source_ratio < min_source_ratio:
        return {
            'translate_mode': 'skip',
            'force_source_all': False,
            'total_zh': total_zh,
            'with_source': with_source,
            'source_ratio': round(source_ratio, 4),
            'long_count': long_count,
            'long_ratio': round(long_ratio, 4),
            'long_threshold': long_th,
            'universal_threshold': universal_th,
            'reason': (
                f'仅中文字幕（{with_source}/{total_zh} 条有原语言原文），'
                f'不送 Gemini，避免照抄现有中文浪费 token'),
        }

    force_source_all = (
        only_problem_cues
        and long_ratio >= universal_th
    )
    use_source = force_source_all or (
        only_problem_cues and long_ratio > long_th
    )
    translate_mode = (
        'source_translate' if use_source else 'en_rewrite')

    if force_source_all:
        reason = (
            f'超长句 {long_count}/{total_zh}（{long_ratio * 100:.0f}%）'
            f'≥ {universal_th * 100:.0f}%：'
            f'全部 {with_source} 条从原语言一次翻译（API 不含现有中文）')
    elif use_source:
        reason = (
            f'超长句 {long_count}/{total_zh}（{long_ratio * 100:.0f}%）'
            f'> {long_th * 100:.0f}%：'
            f'仅问题句从原语言翻译（API 不含现有中文）')
    else:
        reason = (
            f'超长句 {long_ratio * 100:.0f}% ≤ {long_th * 100:.0f}%：'
            f'仅问题句从原语言重新译/压缩（API 不含现有中文）')

    return {
        'translate_mode': translate_mode,
        'force_source_all': force_source_all,
        'total_zh': total_zh,
        'with_source': with_source,
        'source_ratio': round(source_ratio, 4),
        'long_count': long_count,
        'long_ratio': round(long_ratio, 4),
        'long_threshold': long_th,
        'universal_threshold': universal_th,
        'reason': reason,
    }


def en_char_count(en: str) -> int:
    """英文字母与数字个数（不含空格标点）。"""
    return len(re.sub(r'[^A-Za-z0-9]', '', en))


def zh_char_count(zh: str) -> int:
    return len(re.sub(r'\s+', '', zh))


def zh_max_from_en(en_chars: int, ratio: float = 1.0, floor: int = 4) -> int:
    """按英文体量估算中文汉字上限。"""
    return max(floor, int(en_chars * ratio))


def max_zh_chars_for_window(
    window_sec: float,
    *,
    chars_per_sec: float = 4.0,
    max_speed: float = 1.5,
) -> int:
    if window_sec <= 0:
        return 12
    return max(4, int(window_sec * chars_per_sec * max_speed))


def parse_rate_pct(rate: str) -> int:
    try:
        return int(str(rate).replace('%', '').replace('+', '').strip())
    except ValueError:
        return 0


def format_rate_pct(pct: int) -> str:
    return f'+{pct}%' if pct >= 0 else f'{pct}%'


def cue_speech_params(
    zh: str,
    window_sec: float,
    base_rate: str,
    *,
    chars_per_sec: float = 4.0,
    max_speed: float = 1.5,
    max_rate_pct: int = 35,
) -> tuple[str, float, int]:
    base_pct = parse_rate_pct(base_rate)
    max_c = max_zh_chars_for_window(
        window_sec, chars_per_sec=chars_per_sec, max_speed=max_speed)
    n = zh_char_count(zh)
    if n <= 0:
        return format_rate_pct(base_pct), 1.0 + base_pct / 100.0, 0

    density = n / max(max_c, 1)
    extra = 0
    if density > 1.0:
        extra = min(max(0, max_rate_pct - base_pct), int((density - 1.0) * 50))
    elif density > 0.85:
        extra = int((density - 0.85) / 0.15 * 10)

    total_pct = min(max_rate_pct, base_pct + extra)
    return (
        format_rate_pct(total_pct),
        1.0 + total_pct / 100.0,
        total_pct - base_pct,
    )


def _get_gemini_client(api_key: str, timeout: float = 120) -> genai.Client:
    key = (api_key or '').strip()
    cache_key = f'{key}:{int(timeout)}'
    if cache_key not in _client_cache:
        _client_cache[cache_key] = genai.Client(
            api_key=key,
            http_options={'timeout': int(timeout * 1000)},
        )
    return _client_cache[cache_key]


def _parse_json_array(text: str) -> list:
    text = text.strip()
    m = re.search(r'\[[\s\S]*\]', text)
    if not m:
        return []
    return json.loads(m.group())


def _gemini_generate(prompt: str, api_key: str, model: str, timeout: float = 120) -> str:
    key = (api_key or '').strip()
    if not key:
        raise RuntimeError('Gemini API Key 为空')
    try:
        client = _get_gemini_client(key, timeout)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config={
                'temperature': 0.15,
                'max_output_tokens': 8192,
            },
        )
        text = (response.text or '').strip()
        if not text:
            raise RuntimeError('Gemini 返回内容为空')
        return text
    except genai_errors.APIError as e:
        raise RuntimeError(str(e)) from e
    except Exception as e:
        raise RuntimeError(str(e)) from e


def normalize_gemini_model(name: str) -> str:
    """返回 trim 后的模型名；空则用默认。"""
    n = (name or '').strip()
    return n or DEFAULT_GEMINI_MODEL


def gemini_model_combobox_values() -> list[str]:
    """配置弹窗下拉：与 AI Studio 相同的显示名。"""
    return [label for _, label in GEMINI_MODEL_CATALOG]


def gemini_model_label(model_id: str) -> str:
    mid = normalize_gemini_model(model_id)
    return GEMINI_MODEL_LABELS.get(mid, mid)


def resolve_gemini_model_from_ui(value: str) -> str:
    """从下拉显示名或 API id 解析为 model id。"""
    v = (value or '').strip()
    if v in GEMINI_MODEL_IDS:
        return GEMINI_MODEL_IDS[v]
    if v in GEMINI_MODEL_LABELS:
        return v
    return normalize_gemini_model(v)


def gemini_combobox_index_for_id(model_id: str) -> int:
    label = gemini_model_label(model_id)
    values = gemini_model_combobox_values()
    if label in values:
        return values.index(label)
    return 0


def test_gemini_api(api_key: str, model: str) -> tuple[bool, str]:
    """仅验证用户配置的模型，不自动换模型、不写 secrets。"""
    key = (api_key or '').strip()
    if not key:
        return False, 'API Key 为空'
    requested = normalize_gemini_model(model or DEFAULT_GEMINI_MODEL)
    try:
        out = _gemini_generate('只回复一个词：OK', key, requested, timeout=45)
        if out:
            return True, ''
        return False, 'Gemini 返回内容为空'
    except Exception as e:
        return False, str(e)


def _gemini_audit_payload(
    payload: list[dict], mode: str, cfg: dict | None,
) -> None:
    """送 API 前自检；失败则抛错，避免 token 浪费。"""
    if not (cfg or {}).get('gemini_preflight', True):
        return
    from gemini_preflight import audit_api_payload
    block = bool((cfg or {}).get('gemini_preflight_block', True))
    ok, errs = audit_api_payload(payload, mode, block_on_error=block)
    if not ok and errs:
        raise ValueError(
            'Gemini 送优自检未通过（已拦截，避免浪费 token）：'
            + '；'.join(errs[:5])
            + (f'…共 {len(errs)} 项' if len(errs) > 5 else ''))


def _rewrite_batch_sync(
    batch: list[dict],
    api_key: str,
    model: str,
    ratio: float,
    cfg: dict | None = None,
    log_session=None,
) -> dict[int, str]:
    cfg = cfg or dub_prep_config()
    has_merge = any(item.get('kind') == 'merge' for item in batch)
    has_suspicious = any(item.get('kind') == 'suspicious' for item in batch)
    payload = []
    for item in batch:
        window = item.get('window_sec', 0)
        if item.get('kind') == 'merge' and item.get('segments'):
            segs = item['segments']
            en = ' '.join(s['en'] for s in segs if s['en']).strip()
            ec = en_char_count(en)
            zmax = cue_zh_max(en, window, ratio, cfg)
            payload.append({
                'index': item['index'],
                'kind': 'merge',
                'segments': _source_segments_payload(segs),
                'en_chars': ec,
                'zh_max': zmax,
            })
        else:
            en = item['en']
            ec = en_char_count(en)
            zmax = cue_zh_max(en, window, ratio, cfg)
            payload.append({
                'index': item['index'],
                'kind': item.get('kind', 'long'),
                'en': en,
                'en_chars': ec,
                'zh_max': zmax,
            })

    instr = GEMINI_REWRITE_INSTRUCTION
    if has_merge:
        instr += GEMINI_MERGE_NOTE
    if has_suspicious:
        instr += GEMINI_SUSPICIOUS_NOTE
    user_msg = instr + '\n\n输入：\n' + json.dumps(
        payload, ensure_ascii=False, indent=2)

    _gemini_audit_payload(payload, 'en_rewrite', cfg)

    t0 = time.perf_counter()
    raw = ''
    try:
        raw = _gemini_generate(user_msg, api_key, model)
    except Exception as e:
        if log_session is not None:
            log_session.record_batch_error(
                items=batch,
                error=str(e),
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        raise

    rows = _parse_json_array(raw)
    out: dict[int, str] = {}
    for row in rows:
        try:
            idx = int(row['index'])
            zh_out = str(row.get('zh', '')).strip()
            zh_out = re.sub(r'\s+', '', zh_out)
            if zh_out:
                out[idx] = zh_out
        except (KeyError, TypeError, ValueError):
            continue

    if log_session is not None:
        log_session.record_batch(
            items=batch,
            instruction=instr,
            payload=payload,
            user_message=user_msg,
            raw_response=raw,
            mapping=out,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )
    return out


def _source_segments_payload(segments: list) -> list[dict]:
    """原文翻译 API 载荷：segments 只送 en 与时间，不送中文。"""
    return [
        {
            'start': s.get('start'),
            'end': s.get('end'),
            'en': s.get('en', ''),
        }
        for s in segments
    ]


def _source_translate_batch_sync(
    batch: list[dict],
    api_key: str,
    model: str,
    ratio: float,
    cfg: dict | None = None,
    log_session=None,
) -> dict[int, str]:
    """从原文（英/俄等）译成配音中文，非改现有中文。"""
    cfg = cfg or dub_prep_config()
    has_merge = any(item.get('kind') == 'merge' for item in batch)
    payload = []
    for j, item in enumerate(batch):
        window = item.get('window_sec', 0)
        prev_src = (batch[j - 1].get('en') or '')[:100] if j > 0 else ''
        next_src = (batch[j + 1].get('en') or '')[:100] if j + 1 < len(batch) else ''
        if item.get('kind') == 'merge' and item.get('segments'):
            segs = item['segments']
            source = ' '.join(s['en'] for s in segs if s['en']).strip()
        else:
            source = item.get('en', '')
        zmax = cue_zh_max(source, window, ratio, cfg)
        row: dict = {
            'index': item['index'],
            'kind': item.get('kind', 'long'),
            'source_lang': detect_source_lang(source),
            'source': source,
            'window_sec': round(float(window), 2),
            'zh_max': zmax,
            'context_prev': prev_src,
            'context_next': next_src,
        }
        if item.get('kind') == 'merge' and item.get('segments'):
            row['segments'] = _source_segments_payload(item['segments'])
        payload.append(row)

    instr = GEMINI_SOURCE_TRANSLATE_INSTRUCTION
    if has_merge:
        instr += GEMINI_SOURCE_MERGE_NOTE
    user_msg = instr + '\n\n输入：\n' + json.dumps(
        payload, ensure_ascii=False, indent=2)

    _gemini_audit_payload(payload, 'source_translate', cfg)

    t0 = time.perf_counter()
    raw = ''
    try:
        raw = _gemini_generate(user_msg, api_key, model)
    except Exception as e:
        if log_session is not None:
            log_session.record_batch_error(
                items=batch,
                error=str(e),
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        raise

    rows = _parse_json_array(raw)
    out: dict[int, str] = {}
    for row in rows:
        try:
            idx = int(row['index'])
            zh_out = str(row.get('zh', '')).strip()
            zh_out = re.sub(r'\s+', '', zh_out)
            if zh_out:
                out[idx] = zh_out
        except (KeyError, TypeError, ValueError):
            continue

    if log_session is not None:
        log_session.record_batch(
            items=batch,
            instruction=instr,
            payload=payload,
            user_message=user_msg,
            raw_response=raw,
            mapping=out,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )
    return out


def _gemini_batch_sync(
    batch: list[dict],
    api_key: str,
    model: str,
    ratio: float,
    *,
    mode: str = 'en_rewrite',
    cfg: dict | None = None,
    log_session=None,
) -> dict[int, str]:
    if mode == 'source_translate':
        return _source_translate_batch_sync(
            batch, api_key, model, ratio, cfg, log_session)
    return _rewrite_batch_sync(
        batch, api_key, model, ratio, cfg, log_session)


def _rule_shorten(zh: str, max_chars: int) -> str:
    zh = re.sub(r'\s+', '', zh.strip())
    if len(zh) <= max_chars:
        return zh
    for sep in ('，', '。', '；', '、', ',', '.', ';'):
        parts = zh.split(sep)
        buf = ''
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if len(buf) + len(p) + (1 if buf else 0) <= max_chars:
                buf = buf + sep + p if buf else p
            else:
                break
        if buf and len(buf) <= max_chars:
            return buf
    return zh[:max_chars]


async def ensure_gemini_api(
    api_key: str,
    model: str,
    *,
    interactive: bool = True,
    error_hint: str = '',
    prompt_if_missing_key: bool = True,
) -> tuple[str, str] | None:
    """
    验证 Gemini API。
    - Key 为空且 prompt_if_missing_key：弹窗让用户填写（仅此时弹窗）。
    - Key 已存在但验证失败：终端报错，不弹窗（请用 配置.bat 修改）。
    返回 (api_key, configured_model) 或 None。
    """
    key = (api_key or '').strip()
    if not key:
        from gemini_secrets import gemini_api_key_from_env
        key = gemini_api_key_from_env()
    requested = normalize_gemini_model(model or DEFAULT_GEMINI_MODEL)

    if not key:
        if not interactive or not prompt_if_missing_key:
            print('  [i] 未配置 Gemini API Key（环境变量 GEMINI_API_KEY），跳过字幕改写')
            return None
        print(
            '\n  未检测到 GEMINI_API_KEY，请设置 Windows 用户环境变量'
            '（见 components\\GEMINI_API_KEY_SETUP.txt），然后运行 config.bat 选择模型')
        data = ask_api_keys_dialog(load_secrets())
        if data is None:
            return None
        key = (data.get('gemini_api_key') or '').strip()
        if not key:
            from gemini_secrets import gemini_api_key_from_env
            key = gemini_api_key_from_env()
        if not key:
            return None
        requested = normalize_gemini_model(
            data.get('gemini_model') or requested)

    ok, err_msg = test_gemini_api(key, requested)
    if ok:
        print(f'  [Gemini] 使用配置模型：{requested}（Key 来自环境变量）')
        return key, requested

    err = error_hint or err_msg
    print(f'\n  [错误] Gemini API 不可用：{err[:240]}')
    if 'location is not supported' in err.lower():
        print('  [说明] 这是 Google 地区限制，换模型通常无效；需可用地区的网络/代理，'
              '或使用该地区申请的 API Key')
    print('  请检查 GEMINI_API_KEY 与 config.bat 中的模型设置')
    return None


async def rewrite_cues_with_gemini(
    cues: list,
    *,
    api_key: str,
    model: str = DEFAULT_GEMINI_MODEL,
    zh_en_ratio: float = 1.0,
    batch_size: int = 20,
    on_batch_error=None,
    prep_cfg: dict | None = None,
    only_problem_cues: bool = True,
    log_session=None,
    subtitle_path: str | None = None,
) -> tuple[list, dict]:
    """
    仅对长句 / 误译嫌疑 / 合并短句调用 Gemini；正常句跳过以省 token。
    返回 (更新后的 cues, 统计)。
    """
    if not api_key:
        return cues, {'skipped': True}

    cfg = prep_cfg or dub_prep_config()
    cfg['only_problem_cues'] = only_problem_cues

    strategy = assess_gemini_translate_strategy(
        cues, zh_en_ratio,
        only_problem_cues=only_problem_cues, cfg=cfg,
    )
    if strategy['translate_mode'] == 'skip':
        print(f'  Gemini 跳过：{strategy["reason"]}')
        return cues, {
            'total': strategy.get('total_zh', 0),
            'sent': 0,
            'changed': 0,
            'skip_reason': strategy['reason'],
            'translate_mode': 'skip',
        }

    translate_mode = strategy['translate_mode']
    force_source_all = strategy['force_source_all']

    items = []
    total_zh = 0
    skipped_no_source = 0
    for i, cue in enumerate(cues):
        en_line, zh = split_bilingual_text(cue['text'])
        source = cue_source_text(cue)
        if not zh:
            continue
        total_zh += 1
        if not source.strip():
            skipped_no_source += 1
            if log_session is not None:
                log_session.record_skipped(
                    i, reason='no_foreign_source',
                    en=en_line, zh=zh,
                    start=cue['start'], end=cue['end'],
                )
            continue
        if force_source_all:
            kind = 'source_all'
        elif only_problem_cues:
            kind = gemini_target_kind(cue, zh_en_ratio, cfg)
            if not kind:
                if log_session is not None:
                    log_session.record_skipped(
                        i, reason='normal_or_filtered',
                        en=source, zh=zh,
                        start=cue['start'], end=cue['end'],
                        kind=classify_cue(cue, zh_en_ratio, cfg),
                    )
                continue
        else:
            kind = classify_cue(cue, zh_en_ratio, cfg)
            if kind in ('ok', 'skip'):
                if log_session is not None:
                    log_session.record_skipped(
                        i, reason='ok_or_skip', en=source, zh=zh,
                        start=cue['start'], end=cue['end'], kind=kind,
                    )
                continue
        window = cue['end'] - cue['start']
        item = {
            'index': i,
            'kind': kind,
            'en': source,
            'zh': zh,
            'window_sec': window,
            'start': cue['start'],
            'end': cue['end'],
        }
        if kind == 'merge' and cue.get('segments'):
            item['segments'] = cue['segments']
        items.append(item)

    if skipped_no_source:
        print(
            f'  Gemini：{skipped_no_source} 条无原语言原文已跳过'
            f'（不送现有中文）')

    if not items:
        if skipped_no_source >= total_zh and total_zh:
            print(f'  Gemini 无需处理：{total_zh} 条均无原语言原文')
        else:
            print(f'  Gemini 无需处理（{total_zh} 条均属正常句）')
        return cues, {
            'total': total_zh,
            'sent': 0,
            'changed': 0,
            'skipped_no_source': skipped_no_source,
        }

    if cfg.get('gemini_preflight', True):
        from gemini_preflight import (
            preflight_rewrite_plan,
            print_preflight_report,
            save_preflight_report,
        )
        pf = preflight_rewrite_plan(
            items, strategy,
            subtitle_path=subtitle_path,
            cfg=cfg,
            only_problem_cues=only_problem_cues,
        )
        print_preflight_report(pf)
        if subtitle_path:
            save_preflight_report(pf, video_or_sub=subtitle_path)
        if pf.get('blocked'):
            err = '; '.join((pf.get('errors') or ['自检拦截'])[:3])
            print(f'  Gemini 已取消 API 调用：{err}')
            return cues, {
                'total': total_zh,
                'sent': 0,
                'changed': 0,
                'preflight': pf,
                'skip_reason': err,
            }
        items = pf.get('_items', items)

    skip_count = total_zh - len(items)

    loop = asyncio.get_event_loop()
    changed = 0
    sent = len(items)
    api_key = (api_key or '').strip()
    auth_retried = False
    compare_rows: list[dict] = []

    if force_source_all:
        print(f'  {strategy["reason"]}')
        print(f'  Gemini 待处理 {sent}/{total_zh} 条（全部从原文翻译）')
    elif translate_mode == 'source_translate':
        print(f'  {strategy["reason"]}')
        print(
            f'  Gemini 待处理 {sent}/{total_zh} 条'
            f'（送优组从原文翻译，跳过 {skip_count} 条正常句）')
    else:
        print(
            f'  Gemini 待处理 {sent}/{total_zh} 条'
            f'（{"仅问题句" if only_problem_cues else "全部"}，'
            f'仅送原语言原文，不含现有中文）')

    def _batch_mode(_batch: list[dict]) -> str:
        if translate_mode == 'source_translate':
            return 'source_translate'
        return 'en_rewrite'

    use_source_single = translate_mode == 'source_translate'
    effective_batch_size = len(items) if use_source_single else batch_size

    b = 0
    while b < len(items):
        batch = items[b:b + effective_batch_size]
        mode = _batch_mode(batch)
        sub_batches = [(batch, mode)]

        batch_mapping: dict[int, str] = {}
        for sub, sub_mode in sub_batches:
            if not sub:
                continue
            label = '原语言翻译' if sub_mode == 'source_translate' else '原语言译/压缩'
            if use_source_single and sub_mode == 'source_translate':
                print(f'  Gemini {label} 一次发送 {len(sub)}/{sent} 组原文…')
            else:
                print(
                    f'  Gemini {label} {b + 1}-{b + len(batch)}/{sent}'
                    f'（本批 {len(sub)} 条）…')
            try:
                mapping = await loop.run_in_executor(
                    None,
                    partial(
                        _gemini_batch_sync,
                        sub, api_key, model, zh_en_ratio,
                        mode=sub_mode, cfg=cfg, log_session=log_session),
                )
            except Exception as e:
                err = str(e)
                print(f'  [警告] Gemini 批次失败：{err[:300]}')
                if on_batch_error and not auth_retried and (
                        '400' in err or '401' in err or '403' in err
                        or 'API key' in err or 'API_KEY' in err):
                    auth_retried = True
                    fixed = await on_batch_error(err)
                    if fixed:
                        api_key, model = fixed
                        if log_session is not None:
                            log_session.model = model
                        continue
                break
            batch_mapping.update(mapping)

        for item in batch:
            i = item['index']
            new_zh = batch_mapping.get(i)
            if not new_zh:
                continue
            en, old_zh = split_bilingual_text(cues[i]['text'])
            if new_zh != old_zh:
                changed += 1
            compare_rows.append({
                'index': i,
                'cue_no': i + 1,
                'start': float(item.get('start', cues[i]['start'])),
                'end': float(item.get('end', cues[i]['end'])),
                'en': item.get('en') or en,
                'zh_before': old_zh,
                'zh_after': new_zh,
                'changed': new_zh != old_zh,
                'kind': item.get('kind', ''),
            })
            cues[i]['text'] = f'{en}\n{new_zh}' if en else new_zh
            cues[i]['zh_dub'] = new_zh

        b += effective_batch_size

    stats = {
        'total': total_zh,
        'sent': sent,
        'skip_count': skip_count,
        'long_count': strategy['long_count'],
        'long_ratio': strategy['long_ratio'],
        'force_source_all': force_source_all,
        'translate_mode': translate_mode,
        'strategy_reason': strategy['reason'],
        'changed': changed,
        'ratio': zh_en_ratio,
        'model': effective_gemini_model(model, cfg),
        'single_request': use_source_single,
        'compare_rows': compare_rows,
    }
    mode_label = '原语言翻译' if translate_mode == 'source_translate' else '原语言译/压缩'
    skip_note = (
        f'（全部 {sent} 组）'
        if force_source_all
        else f'（跳过 {skip_count} 条正常句）'
    )
    print(
        f'  Gemini {mode_label}完成：{changed}/{sent} 条有调整{skip_note}')
    return cues, stats


def _speech_align_batch_sync(
    batch: list[dict],
    api_key: str,
    model: str,
    glossary_text: str = '',
    log_session=None,
) -> dict[int, str]:
    payload = []
    for item in batch:
        wp = item.get('window_params') or {}
        payload.append({
            'index': item['index'],
            'en': item.get('en', ''),
            'duration': wp.get('duration', item.get('window_sec', 0)),
            'target_chars': wp.get('target_chars', 12),
            'max_chars': wp.get('max_chars', 16),
        })
    instr = GEMINI_SPEECH_ALIGN_INSTRUCTION
    if glossary_text:
        instr += '\n\n' + glossary_text
    user_msg = instr + '\n\n输入：\n' + json.dumps(
        payload, ensure_ascii=False, indent=2)

    _gemini_audit_payload(payload, 'speech_align', {'gemini_preflight': True})

    t0 = time.perf_counter()
    try:
        raw = _gemini_generate(user_msg, api_key, model)
    except Exception as e:
        if log_session is not None:
            log_session.record_batch_error(
                items=batch,
                error=str(e),
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        raise

    rows = _parse_json_array(raw)
    out: dict[int, str] = {}
    for row in rows:
        try:
            idx = int(row['index'])
            zh_out = str(row.get('zh', '')).strip()
            zh_out = re.sub(r'\s+', '', zh_out)
            max_c = next(
                (p['max_chars'] for p in payload if p['index'] == idx), 999)
            if zh_out and len(zh_out) <= max_c:
                out[idx] = zh_out
            elif zh_out:
                out[idx] = zh_out[:max_c]
        except (KeyError, TypeError, ValueError):
            continue

    if log_session is not None:
        log_session.record_batch(
            items=batch,
            instruction=instr,
            payload=payload,
            user_message=user_msg,
            raw_response=raw,
            mapping=out,
            duration_ms=(time.perf_counter() - t0) * 1000,
            glossary_included=bool(glossary_text),
        )
    return out


async def rewrite_cues_speech_aligned(
    cues: list,
    *,
    api_key: str,
    model: str = DEFAULT_GEMINI_MODEL,
    batch_size: int = 20,
    on_batch_error=None,
    glossary_text: str = '',
    log_session=None,
    only_problem_cues: bool = False,
    prep_cfg: dict | None = None,
    zh_en_ratio: float = 1.0,
) -> tuple[list, dict]:
    """
    Speech Activity 口播优化：带 duration / target_chars / max_chars 窗口约束。
    only_problem_cues=True 时仅送超长/误译/合并句（与 rewrite 模式相同筛选）。
    """
    if not api_key:
        return cues, {'skipped': True}

    cfg = prep_cfg or dub_prep_config()
    cfg['only_problem_cues'] = only_problem_cues
    total_zh = 0
    items = []
    for i, cue in enumerate(cues):
        en_line, zh = split_bilingual_text(cue.get('text', ''))
        source = cue_source_text(cue)
        if not zh:
            continue
        total_zh += 1
        if not source.strip():
            if log_session is not None:
                log_session.record_skipped(
                    i, reason='no_foreign_source',
                    en=en_line, zh=zh,
                    start=cue['start'], end=cue['end'],
                )
            continue
        if only_problem_cues:
            kind = gemini_target_kind(cue, zh_en_ratio, cfg)
            if not kind:
                if log_session is not None:
                    log_session.record_skipped(
                        i, reason='normal_or_filtered',
                        en=source, zh=zh,
                        start=cue['start'], end=cue['end'],
                        kind=classify_cue(cue, zh_en_ratio, cfg),
                    )
                continue
        wp = cue.get('window_params') or {}
        if not wp:
            win = cue['end'] - cue['start']
            from speech_activity import compute_window_params
            wp = compute_window_params(win)
        items.append({
            'index': i,
            'en': source,
            'zh': zh,
            'kind': 'speech_align',
            'window_params': wp,
            'window_sec': wp.get('duration', cue['end'] - cue['start']),
            'start': cue['start'],
            'end': cue['end'],
        })

    if not items:
        print(
            f'  Gemini 无需处理（{total_zh} 条均属正常句，模式='
            f'{"仅问题句" if only_problem_cues else "全部"}）',
            flush=True)
        return cues, {'total': total_zh, 'sent': 0, 'changed': 0, 'only_problem_cues': only_problem_cues}

    loop = asyncio.get_event_loop()
    changed = 0
    sent = len(items)
    api_key = (api_key or '').strip()
    auth_retried = False

    scope = '仅问题句' if only_problem_cues else '全部中文组'
    print(
        f'  Gemini 口播优化（Speech Activity，{scope}）'
        f'{sent}/{total_zh} 条，模型 {model}…',
        flush=True)

    b = 0
    while b < len(items):
        batch = items[b:b + batch_size]
        print(f'  Gemini 口播 {b + 1}-{b + len(batch)}/{sent}...', flush=True)
        try:
            mapping = await loop.run_in_executor(
                None,
                partial(
                    _speech_align_batch_sync,
                    batch, api_key, model, glossary_text, log_session),
            )
        except Exception as e:
            err = str(e)
            print(f'  [警告] Gemini 口播批次失败：{err[:300]}')
            if on_batch_error and not auth_retried and (
                    '400' in err or '401' in err or '403' in err
                    or 'API key' in err or 'API_KEY' in err):
                auth_retried = True
                fixed = await on_batch_error(err)
                if fixed:
                    api_key, model = fixed
                    if log_session is not None:
                        log_session.model = model
                    continue
            b += batch_size
            continue

        for item in batch:
            i = item['index']
            new_zh = mapping.get(i)
            if not new_zh:
                continue
            en, old_zh = split_bilingual_text(cues[i]['text'])
            if new_zh != old_zh:
                changed += 1
            cues[i]['text'] = f'{en}\n{new_zh}' if en else new_zh
            cues[i]['zh_dub'] = new_zh
            cues[i]['speech_aligned'] = True

        b += batch_size

    print(f'  Gemini 口播优化完成：{changed}/{sent} 条有调整')
    return cues, {
        'sent': sent, 'changed': changed,
        'model': effective_gemini_model(model, cfg),
        'total': total_zh, 'only_problem_cues': only_problem_cues,
    }


def resolve_gemini_model(secrets: dict | None = None, cfg: dict | None = None) -> str:
    """secrets 优先，其次配置文件；严格用用户所选，不自动换模型。"""
    sec = secrets or load_secrets()
    if sec.get('gemini_model'):
        return normalize_gemini_model(str(sec['gemini_model']))
    if cfg and cfg.get('ai_shorten_model_gemini'):
        return normalize_gemini_model(str(cfg['ai_shorten_model_gemini']))
    return DEFAULT_GEMINI_MODEL


def effective_gemini_model(model: str | None = None, cfg: dict | None = None) -> str:
    """报告/日志统一用：显式 model 优先，否则读 secrets/config。"""
    if model and str(model).strip():
        return normalize_gemini_model(str(model))
    return resolve_gemini_model(cfg=cfg)


def ask_api_keys_dialog(
    existing: dict | None = None,
    error_hint: str = '',
    default_model: str | None = None,
) -> dict | None:
    """弹窗选择 Gemini 模型（API Key 仅从环境变量读取）。"""
    existing = existing or load_secrets()
    from gemini_secrets import gemini_api_key_from_env, gemini_api_key_status
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except ImportError:
        return ask_api_keys_console(existing, default_model=default_model)

    result = {'value': None}
    root = tk.Tk()
    root.title('Gemini API 配置')
    root.geometry('580x360')
    root.lift()
    root.attributes('-topmost', True)
    root.after(300, lambda: root.attributes('-topmost', False))
    root.focus_force()
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f'580x360+{(sw - 580) // 2}+{(sh - 360) // 2}')

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill='both', expand=True)
    if error_hint:
        ttk.Label(
            frm, text=f'上次错误：{error_hint[:200]}',
            foreground='red', wraplength=540,
        ).pack(anchor='w', pady=(0, 6))
    ttk.Label(
        frm,
        text='Speech Activity / 字幕改写需 Gemini API Key。\n'
             'Key 仅从 Windows 系统/用户环境变量读取，不写入任何项目文件。\n'
             '变量名：GEMINI_API_KEY（设置步骤见 GEMINI_API_KEY_SETUP.txt）\n'
             '推荐字幕模型：Gemini 2.5 Flash Lite / Gemini 2.5 Flash',
        wraplength=540,
    ).pack(anchor='w', pady=(0, 10))

    key_status = gemini_api_key_status()
    key_fg = 'green' if gemini_api_key_from_env() else 'red'
    ttk.Label(
        frm, text=f'API Key：{key_status}',
        foreground=key_fg, wraplength=540,
    ).pack(anchor='w', pady=(0, 8))

    ttk.Label(frm, text='Gemini 模型（必选）').pack(anchor='w', pady=(4, 0))
    cur_id = normalize_gemini_model(
        default_model or existing.get('gemini_model') or DEFAULT_GEMINI_MODEL)
    combo_values = gemini_model_combobox_values()
    model_var = tk.StringVar(value=gemini_model_label(cur_id))
    model_box = ttk.Combobox(
        frm, textvariable=model_var, width=44,
        values=combo_values, state='readonly')
    model_box.pack(anchor='w', fill='x')
    model_box.current(gemini_combobox_index_for_id(cur_id))

    def on_ok():
        model = resolve_gemini_model_from_ui(model_var.get())
        key = gemini_api_key_from_env()
        if not key:
            messagebox.showwarning(
                '提示',
                '未检测到 GEMINI_API_KEY。\n'
                '请在 Windows 用户环境变量中配置（见 components\\GEMINI_API_KEY_SETUP.txt），'
                '然后重新打开本窗口。',
            )
            return
        if model not in GEMINI_MODEL_CHOICES and model not in GEMINI_SUBTITLE_MODEL_IDS:
            messagebox.showwarning('提示', '请从下拉列表选择 Gemini 模型')
            return
        if model not in GEMINI_SUBTITLE_MODEL_IDS:
            if not messagebox.askyesno(
                '提示',
                f'「{model_var.get()}」为 TTS/音频模型，不适合字幕改写。\n'
                '仍要保存吗？',
            ):
                return
        data = dict(existing)
        data['gemini_model'] = model
        save_secrets(data)
        data['gemini_api_key'] = key
        result['value'] = data
        root.destroy()

    def on_skip():
        result['value'] = {}
        root.destroy()

    bf = ttk.Frame(frm)
    bf.pack(fill='x', pady=(12, 0))
    ttk.Button(bf, text='保存', command=on_ok).pack(side='left', padx=4)
    ttk.Button(bf, text='跳过', command=on_skip).pack(side='left', padx=4)
    ttk.Label(bf, text='→ 模型存 tubeget.secrets.json；Key 仅环境变量', foreground='gray').pack(side='left', padx=8)
    root.mainloop()
    return result['value']


def ask_api_keys_console(
    existing: dict | None = None,
    default_model: str | None = None,
) -> dict:
    existing = existing or load_secrets()
    from gemini_secrets import gemini_api_key_from_env, gemini_api_key_status
    print('\n  === Gemini 配置（Key 仅环境变量）===')
    print(f'  {gemini_api_key_status()}')
    cur = default_model or existing.get('gemini_model') or DEFAULT_GEMINI_MODEL
    print('  可选模型：')
    for label in gemini_model_combobox_values():
        print(f'    - {label}')
    mdl_in = input(f'  Gemini 模型 [{gemini_model_label(cur)}]: ').strip()
    mdl = resolve_gemini_model_from_ui(mdl_in) if mdl_in else normalize_gemini_model(cur)
    data = dict(existing)
    if mdl:
        data['gemini_model'] = mdl
    key = gemini_api_key_from_env()
    if key:
        data['gemini_api_key'] = key
    if data.get('gemini_model'):
        save_secrets(data)
    return data if gm else {}


async def shorten_zh_gemini(
    text: str,
    max_chars: int,
    *,
    gemini_key: str = '',
    gemini_model: str = DEFAULT_GEMINI_MODEL,
    en_ref: str = '',
) -> tuple[str, str]:
    """单条兜底压缩：先本地策略，仍超长再 Gemini。"""
    from zh_compress import compress_zh_for_dub, load_strategy

    zh = re.sub(r'\s+', '', text.strip())
    if len(zh) <= max_chars:
        return zh, 'unchanged'

    strat = load_strategy()
    compressed, how = compress_zh_for_dub(zh, max_chars, strat)
    if len(compressed) <= max_chars:
        return compressed, how

    if not gemini_key:
        return compressed, how if how != 'unchanged' else 'rule'

    batch = [{'index': 0, 'en': en_ref or '（无）', 'zh': zh, 'window_sec': 0}]
    loop = asyncio.get_event_loop()
    try:
        m = await loop.run_in_executor(
            None, _rewrite_batch_sync, batch, gemini_key, gemini_model, 1.0)
        if 0 in m and m[0]:
            out = m[0][:max_chars]
            return out, 'gemini'
    except Exception:
        pass
    return compressed, how if how != 'unchanged' else 'rule'
