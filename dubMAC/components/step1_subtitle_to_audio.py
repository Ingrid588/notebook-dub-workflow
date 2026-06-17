#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
步骤一：字幕文件 → 配音音频
============================
把中文字幕（.srt / .vtt）按时间轴逐句生成语音，
最终合并成一个与原视频等长、时间轴对齐的 MP3 文件。

功能：
  - 自动过滤口水词（嗯、啊、呃 等）
  - 敏感词直接删除（不配音、不插入滴声）
  - 支持自定义口水词和敏感词列表

依赖：
    pip install edge-tts pydub
    pip install pyttsx3          # 可选：Edge 失败时用 Windows 本地语音补全

用法：
    python step1_subtitle_to_audio.py 字幕.srt
    python step1_subtitle_to_audio.py 字幕.srt --voice 云希
    python step1_subtitle_to_audio.py 字幕.srt --no-filter     # 关闭过滤
    python step1_subtitle_to_audio.py 字幕.srt --beep-words "妈的,滚蛋"
"""

import asyncio
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE_STEP1 = Path(__file__).parent.resolve()
if str(HERE_STEP1) not in sys.path:
    sys.path.insert(0, str(HERE_STEP1))

from dub_naming import dub_output_mp3_path, dub_stem, video_stem_from_subtitle
from edge_tts_scheduler import (
    load_edge_scheduler_config,
    print_edge_scheduler_config,
    run_edge_scheduler,
)

# pyttsx3 非线程安全，本地补全时串行调用
_local_tts_lock = threading.Lock()

def setup_console_encoding():
    """在 Windows 控制台下尽量避免 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

# ── 声音映射 ────────────────────────────────────────────────
VOICE_MAP = {
    '晓晓': 'zh-CN-XiaoxiaoNeural',
    '晓伊': 'zh-CN-XiaoyiNeural',
    '晓辰': 'zh-CN-XiaochenNeural',
    '晓萱': 'zh-CN-XiaoxuanNeural',
    '晓悠': 'zh-CN-XiaoyouNeural',
    '晓墨': 'zh-CN-XiaomoNeural',
    '晓梦': 'zh-CN-XiaomengNeural',
    '晓涵': 'zh-CN-XiaohanNeural',
    '晓秋': 'zh-CN-XiaoqiuNeural',
    '晓睿': 'zh-CN-XiaoruiNeural',
    '晓双': 'zh-CN-XiaoshuangNeural',
    '晓颜': 'zh-CN-XiaoyanNeural',
    '晓甄': 'zh-CN-XiaozhenNeural',
    '云希': 'zh-CN-YunxiNeural',
    '云扬': 'zh-CN-YunyangNeural',
    '云健': 'zh-CN-YunjianNeural',
    '云枫': 'zh-CN-YunfengNeural',
    '云皓': 'zh-CN-YunhaoNeural',
    '云夏': 'zh-CN-YunxiaNeural',
    '云泽': 'zh-CN-YunzeNeural',
}

# ── 口水词列表（整句都是口水词则跳过，出现在句中则删除）──
FILLER_WORDS = [
    # 语气词
    '嗯', '啊', '哦', '呃', '哈', '嘿', '哎', '唉', '哟', '哇',
    '嗯嗯', '啊啊', '哦哦', '嗯哦', '哦嗯',
    # 口头禅
    '那个', '就是', '然后呢', '然后就是', '对对对', '对吧', '对啊',
    '就是说', '也就是说', '怎么说呢', '怎么讲', '我觉得吧',
    '你知道吗', '你懂吗', '你明白吗', '是吧', '是不是', '对不对',
    '好吧', '好的好的', '嗯对', '嗯嗯对',
    # 英文口水词（翻译后可能残留）
    'um', 'uh', 'er', 'ah', 'hmm', 'like', 'you know', 'i mean',
    'so', 'well', 'right', 'okay',
]

# ── 敏感词列表（从文本中删除）────────────────────────────────
SENSITIVE_WORDS = [
    # 常见粗口（可自行扩展）
    '妈的', '操', '草', '他妈', '你妈', '狗日', '日你',
    '傻逼', '煞笔', '逼', '屌', '鸡巴', '狗屁',
    '滚蛋', '去死', '死去', '混蛋', '王八蛋', 'bastard',
    'fuck', 'shit', 'damn', 'hell', 'ass', 'bitch',
    'crap', 'cock', 'dick',
]

# ── 无上下文「音乐」标注（自动字幕常见，非口播内容）────────
_MUSIC_BRACKET_RE = re.compile(r'[【\[\(（]\s*音乐\s*[】\]\)）]')
_MUSIC_ORPHAN_RE = re.compile(r'(?<![\u4e00-\u9fff])音乐(?![\u4e00-\u9fff])')


def strip_contextless_music(text: str) -> str:
    """删除无上下文的「音乐」标注；保留「背景音乐」「音乐课」等复合词。"""
    if not text:
        return text
    cleaned_lines: list[str] = []
    for line in text.split('\n'):
        s = _MUSIC_BRACKET_RE.sub('', line)
        s = _MUSIC_ORPHAN_RE.sub('', s)
        s = re.sub(r'\s+', ' ', s).strip()
        s = re.sub(r'^[，,、；;：:。！？.!?\s]+', '', s)
        s = re.sub(r'[，,、\s]+$', '', s)
        if s:
            cleaned_lines.append(s)
    result = '\n'.join(cleaned_lines).strip()
    return re.sub(r'\n{2,}', '\n', result)


def filter_cues_contextless_music(cues: list) -> tuple[list, int]:
    """逐条删除无上下文「音乐」；整句仅剩标注则跳过该 cue。"""
    out: list = []
    skipped = 0
    for cue in cues:
        text = (cue.get('text') or '').strip()
        cleaned = strip_contextless_music(text)
        if not cleaned:
            skipped += 1
            continue
        out.append({**cue, 'text': cleaned} if cleaned != text else cue)
    return out, skipped


# ── 文本过滤 ────────────────────────────────────────────────

def normalize_gender_pronouns(text: str) -> str:
    """全局统一：他/她、她/他 → 他们（所有视频生效）。"""
    for old in ('他/她', '她/他', '他／她', '她／他'):
        text = text.replace(old, '他们')
    return text


def is_pure_filler(text: str, fillers: list) -> bool:
    """判断整句是否是口水词（跳过这句）"""
    t = text.strip().rstrip('。，！？.!?,、')
    return t.lower() in [f.lower() for f in fillers] or len(t) <= 1

def clean_text(text: str, fillers: list, sensitive: list) -> str:
    """
    清理文本：
    1. 删除句中的口水词短语（连同紧跟的标点一起删除）
    2. 敏感词直接删除（整句删空则跳过该条字幕）
    """
    result = text

    # 删除句中口水词（只删除2字以上的，避免误删正常字）
    # 连同口水词后面紧跟的逗号、顿号等标点一起删除
    for fw in sorted(fillers, key=len, reverse=True):
        if len(fw) >= 2:
            result = re.sub(re.escape(fw) + r'[，,、；;：:。！？.!?\s]*',
                            '', result, flags=re.IGNORECASE)

    # 敏感词直接删除
    for sw in sorted(sensitive, key=len, reverse=True):
        result = re.sub(
            re.escape(sw) + r'[，,、；;：:。！？.!?\s]*',
            '', result, flags=re.IGNORECASE)

    # 清理多余空格和标点
    result = re.sub(r'\s+', ' ', result).strip()
    result = re.sub(r'[，。！？、；,;.!?]{2,}', '。', result)
    # 去掉开头和结尾的孤立标点
    result = re.sub(r'^[，,、；;：:。！？.!?\s]+', '', result)
    result = re.sub(r'[，,、\s]+$', '', result)
    result = normalize_gender_pronouns(result)

    return result

def process_cue(cue: dict, fillers: list, sensitive: list,
                filter_on: bool) -> dict | None:
    """
    处理单条字幕：
    - 返回 None：跳过（整句是口水词）
    - 返回修改后的 cue
    """
    text = normalize_gender_pronouns(cue['text'].strip())
    cue = {**cue, 'text': text}

    text = cue['text'].strip()

    if filter_on:
        # 整句是口水词 → 跳过（静音）
        if is_pure_filler(text, fillers):
            return None
        cleaned = clean_text(text, fillers, sensitive)
    else:
        cleaned = text

    cleaned = strip_contextless_music(cleaned)

    # 清理后为空 → 跳过
    if not cleaned.strip():
        return None

    return {**cue, 'text': cleaned, 'original': text}

# ── 哔声生成 ────────────────────────────────────────────────

def make_beep(duration_ms: int = 500, freq: int = 1000) -> 'AudioSegment':
    """生成哔声（纯音调）"""
    try:
        import numpy as np
        from pydub import AudioSegment
        import struct

        sample_rate = 44100
        n_samples = int(sample_rate * duration_ms / 1000)
        t = [i / sample_rate for i in range(n_samples)]
        samples = [int(32767 * 0.5 * (1 if i > n_samples//10 and i < n_samples*9//10 else 0)
                       * __import__('math').sin(2 * __import__('math').pi * freq * ti))
                   for i, ti in enumerate(t)]
        raw = struct.pack(f'<{n_samples}h', *samples)
        return AudioSegment(raw, frame_rate=sample_rate, sample_width=2, channels=1)
    except ImportError:
        from pydub import AudioSegment
        return AudioSegment.silent(duration=duration_ms)  # 无 numpy 时用静音代替

# ── 字幕解析 ────────────────────────────────────────────────

def parse_time(t: str) -> float:
    t = t.strip().replace(',', '.')
    parts = t.split(':')
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(t)

_CUE_INDEX_RE = re.compile(r'^\d+\s*$')
_TIME_LINE_RE = re.compile(r'([\d:,\.]+)\s*-->\s*([\d:,\.]+)')


def _peek_next_cue(lines: list[str], pos: int) -> bool:
    """判断 pos 起（可含空行）是否为下一条字幕（序号 + 时间轴）。"""
    j = pos
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j >= len(lines) or not _CUE_INDEX_RE.match(lines[j].strip()):
        return False
    j += 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    return j < len(lines) and bool(_TIME_LINE_RE.search(lines[j]))


def parse_srt(text: str) -> list:
    """
    解析 SRT。支持块内空行，例如序号/时间轴/英文/中文之间各有空行。
    仅在「下一条序号+时间轴」出现处切开，不会因块内空行误拆。
    """
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    cues: list[dict] = []
    i = 0
    n = len(lines)

    while i < n:
        while i < n and not lines[i].strip():
            i += 1
        if i >= n:
            break

        if _CUE_INDEX_RE.match(lines[i].strip()):
            i += 1
            while i < n and not lines[i].strip():
                i += 1
        if i >= n:
            break

        m = _TIME_LINE_RE.search(lines[i])
        if not m:
            i += 1
            continue
        start = parse_time(m.group(1))
        end = parse_time(m.group(2))
        i += 1

        text_lines: list[str] = []
        while i < n:
            if not lines[i].strip():
                if _peek_next_cue(lines, i):
                    break
                i += 1
                continue
            if _peek_next_cue(lines, i):
                break
            text_lines.append(lines[i].strip())
            i += 1

        body = '\n'.join(text_lines).strip()
        body = re.sub(r'<[^>]+>', '', body).strip()
        if body:
            cues.append({'start': start, 'end': end, 'text': body})

    return cues

def parse_vtt(text: str) -> list:
    text = re.sub(r'^WEBVTT.*?\n\n', '', text, flags=re.DOTALL)
    return parse_srt(text)

def load_subtitle(path: str) -> list:
    """加载 .srt / .txt（SRT 正文）/ .vtt 字幕。"""
    with open(path, 'r', encoding='utf-8-sig') as f:
        text = f.read()
    ext = Path(path).suffix.lower()
    cues = parse_vtt(text) if ext == '.vtt' else parse_srt(text)
    print(f'✅ 解析字幕：{len(cues)} 条')
    return cues


def extract_chinese_from_bilingual(text: str) -> str:
    """从双语字幕块中提取中文行（与 run_batch_course 一致）。"""
    from cue_synth_eligibility import extract_chinese as _extract
    return _extract(text)


def is_bilingual_subtitle_path(path: str | Path) -> bool:
    """双语/合并字幕（需只取中文行配音，避免 EN 分片被念出）。"""
    n = Path(path).name.lower()
    if 'bilingual' in n or 'fit-dub' in n:
        return True
    if 'en-zh-merge' in n or 'zh-en-merge' in n or '.merge.' in n:
        return True
    return False


def _needs_coalesce_fragmented_cues(cues: list) -> bool:
    """检测 en-zh-merge：大量仅英文 cue 与中文 cue 分离。"""
    from ai_shorten import split_bilingual_text
    if not cues:
        return False
    en_only = 0
    for c in cues:
        en, zh = split_bilingual_text(c.get('text', ''))
        if en.strip() and not zh.strip():
            en_only += 1
    return en_only >= max(3, int(len(cues) * 0.08))


def coalesce_fragmented_bilingual_cues(cues: list) -> tuple[list, dict]:
    """
    en-zh-merge：把连续 EN 分片与下一条含中文的 cue 合成一条，
    时间轴取首 EN 的 start、含中文 cue 的 end。
    """
    from ai_shorten import split_bilingual_text

    out: list[dict] = []
    en_parts: list[str] = []
    t_start: float | None = None
    skipped_en = 0

    for cue in cues:
        en, zh = split_bilingual_text(cue.get('text', ''))
        if t_start is None:
            t_start = float(cue['start'])
        if en.strip():
            en_parts.append(en.strip())
        if not zh.strip():
            continue
        full_en = ' '.join(en_parts).strip()
        en_parts = []
        block = f'{full_en}\n{zh}' if full_en else zh
        out.append({
            'start': t_start,
            'end': float(cue['end']),
            'text': block,
            'tts_text': zh,
        })
        t_start = None

    skipped_en = len(en_parts)
    stats = {
        'before': len(cues),
        'after': len(out),
        'skipped_en_only_tail': skipped_en,
    }
    return out, stats


def find_zh_subtitle(directory: str) -> list:
    """
    在目录里找所有中文字幕文件。
    支持命名格式：
      .zh-Hans.srt / .zh-CN.srt / .zh.srt
    跳过：.en.srt / .en-zh-merge.srt / .merge.srt
    """
    from pathlib import Path
    d = Path(directory)
    all_subs = list(d.glob('*.srt')) + list(d.glob('*.vtt'))

    # 跳过这些（英文或双语）
    skip_markers = ['en-zh-merge', 'en-zh', '.en.', '-en.',
                    'english', 'merge', 'bilingual', '双语']
    # 中文标识
    zh_markers   = ['zh-hans', 'zh-cn', '.zh.', '-zh.',
                    'chs', 'chi', 'chinese', '简体', '中文']

    results = []
    for sub in sorted(all_subs):
        name = sub.name.lower()
        # 跳过英文/双语
        if any(m in name for m in skip_markers):
            continue
        is_zh = any(m in name for m in zh_markers)
        results.append((sub, is_zh))

    # 中文标识的排前面
    results.sort(key=lambda x: (0 if x[1] else 1, x[0].name))
    found = [str(s) for s, _ in results]
    return found


SENTENCE_END_RE = re.compile(r'[。！？.!?；…]$')
# 烧录/外挂单行最大字数（一句一行，避免占满屏幕）
MAX_ZH_CHARS_PER_LINE = 22
MAX_EN_CHARS_PER_LINE = 44


def merge_cues(cues: list, max_gap: float = 1.5, max_duration: float = 8.0,
               hard_max_duration: float = 12.0, for_subtitle: bool = False,
               break_on_sentence_end: bool = True) -> list:
    """
    把相邻字幕合并成自然句子。
    - for_subtitle=True：与烧录一致，用较短时长上限，避免一行过长
    - break_on_sentence_end=False：预览直配时连续合并，避免一句一 TTS 卡顿
    """
    if not cues:
        return cues

    merged = []
    buf = dict(cues[0])

    for cur in cues[1:]:
        gap = cur['start'] - buf['end']
        combined = buf['text'] + cur['text']
        duration = cur['end'] - buf['start']
        ends_sentence = (
            break_on_sentence_end
            and bool(SENTENCE_END_RE.search(buf['text'].strip()))
        )
        gap_too_big = gap > max_gap
        hard_cut = duration > hard_max_duration

        too_long = duration > max_duration
        if gap_too_big or hard_cut or ends_sentence or too_long:
            merged.append(buf)
            buf = dict(cur)
        else:
            buf = {'start': buf['start'], 'end': cur['end'], 'text': combined}

    merged.append(buf)
    label = '短句' if for_subtitle else '配音'
    print(f'  句子合并（{label}）：{len(cues)} 条 → {len(merged)} 条')
    return merged


def merge_cues_in_batches(cues: list, batch_size: int = 3) -> list:
    """
    把连续 N 条字幕合成一条配音单元。
    时间窗 = 首句 start ～ 末句 end，文本拼接后一次 TTS（连贯朗读）。
    """
    if batch_size < 2 or not cues:
        return cues
    merged: list[dict] = []
    for i in range(0, len(cues), batch_size):
        chunk = cues[i:i + batch_size]
        text = ''.join(str(c.get('text', '')).strip() for c in chunk)
        if not text:
            continue
        merged.append({
            'start': chunk[0]['start'],
            'end': chunk[-1]['end'],
            'text': text,
        })
    print(f'  批量句合并（每 {batch_size} 句）：{len(cues)} 条 → {len(merged)} 条')
    return merged


def video_base_from_zh_subtitle(zh_srt_path: str) -> str:
    """从字幕路径得到原视频主名（与 dub_naming 规则一致）。"""
    return video_stem_from_subtitle(zh_srt_path)


def dub_status_path(zh_srt_path: str) -> Path:
    parent = Path(zh_srt_path).parent
    base   = video_base_from_zh_subtitle(zh_srt_path)
    return parent / f'{base}.dub-status.json'


def dub_work_dir(zh_srt_path: str) -> Path:
    parent = Path(zh_srt_path).parent
    base = video_base_from_zh_subtitle(zh_srt_path)
    return parent / f'{dub_stem(base)}.dub-work'


def load_dub_status(zh_srt_path: str) -> dict | None:
    path = dub_status_path(zh_srt_path)
    if not path.exists():
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _merge_dub_status_extra(prev: dict | None, data: dict) -> dict:
    """保留 hybrid 写入的引擎标注（cues.engine、kokoro_samples_dir 等）。"""
    out = dict(data)
    if not prev:
        return out
    for key in ('cues', 'engine_counts', 'kokoro_samples_dir'):
        if prev.get(key) is not None:
            out[key] = prev[key]
    return out


def write_dub_status(zh_srt_path: str, data: dict) -> None:
    path = dub_status_path(zh_srt_path)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_dub_complete(zh_srt_path: str, voice: str | None = None) -> bool:
    """
    配音是否已全部成功（可用于批量步骤二/三门禁）。
    无状态文件时：若存在最终 MP3 则视为旧版已完成。
    """
    data = load_dub_status(zh_srt_path)
    if data is not None:
        if not data.get('complete'):
            return False
        if voice and data.get('voice') and data.get('voice') != voice:
            return False
        mp3 = dub_output_mp3_path(zh_srt_path, voice or data.get('voice', '晓晓'))
        return mp3.exists()
    if voice:
        return dub_output_mp3_path(zh_srt_path, voice).exists()
    return False


def find_en_srt(zh_srt_path: str) -> str | None:
    """根据中文字幕路径找对应的英文字幕 .en.srt"""
    p = Path(zh_srt_path)
    parent = p.parent
    base = video_base_from_zh_subtitle(zh_srt_path)

    for ext in ('.srt', '.vtt'):
        exact = parent / f'{base}.en{ext}'
        if exact.exists():
            return str(exact)

    for sub in sorted(parent.glob('*.srt')) + sorted(parent.glob('*.vtt')):
        name = sub.name.lower()
        if '.en.' in name or name.endswith('.en.srt') or '.en-' in name:
            if sub.stem.lower().startswith(base.lower()[:15]):
                return str(sub)
    return None


def _cues_overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return a_end > b_start and a_start < b_end


# 相邻字幕最小间隔（秒），避免烧录时两条同时叠在一起
SUBTITLE_MIN_GAP = 0.04


def normalize_subtitle_timeline(cues: list, min_gap: float = SUBTITLE_MIN_GAP) -> list:
    """保证字幕时间轴单调不交错：上一条结束后再出现下一条。"""
    if not cues:
        return cues

    normalized = []
    prev_end = 0.0
    for i, cue in enumerate(cues):
        start = float(cue['start'])
        end = float(cue['end'])
        text = cue.get('text', '').strip()
        if not text:
            continue

        # 与中文配音/原字幕起点对齐，不提前；仅防止与上一条重叠
        if normalized and start < prev_end + min_gap:
            start = prev_end + min_gap

        if end <= start:
            end = start + 0.8

        normalized.append({**cue, 'start': start, 'end': end, 'text': text})
        prev_end = end

    # 防止本条结束时间侵入下一条开始
    for i in range(len(normalized) - 1):
        next_start = normalized[i + 1]['start']
        if normalized[i]['end'] > next_start - min_gap:
            normalized[i]['end'] = max(
                normalized[i]['start'] + 0.5,
                next_start - min_gap,
            )

    if normalized:
        print(f'  时间轴校正：{len(cues)} 条 → {len(normalized)} 条（无交错）')
    return normalized


def _max_chars_for_line(text: str) -> int:
    zh = len(re.findall(r'[\u4e00-\u9fff]', text))
    if zh >= max(4, len(text) // 4):
        return MAX_ZH_CHARS_PER_LINE
    return MAX_EN_CHARS_PER_LINE


CONTINUATION_MARK = '——'

# 不可从中间拆开的词组（切分点落在词组内部时才禁止，如「没|有」）
_ZH_INSEPARABLE_PAIRS = frozenset({
    '没有', '不是', '不会', '不能', '不要', '不用', '不够', '不在', '不必', '不可',
    '可以', '可能', '已经', '正在', '还是', '就是', '也是', '都是', '再来',
    '非常', '无法', '应该', '需要', '准备', '为什么', '是不是', '能不能',
    '要不要', '有没有', '一个', '一些', '一点', '一下', '一种', '一起',
    '他们', '她们', '我们', '你们', '自己', '什么', '怎么', '因为', '所以',
    '但是', '然而', '虽然', '如果', '那么', '这个', '那个', '这些', '那些',
    '不过', '只是', '只有', '只要', '即使', '尽管', '无论', '不管', '不仅',
    '而且', '并且', '或者', '以及', '而是', '他的', '她的', '它的', '我的',
    '你的', '的话', '里面', '外面', '上面', '下面', '之前', '之后', '之间',
})


def _is_bad_zh_boundary(text: str, pos: int) -> bool:
    """切分点是否落在已知词组内部。"""
    if pos <= 0 or pos >= len(text):
        return False
    for pair in _ZH_INSEPARABLE_PAIRS:
        plen = len(pair)
        if plen < 2:
            continue
        start = 0
        while True:
            idx = text.find(pair, start)
            if idx < 0:
                break
            if idx < pos < idx + plen:
                return True
            start = idx + 1
    return False


def _find_natural_zh_split(text: str, max_chars: int) -> int:
    """在 max_chars 附近找自然断点，避免拆开词组。"""
    if len(text) <= max_chars:
        return len(text)
    window = text[: max_chars + 20]
    best = -1
    for sep in '，,、；;：: ':
        idx = window.rfind(sep)
        if idx >= max(4, max_chars // 3):
            best = max(best, idx + 1)
    if best < 0:
        best = max_chars
    while best > 2 and _is_bad_zh_boundary(text, best):
        best -= 1
    if best <= 2:
        best = max_chars
        while best > 2 and _is_bad_zh_boundary(text, best):
            best -= 1
    return min(max(2, best), len(text))


def _split_long_text(text: str, max_chars: int) -> list[str]:
    """过长片段按弱停顿再切，禁止在词组中间硬切。"""
    text = text.strip()
    if not text or len(text) <= max_chars:
        return [text] if text else []

    parts: list[str] = []
    rest = text
    while len(rest) > max_chars:
        best = _find_natural_zh_split(rest, max_chars)
        chunk = rest[:best].strip()
        if chunk:
            parts.append(chunk)
        rest = rest[best:].strip()
    if rest:
        parts.append(rest)
    return parts


def zh_ends_sentence(text: str) -> bool:
    return bool(re.search(r'[。！？…!?]$', (text or '').strip()))


def zh_has_continuation_mark(text: str) -> bool:
    return (text or '').rstrip().endswith(CONTINUATION_MARK)


def strip_continuation_mark(text: str) -> str:
    """烧录/外挂字幕显示时去掉配音用的未完标记。"""
    t = (text or '').rstrip()
    while t.endswith(CONTINUATION_MARK):
        t = t[:-len(CONTINUATION_MARK)].rstrip()
    return t


def mark_incomplete_zh_parts(parts: list[str]) -> list[str]:
    """非末段且非整句结束时，句尾加 —— 供 TTS 拖音表示话未说完。"""
    out: list[str] = []
    for i, raw in enumerate(parts):
        p = strip_continuation_mark(raw.strip())
        if not p:
            continue
        is_last = i == len(parts) - 1
        if not is_last and not zh_ends_sentence(p):
            p = p + CONTINUATION_MARK
        out.append(p)
    return out


def apply_continuation_to_zh(zh: str, *, is_last: bool) -> str:
    zh = strip_continuation_mark(zh.strip())
    if not zh:
        return zh
    if not is_last and not zh_ends_sentence(zh):
        return zh + CONTINUATION_MARK
    return zh


def _split_cue_text_to_parts(text: str) -> list[str]:
    """先按句号分句，过长再按逗号切分。"""
    text = text.strip()
    if not text:
        return []
    sentences = [p.strip() for p in re.split(r'(?<=[。！？.!?；])', text) if p.strip()]
    if not sentences:
        sentences = [text]
    parts: list[str] = []
    for sent in sentences:
        cap = _max_chars_for_line(sent)
        parts.extend(_split_long_text(sent, cap))
    return parts


def split_cues_by_sentence_punctuation(cues: list) -> list:
    """
    拆成短行字幕：句末标点分句 + 过长按逗号切分，并按字数比例分配时间。
    """
    result = []
    for cue in cues:
        text = cue['text'].strip()
        if not text:
            continue
        parts = _split_cue_text_to_parts(text)
        if len(parts) <= 1:
            result.append({**cue, 'text': parts[0] if parts else text})
            continue
        total_len = sum(len(p) for p in parts)
        duration = max(0.5, cue['end'] - cue['start'])
        t = cue['start']
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                end_t = cue['end']
            else:
                seg = duration * (len(part) / total_len)
                end_t = min(cue['end'], t + max(0.55, seg))
            result.append({'start': t, 'end': end_t, 'text': part})
            t = end_t
    result = normalize_subtitle_timeline(result)
    print(f'  短句拆行：{len(cues)} 条 → {len(result)} 条（约一句一行）')
    return result


def _zip_subtitle_parts(zh_parts: list[str], en_parts: list[str]) -> list[tuple[str, str]]:
    """把中英文片段对齐成对（条数不一致时合并到较短一侧的末条）。"""
    if not zh_parts:
        return [('', p) for p in en_parts]
    if not en_parts:
        return [(p, '') for p in zh_parts]
    if len(zh_parts) == len(en_parts):
        return list(zip(en_parts, zh_parts))

    if len(en_parts) > len(zh_parts):
        en_merged = en_parts[: len(zh_parts) - 1]
        en_merged.append(' '.join(en_parts[len(zh_parts) - 1 :]))
        en_parts = en_merged
    else:
        zh_merged = zh_parts[: len(en_parts) - 1]
        zh_merged.append(''.join(zh_parts[len(en_parts) - 1 :]))
        zh_parts = zh_merged
    return list(zip(en_parts, zh_parts))


def split_long_bilingual_display(cues: list) -> list:
    """双语烧录前：任一行过长则按标点拆成多段并分时显示。"""
    return split_bilingual_cues_for_display(cues)


def expand_bilingual_cues_to_speech_units(
    cues: list,
    *,
    max_zh: int = MAX_ZH_CHARS_PER_LINE,
    max_en: int = MAX_EN_CHARS_PER_LINE,
    mark_continuation: bool = True,
) -> list:
    """
    拆成「一句一配音、一句一字幕」单元（各单元独立 TTS）。
    多条时按字数比例切分时间轴（与显示拆行一致），避免多句共用同一
    start/end 导致合并时链式顺延、听感断续。
    """
    out: list[dict] = []
    for ci, cue in enumerate(cues):
        text = str(cue.get('text', '')).strip()
        if not text:
            continue
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) >= 2:
            en_line, zh_line = lines[0], lines[1]
            en_parts = _split_line_for_display(en_line, max_en)
            zh_parts = _split_line_for_display(zh_line, max_zh)
            pairs = _zip_subtitle_parts(zh_parts, en_parts)
        elif len(lines) == 1:
            cap = min(_max_chars_for_line(lines[0]), max(max_zh, max_en))
            parts = _split_line_for_display(lines[0], cap)
            pairs = [(p, '') for p in parts]
        else:
            pairs = [(text, '')]

        if not pairs:
            continue

        if len(pairs) == 1:
            en_p, zh_p = pairs[0]
            if mark_continuation and zh_p:
                zh_p = apply_continuation_to_zh(zh_p, is_last=True)
            body = f'{en_p}\n{zh_p}' if en_p and zh_p else (en_p or zh_p)
            out.append({
                'start': cue['start'],
                'end': cue['end'],
                'text': body,
                'speech_unit': True,
                'source_cue': ci,
                'unit_idx': 0,
            })
            continue

        total_w = sum(len(a) + len(b) for a, b in pairs) or 1
        dur = max(0.5, float(cue['end']) - float(cue['start']))
        t = float(cue['start'])
        parent_wp = cue.get('window_params')
        for ui, (en_p, zh_p) in enumerate(pairs):
            if mark_continuation and zh_p:
                zh_p = apply_continuation_to_zh(
                    zh_p, is_last=(ui == len(pairs) - 1))
            end_t = (
                float(cue['end']) if ui == len(pairs) - 1
                else min(
                    float(cue['end']),
                    t + max(0.55, dur * (len(en_p) + len(zh_p)) / total_w),
                )
            )
            body = f'{en_p}\n{zh_p}' if en_p and zh_p else (en_p or zh_p)
            row: dict = {
                'start': t,
                'end': end_t,
                'text': body,
                'speech_unit': True,
                'source_cue': ci,
                'unit_idx': ui,
            }
            slice_dur = max(0.3, end_t - t)
            if parent_wp:
                try:
                    from speech_activity import (
                        compute_window_params,
                        speech_activity_config,
                    )
                    scale = slice_dur / max(dur, 0.01)
                    pw = dict(parent_wp)
                    for key in ('target_chars', 'max_chars', 'min_chars'):
                        if key in pw and pw[key]:
                            pw[key] = max(1, int(round(int(pw[key]) * scale)))
                    pw['duration'] = round(slice_dur, 3)
                    row['window_params'] = pw
                    row['speech_window'] = {
                        'start': round(t, 3),
                        'end': round(end_t, 3),
                        'duration': round(slice_dur, 3),
                    }
                except ImportError:
                    pass
            out.append(row)
            t = end_t

    if len(out) != len(cues):
        print(
            f'  配音拆句：{len(cues)} 条 → {len(out)} 句'
            f'（已按字数分配子时间轴，避免多句抢同一窗口）')
    return out


def split_bilingual_cues_for_display(
    cues: list,
    *,
    max_zh: int = MAX_ZH_CHARS_PER_LINE,
    max_en: int = MAX_EN_CHARS_PER_LINE,
) -> list:
    """
    外挂/烧录显示：按句末标点拆条（仅当尚未按句独立配音时使用）。
    若 cues 已含 speech_unit（每句独立 TTS+同步），请直接写入，勿再按比例切时间。
    """
    if cues and all(c.get('speech_unit') for c in cues if c.get('text')):
        return cues

    out: list[dict] = []
    for cue in cues:
        text = str(cue.get('text', '')).strip()
        if not text:
            continue
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) >= 2:
            en_line, zh_line = lines[0], lines[1]
            en_parts = _split_line_for_display(en_line, max_en)
            zh_parts = _split_line_for_display(zh_line, max_zh)
            pairs = _zip_subtitle_parts(zh_parts, en_parts)
            if len(pairs) <= 1:
                out.append({**cue, 'text': f'{en_line}\n{zh_line}'})
            else:
                out.extend(_time_slice_cue(cue, pairs))
        elif len(lines) == 1:
            cap = _max_chars_for_line(lines[0])
            cap = min(cap, max(max_zh, max_en))
            parts = _split_line_for_display(lines[0], cap)
            if len(parts) <= 1:
                out.append(cue)
            else:
                out.extend(_time_slice_cue(cue, [(p, '') for p in parts]))
        else:
            out.append(cue)
    result = normalize_subtitle_timeline(out)
    if len(result) != len(cues):
        print(
            f'  显示字幕拆行：{len(cues)} 条 → {len(result)} 条'
            f'（一句一行，中文≤{max_zh}字 英文≤{max_en}字）')
    return result


def _split_line_for_display(text: str, max_chars: int) -> list[str]:
    """句末标点分句，过长再按逗号切到单行字数上限。"""
    text = text.strip()
    if not text:
        return []
    sentences = [
        p.strip()
        for p in re.split(r'(?<=[。！？.!?；])', text)
        if p.strip()
    ]
    if not sentences:
        sentences = [text]
    parts: list[str] = []
    for sent in sentences:
        if len(sent) <= max_chars:
            parts.append(sent)
        else:
            parts.extend(_split_long_text(sent, max_chars))
    return parts or [text]


def _time_slice_cue(cue: dict, pairs: list[tuple[str, str]]) -> list[dict]:
    total = sum(len(a) + len(b) for a, b in pairs) or 1
    dur = max(0.5, cue['end'] - cue['start'])
    t = cue['start']
    rows = []
    for i, (en_p, zh_p) in enumerate(pairs):
        end_t = cue['end'] if i == len(pairs) - 1 else min(
            cue['end'], t + max(0.55, dur * (len(en_p) + len(zh_p)) / total))
        if en_p and zh_p:
            body = f'{en_p}\n{zh_p}'
        else:
            body = en_p or zh_p
        rows.append({**cue, 'start': t, 'end': end_t, 'text': body})
        t = end_t
    return rows


def _en_cue_in_zh_segment(ec: dict, seg_start: float, seg_end: float) -> bool:
    """英文句与当前中文段有足够重叠才算入本句（避免拖入下一句英文）。"""
    overlap_start = max(ec['start'], seg_start)
    overlap_end = min(ec['end'], seg_end)
    if overlap_end <= overlap_start:
        return False
    overlap = overlap_end - overlap_start
    en_dur = max(0.01, ec['end'] - ec['start'])
    return overlap / en_dur >= 0.35


def generate_sync_bilingual_srt(zh_cues: list, en_srt_path: str,
                                output_path: str) -> bool:
    """
    生成烧录用双语 SRT（一行一句，时间轴不交错）：
    - 中文：与配音/原中文字幕同一起止（说第一个字时出现）
    - 英文：仅收集落在该中文时间段内的 .en.srt 原文
    - 显示时长=中文段时长，不因英文拖长导致与下一条重叠
    """
    with open(en_srt_path, 'r', encoding='utf-8-sig') as f:
        en_text = f.read()
    ext = Path(en_srt_path).suffix.lower()
    en_cues = parse_vtt(en_text) if ext == '.vtt' else parse_srt(en_text)
    if not en_cues:
        print(f'  ⚠️  英文字幕为空：{en_srt_path}')
        return False

    result_cues = []
    for i, zh_cue in enumerate(zh_cues):
        zh_text = zh_cue['text'].strip()
        if not zh_text:
            continue

        seg_start = float(zh_cue['start'])
        seg_end = float(zh_cue['end'])

        en_hits = []
        for ec in en_cues:
            if _en_cue_in_zh_segment(ec, seg_start, seg_end):
                t = ec['text'].strip()
                if t:
                    en_hits.append((ec['start'], t))

        en_hits.sort(key=lambda x: x[0])
        en_parts = []
        for _, t in en_hits:
            if not en_parts or en_parts[-1] != t:
                en_parts.append(t)
        en_text_line = ' '.join(en_parts).strip()

        cue_start = seg_start
        cue_end = seg_end

        if en_text_line and zh_text:
            combined = f'{en_text_line}\n{zh_text}'
        elif zh_text:
            combined = zh_text
        elif en_text_line:
            combined = en_text_line
        else:
            continue

        result_cues.append({
            'index': i + 1,
            'start': cue_start,
            'end': cue_end,
            'text': combined,
        })

    result_cues = normalize_subtitle_timeline(result_cues)
    result_cues = split_long_bilingual_display(result_cues)

    with open(output_path, 'w', encoding='utf-8') as f:
        for cue in result_cues:
            f.write(f'{cue["index"]}\n')
            f.write(f'{format_srt_time(cue["start"])} --> {format_srt_time(cue["end"])}\n')
            f.write(f'{cue["text"]}\n\n')

    print(f'  ✅ 同步双语字幕：{len(result_cues)} 条（短句一行 | 与中文同起）→ {output_path}')
    return True



def create_display_cues(cues: list) -> list:
    """
    创建显示用字幕（不合并）：
    - 按标点断句，每句一行
    - 去掉标点符号
    - 按字数比例分配时间
    """
    result = []
    for cue in cues:
        text = cue['text'].strip()
        if not text:
            continue
        # 按标点切分
        parts = re.split(r'[，。！？、；：…—,.!?;:]+', text)
        parts = [p.strip() for p in parts if p.strip()]
        if not parts:
            continue
        if len(parts) == 1:
            result.append({**cue, 'text': parts[0]})
            continue
        # 按字数比例分配时间
        total_len = sum(len(p) for p in parts) or 1
        duration = max(0.5, cue['end'] - cue['start'])
        t = cue['start']
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                end_t = cue['end']
            else:
                seg = duration * (len(part) / total_len)
                end_t = min(cue['end'], t + max(0.4, seg))
            result.append({'start': t, 'end': end_t, 'text': part})
            t = end_t
    result = normalize_subtitle_timeline(result)
    print(f'  显示字幕：{len(cues)} 条 -> {len(result)} 条（按标点断句，去标点）')
    return result


def write_sync_zh_srt(zh_cues: list, output_path: str) -> None:
    """写出与配音完全一致的中文字幕。"""
    with open(output_path, 'w', encoding='utf-8') as f:
        for i, cue in enumerate(zh_cues, 1):
            f.write(f'{i}\n')
            f.write(f'{format_srt_time(cue["start"])} --> {format_srt_time(cue["end"])}\n')
            f.write(f'{cue["text"]}\n\n')
    print(f'  ✅ 同步中文字幕：{len(zh_cues)} 条 → {output_path}')


def format_srt_time(sec: float) -> str:
    """秒 → SRT 时间格式 00:00:00,000"""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int((sec - int(sec)) * 1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def generate_merged_bilingual_srt(merged_zh_cues: list,
                                   bilingual_srt_path: str,
                                   output_path: str) -> bool:
    """
    根据合并后的中文时间段，生成对应的合并双语字幕。
    每个合并段的时间范围内，收集所有英文行和中文行，合并成一条字幕。
    """
    # 解析双语字幕
    with open(bilingual_srt_path, 'r', encoding='utf-8-sig') as f:
        bil_text = f.read()
    bil_cues = parse_srt(bil_text)
    if not bil_cues:
        bil_cues = parse_vtt(bil_text)

    # 双语字幕每条通常是两行：英文\n中文
    def split_bilingual(text: str):
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        if len(lines) >= 2:
            # 判断哪行是英文哪行是中文
            zh_ratio = lambda t: len(re.findall(r'[\u4e00-\u9fff]', t)) / max(len(t), 1)
            if zh_ratio(lines[0]) > zh_ratio(lines[-1]):
                return lines[-1], lines[0]   # en, zh
            else:
                return lines[0], lines[-1]   # en, zh
        return lines[0], ''

    # 按合并后的中文时间段，收集对应的双语内容
    result_cues = []
    for i, zh_cue in enumerate(merged_zh_cues):
        seg_start = zh_cue['start']
        seg_end   = zh_cue['end']

        # 收集这个时间段内的所有双语字幕
        en_parts, zh_parts = [], []
        for bc in bil_cues:
            # 有重叠即收集
            if bc['end'] > seg_start and bc['start'] < seg_end:
                en, zh = split_bilingual(bc['text'])
                if en and en not in en_parts:
                    en_parts.append(en)
                if zh and zh not in zh_parts:
                    zh_parts.append(zh)

        if not en_parts and not zh_parts:
            # 双语字幕里没找到对应内容，用中文字幕代替
            zh_parts = [zh_cue['text']]

        en_text = ' '.join(en_parts)
        zh_text = ''.join(zh_parts)

        # 双语格式：英文在上，中文在下
        combined = ''
        if en_text and zh_text:
            combined = f'{en_text}\n{zh_text}'
        elif zh_text:
            combined = zh_text
        elif en_text:
            combined = en_text

        if combined:
            result_cues.append({
                'index': i + 1,
                'start': seg_start,
                'end':   seg_end,
                'text':  combined,
            })

    # 写出 SRT 文件
    with open(output_path, 'w', encoding='utf-8') as f:
        for cue in result_cues:
            f.write(f'{cue["index"]}\n')
            f.write(f'{format_srt_time(cue["start"])} --> {format_srt_time(cue["end"])}\n')
            f.write(f'{cue["text"]}\n\n')

    print(f'  ✅ 合并双语字幕：{len(result_cues)} 条 → {output_path}')
    return True

# ── TTS 合成 ────────────────────────────────────────────────

def _edge_rate_to_local_wpm(rate: str) -> int:
    """把 Edge 的 +0% 语速映射为 pyttsx3 的 words-per-minute。"""
    base = 200
    m = re.match(r'([+-]?\d+)%', rate.strip())
    if m:
        pct = int(m.group(1))
        return max(80, min(320, base + base * pct // 100))
    return base


LOCAL_VOICE_ALIASES = {
    'kangkang': ('kangkang', 'kang kang', '康康'),
    'huihui': ('huihui', 'hui hui', '慧慧', '晓晓'),
    'yaoyao': ('yaoyao', '瑶瑶'),
    'zhiwei': ('zhiwei', 'zhi wei', '志伟'),
    'hanhan': ('hanhan', '涵涵'),
}


def _voice_blob(v) -> str:
    return f'{v.name} {getattr(v, "id", "")}'.lower()


def _guess_voice_gender(blob: str) -> str:
    if any(k in blob for k in ('kangkang', 'kang kang', 'zhiwei', 'yunxi', 'yunyang', 'male', '男')):
        return 'male'
    if any(k in blob for k in ('huihui', 'yaoyao', 'xiaoxiao', 'female', '女')):
        return 'female'
    return 'unknown'


def find_local_voices(zh_only: bool = True, gender: str | None = None) -> list[dict]:
    """列出 pyttsx3 可见的中文语音。"""
    try:
        import pyttsx3
    except ImportError:
        return []

    engine = pyttsx3.init()
    out = []
    seen = set()
    for v in engine.getProperty('voices'):
        blob = _voice_blob(v)
        if zh_only and not any(
            k in blob for k in (
                'zh-cn', 'zh_cn', 'zh-hans', 'zh-tw', 'zh_tw',
                'chinese', '简体', '繁体', '中文', 'huihui', 'kangkang',
                'yaoyao', 'zhiwei', 'hanhan',
            )
        ):
            continue
        key = 'other'
        for alias, needles in LOCAL_VOICE_ALIASES.items():
            if any(n in blob for n in needles):
                key = alias
                break
        g = _guess_voice_gender(blob)
        if gender and g != gender and g != 'unknown':
            continue
        vid = v.id
        if vid in seen:
            continue
        seen.add(vid)
        simp = 'zh-cn' in blob or 'zh_cn' in blob or 'zh-hans' in blob or 'simplified' in blob
        out.append({
            'key': key,
            'name': v.name,
            'id': vid,
            'gender': g,
            'simplified': simp,
            'blob': blob,
        })
    try:
        engine.stop()
    except Exception:
        pass
    return out


def _load_local_voice_prefs() -> tuple[str, str]:
    """从 tubeget.config.json 读取 local_voice / local_voice_gender。"""
    voice_key = 'kangkang'
    gender = 'male'
    cfg_path = Path(__file__).parent / 'tubeget.config.json'
    if cfg_path.exists():
        try:
            with open(cfg_path, encoding='utf-8') as f:
                data = json.load(f)
            voice_key = str(data.get('local_voice', voice_key)).lower()
            gender = str(data.get('local_voice_gender', gender)).lower()
        except Exception:
            pass
    return voice_key, gender


def resolve_local_voice_id(
    engine, voice_key: str | None = None, gender: str | None = None,
) -> str | None:
    """按配置 key / 性别选择语音 ID；优先简体男声 Kangkang。"""
    if voice_key is None or gender is None:
        dk, dg = _load_local_voice_prefs()
        voice_key = voice_key or dk
        gender = gender or dg

    voices = find_local_voices(zh_only=True, gender=None)
    if not voices:
        return None

    key = voice_key.lower()

    def score(item: dict) -> tuple:
        blob = item['blob']
        key_hit = 0 if item['key'] == key else 1
        g_hit = 0 if (gender == 'male' and item['gender'] == 'male') else (
            0 if (gender == 'female' and item['gender'] == 'female') else 1)
        simp = 0 if item['simplified'] else 1
        # 云希不可用：本地男声优先 Kangkang 简体，避免落到慧慧
        if key == 'zhiwei':
            trad = 0 if 'zh-tw' in blob or 'zh_tw' in blob or 'taiwan' in blob else 1
            return (key_hit, trad, g_hit, simp, blob)
        return (key_hit, g_hit, simp, blob)

    ranked = sorted(voices, key=score)
    return ranked[0]['id'] if ranked else None


def synth_one_local(
    text: str, out_path: str, rate: str = '+0%',
    voice_key: str | None = None, mark_fallback: bool = True,
) -> bool:
    """
    使用 Windows 本地 SAPI 语音（pyttsx3）合成，用于 Edge 在线失败时补全。
    需安装：pip install pyttsx3
    """
    try:
        import pyttsx3
        from pydub import AudioSegment
    except ImportError:
        return False

    text = text.strip()
    if not text:
        return False

    wav_path = out_path.rsplit('.', 1)[0] + '_local.wav'

    with _local_tts_lock:
        try:
            engine = pyttsx3.init()
            voice_id = resolve_local_voice_id(engine, voice_key=voice_key)
            if voice_id:
                engine.setProperty('voice', voice_id)
            engine.setProperty('rate', _edge_rate_to_local_wpm(rate))
            engine.save_to_file(text, wav_path)
            engine.runAndWait()
            try:
                engine.stop()
            except Exception:
                pass
        except Exception:
            return False

    if not os.path.exists(wav_path) or os.path.getsize(wav_path) < 500:
        return False

    try:
        AudioSegment.from_wav(wav_path).export(out_path, format='mp3', bitrate='128k')
    except Exception:
        return False
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass

    return os.path.exists(out_path) and os.path.getsize(out_path) > 500



# ── Kokoro TTS（本地离线）──────────────────────────────────────
_kokoro_pipeline = None

def get_kokoro_pipeline():
    global _kokoro_pipeline
    if _kokoro_pipeline is None:
        from kokoro import KPipeline
        _kokoro_pipeline = KPipeline(lang_code='z')
        print('  Kokoro TTS 已加载（本地离线）')
    return _kokoro_pipeline


async def synth_one_kokoro(text: str, voice: str, rate: str,
                            out_path: str, cue_index: int = -1) -> bool:
    """使用 Kokoro 本地合成"""
    import soundfile as sf
    import numpy as np
    try:
        # rate: "+0%" -> speed 1.0
        pct = int(rate.replace('%', '').replace('+', ''))
        speed = 1.0 + pct / 100.0
    except:
        speed = 1.0
    try:
        pipeline = get_kokoro_pipeline()
        audio_parts = []
        for _, _, audio in pipeline(text, voice=voice, speed=speed):
            audio_parts.append(audio)
        if not audio_parts:
            return False
        combined = np.concatenate(audio_parts)
        if len(combined) < 100:
            return False
        if len(combined) / 24000 < 0.8:
            label = f'第 {cue_index + 1} 条' if cue_index >= 0 else ''
            print(f'\n    [失败] {label} Kokoro 输出过短，请升级: '
                  f'py -3.12 -m pip install "kokoro>=0.9.2" "misaki[zh]>=0.9.2"')
            return False
        wav_path = (
            out_path if out_path.lower().endswith('.wav')
            else out_path.rsplit('.', 1)[0] + '_kokoro.wav'
        )
        sf.write(wav_path, combined, 24000)
        try:
            from pydub import AudioSegment
            AudioSegment.from_wav(wav_path).export(
                out_path, format='mp3', bitrate='128k')
        finally:
            if wav_path != out_path and os.path.exists(wav_path):
                os.remove(wav_path)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 500
    except Exception as e:
        label = f'第 {cue_index + 1} 条' if cue_index >= 0 else ''
        print(f'\n    [失败] {label} Kokoro: {e}')
        return False


def _load_azure_config() -> tuple[str, str]:
    """从配置文件或环境变量读取 Azure Key 和 Region。"""
    # 优先读当前目录的 tubeget.config.json
    cfg_path = Path(__file__).parent / 'tubeget.config.json'
    if cfg_path.exists():
        try:
            with open(cfg_path, encoding='utf-8') as f:
                data = json.load(f)
            az = data.get('azure') or {}
            key = az.get('key', '')
            region = az.get('region', 'eastasia')
            if key:
                return key, region
        except Exception:
            pass
    # 再从环境变量读
    return os.environ.get('AZURE_SPEECH_KEY', ''), os.environ.get('AZURE_SPEECH_REGION', 'eastasia')


async def synth_one_azure(text: str, voice_id: str, rate: str,
                          out_path: str, retries: int = 3,
                          cue_index: int = -1) -> bool:
    """使用 Azure TTS 合成单条音频。"""
    import urllib.request
    import urllib.error

    key, region = _load_azure_config()
    if not key:
        label = f'第 {cue_index + 1} 条' if cue_index >= 0 else '本条'
        print(f'\n    [失败] {label} Azure Key 未配置')
        return False

    # 过滤纯标点/空文本，Azure 无法合成
    import xml.sax.saxutils
    clean = re.sub(r"[\s。，！？.!?,、；;：:…—\"\"\'\'\[\]【】（）()]+", '', text)
    if not clean:
        return False
    safe_text = xml.sax.saxutils.escape(text.strip())
    if not safe_text:
        return False

    # rate: "+0%" / "-10%" 等 → prosody rate 属性
    ssml = (
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='zh-CN'>"
        f"<voice name='{voice_id}'>"
        f"<prosody rate='{rate}'>{safe_text}</prosody>"
        "</voice></speak>"
    )
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-16khz-128kbitrate-mono-mp3",
    }

    def _do_azure_request(url, headers, ssml_bytes):
        """在线程池中执行阻塞 HTTP 请求。"""
        req = urllib.request.Request(url, data=ssml_bytes, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read(), None
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')[:200]
            return None, f'HTTP {e.code}: {body}'
        except Exception as e:
            return None, f'{type(e).__name__}: {e}'

    loop = asyncio.get_event_loop()
    last_err = None
    for attempt in range(1, retries + 1):
        data, err = await loop.run_in_executor(
            None, _do_azure_request, url, headers, ssml.encode('utf-8'))
        if err:
            last_err = err
            if attempt < retries:
                await asyncio.sleep(attempt * 2)
            continue
        if data and len(data) > 500:
            with open(out_path, 'wb') as f:
                f.write(data)
            return True
        else:
            sz = len(data) if data else 0
            last_err = f'返回数据过短({sz}字节)'
            if attempt < retries:
                await asyncio.sleep(attempt * 2)

    label = f'第 {cue_index + 1} 条' if cue_index >= 0 else '本条'
    print(f'\n    [失败] {label} Azure 合成失败: {last_err}')
    return False


def _load_tts_engine_config() -> tuple[str, str]:
    """返回 (engine, kokoro_voice)。"""
    if os.environ.get('TUBEGET_QUICK_BILINGUAL', '').strip().lower() in (
            '1', 'true', 'yes'):
        return 'edge', 'zm_yunxi'
    cfg_path = Path(__file__).parent / 'tubeget.config.json'
    engine = 'edge'
    kokoro_voice = 'zm_yunxi'
    if cfg_path.exists():
        try:
            with open(cfg_path, encoding='utf-8') as f:
                cfg = json.load(f)
            engine = str(cfg.get('engine', 'edge')).lower().replace('_', '+')
            kokoro_voice = cfg.get('kokoro_voice', 'zm_yunxi')
        except Exception:
            pass
    return engine, kokoro_voice


def trim_audio(mp3_path: str, max_duration: float) -> bool:
    """
    硬切音频（已弃用：会截断语义，不应在配音流程中使用）。
    超窗请用 fit_audio_no_truncate / adjust_speed 加速，或源头缩短字幕。
    """
    if os.environ.get('TUBEGET_NEVER_TRUNCATE_DUB', '').strip().lower() in (
            '1', 'true', 'yes'):
        return False
    import subprocess as _sp
    if max_duration <= 0.1:
        return False
    tmp = mp3_path + '.trim.mp3'
    r = _sp.run(
        ['ffmpeg', '-y', '-i', mp3_path,
         '-t', f'{max_duration:.3f}', '-b:a', '128k', tmp],
        capture_output=True, text=True, errors='replace')
    if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 100:
        os.replace(tmp, mp3_path)
        return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def fit_audio_strict_window(
    mp3_path: str,
    window_sec: float,
    *,
    speak_duration: float | None = None,
    max_tempo: float = 1.5,
    hard_max_tempo: float = 2.0,
    allow_slack_sec: float = 0.0,
) -> dict:
    """
    Speech Activity：优先加速对齐时间窗；绝不截断音频。
    先 max_tempo（默认 1.5×），仍超窗再 hard_max_tempo；仍超则保留全长。
    """
    window_sec = max(0.3, window_sec)
    slack = max(0.0, allow_slack_sec)
    speak = speak_duration if speak_duration is not None else window_sec
    speak = max(0.25, speak)
    limit = speak + slack

    actual = get_audio_duration(mp3_path)
    if actual <= 0:
        return {'action': 'none', 'actual_sec': 0, 'window_sec': window_sec}

    action = 'unchanged'
    if actual > limit + 0.03:
        if adjust_speed(mp3_path, limit, max_tempo=max_tempo):
            action = 'speedup'
        actual = get_audio_duration(mp3_path)
    if actual > limit + 0.03 and hard_max_tempo > max_tempo + 0.02:
        ratio = actual / limit
        tempo = min(ratio, hard_max_tempo)
        if tempo > max_tempo + 0.02 and _ffmpeg_atempo_chain(mp3_path, tempo):
            action = 'speedup_extended'
        actual = get_audio_duration(mp3_path)

    over = actual > limit + 0.05
    return {
        'action': action,
        'actual_sec': round(get_audio_duration(mp3_path) or actual, 3),
        'window_sec': round(window_sec, 3),
        'speak_duration': round(speak, 3),
        'allow_slack_sec': round(slack, 3),
        'over_window': over,
    }


def apply_tail_fade(mp3_path: str, fade_ms: int = 0) -> bool:
    """已禁用：正常口播不做句尾淡出。"""
    _ = mp3_path, fade_ms
    return False


def _speech_strict_window_enabled(cfg: dict | None = None) -> bool:
    """硬窗口：每条从字幕起点开播（可能叠音）。软窗口时改为顺延防重叠。"""
    c = cfg or _load_dub_pipeline_config()
    if bool(c.get('speech_window_soft', True)):
        return False
    return bool(c.get('speech_activity_align') or c.get('dub_strict_window'))


def _load_dub_pipeline_config() -> dict:
    """配音管线：是否生成 sync 字幕、批量句合并、窗口内播音等。"""
    defaults = {
        'generate_sync_subtitles': False,
        'dub_batch_merge_size': 3,
        'dub_split_by_sentence': False,
        'dub_anchor_window': True,
        'dub_fill_window': True,
        'dub_strict_window': False,
        'speech_activity_align': False,
        'speech_window_soft': True,
        'speech_activity_max_tempo': 1.3,
        'speech_activity_hard_max_tempo': 1.5,
        'dub_never_truncate': False,
        'speech_activity_end_buffer_sec': 0.0,
        'speech_activity_fade_out_ms': 0,
        'speech_activity_continuation_slack_sec': 1.5,
        'merge_max_gap': 1.2,
        'merge_max_duration': 7.0,
        'merge_hard_max_duration': 12.0,
        'edge_segment_mode': True,
        'edge_segment_profile': 'auto',
    }
    for cfg_path in (Path.cwd() / 'tubeget.config.json',
                     Path(__file__).parent / 'tubeget.config.json'):
        if not cfg_path.exists():
            continue
        try:
            with open(cfg_path, encoding='utf-8') as f:
                cfg = json.load(f)
            defaults.update({
                k: cfg[k] for k in defaults
                if k in cfg and not str(k).startswith('_')
            })
        except Exception:
            pass
        break
    policy = _load_dub_policy_config_raw()
    if 'generate_sync_subtitles' in policy:
        defaults['generate_sync_subtitles'] = policy['generate_sync_subtitles']
    if os.environ.get('TUBEGET_QUICK_BILINGUAL', '').strip().lower() in (
            '1', 'true', 'yes'):
        defaults.update({
            'speech_activity_align': False,
            'speech_window_soft': False,
            'dub_strict_window': True,
            'dub_fill_window': False,
            'dub_anchor_window': True,
            'dub_never_truncate': True,
            'quick_dub_max_tempo': 2.0,
            'quick_dub_segment_max_chars': 420,
            'generate_sync_subtitles': False,
            'dub_batch_merge_size': 0,
            'edge_segment_mode': False,
            'merge_max_gap': 1.2,
            'merge_max_duration': 7.0,
            'merge_hard_max_duration': 10.0,
            'merge_break_on_sentence_end': True,
        })
    return defaults


def dub_pipeline_fingerprint(cfg: dict | None = None) -> str:
    """配音切分策略指纹（变更后须清空 dub-work 缓存）。"""
    c = cfg or _load_dub_pipeline_config()
    seg = 'seg1' if c.get('edge_segment_mode', True) else 'seg0'
    return (
        f'{seg}'
        f'_b{int(c.get("dub_batch_merge_size", 0) or 0)}'
        f'_s{1 if c.get("dub_split_by_sentence") else 0}'
        f'_g{c.get("merge_max_gap", 1.2)}'
        f'_p{c.get("edge_segment_profile", "auto")}'
    )


def _load_dub_policy_config_raw() -> dict:
    defaults = {
        'allow_partial_dub': True,
        'min_dub_success_rate': 0.98,
        'max_ignored_missing_ratio': 0.001,
        'generate_sync_subtitles': False,
    }
    for cfg_path in (Path.cwd() / 'tubeget.config.json',
                     Path(__file__).parent / 'tubeget.config.json'):
        if not cfg_path.exists():
            continue
        try:
            with open(cfg_path, encoding='utf-8') as f:
                cfg = json.load(f)
            defaults.update({
                k: cfg[k] for k in defaults
                if k in cfg and not str(k).startswith('_')
            })
        except Exception:
            pass
        break
    return defaults


def _load_dub_policy_config() -> dict:
    """配音完整性策略（tubeget.config.json）。"""
    raw = _load_dub_policy_config_raw()
    return {k: raw[k] for k in (
        'allow_partial_dub', 'min_dub_success_rate', 'max_ignored_missing_ratio',
    )}


def dub_partial_log_path(zh_srt_path: str) -> Path:
    """千分之一可忽略缺句的审计日志（按视频主名，追加写入）。"""
    parent = Path(zh_srt_path).parent
    base = video_base_from_zh_subtitle(zh_srt_path)
    return parent / f'{base}.dub-partial.log'


def log_ignored_missing_dub(
    zh_srt_path: str,
    *,
    total: int,
    failed: list[int],
    missing_ratio: float,
    voice: str,
    note: str = '',
) -> Path:
    from datetime import datetime
    path = dub_partial_log_path(zh_srt_path)
    cue_nos = [i + 1 for i in failed]
    line = (
        f'{datetime.now().isoformat(timespec="seconds")} '
        f'PARTIAL_ACCEPT subtitle={Path(zh_srt_path).name} '
        f'total={total} missing={len(failed)} ratio={missing_ratio:.6f} '
        f'indices={failed} cue_nos={cue_nos} voice={voice}'
    )
    if note:
        line += f' note={note}'
    line += '\n'
    with open(path, 'a', encoding='utf-8') as f:
        f.write(line)
    return path


def can_ignore_missing_dub(
    failed_count: int, total: int, policy: dict | None = None,
) -> bool:
    """缺失比例 ≤ max_ignored_missing_ratio（默认千分之一）时可忽略缺句继续合并。"""
    if failed_count <= 0 or total <= 0:
        return False
    policy = policy or _load_dub_policy_config()
    if not policy.get('allow_partial_dub', True):
        return False
    ratio = failed_count / total
    return ratio <= float(policy.get('max_ignored_missing_ratio', 0.001))


async def last_chance_local_fill(
    cues: list, failed: list[int], rate: str, work_dir: str,
) -> tuple[list, list[int]]:
    """末轮：仍失败的句依次尝试 Kokoro → Windows 本地语音。"""
    from cue_synth_eligibility import is_synth_eligible, extract_chinese
    if not failed:
        return [], []
    synth_failed = [
        i for i in failed
        if is_synth_eligible(cues[i].get('text', ''), extract_fn=extract_chinese)
    ]
    skipped = len(failed) - len(synth_failed)
    if skipped:
        print(f'  末轮补全：跳过 {skipped} 条不可合成句（成片处为静音）')
    failed = synth_failed
    if not failed:
        return [], []
    _, kokoro_voice = _load_tts_engine_config()
    loop = asyncio.get_event_loop()
    recovered: list[tuple] = []
    still: list[int] = []
    print(f'  末轮补全：{len(failed)} 条 → Kokoro → 本地语音')
    for i in sorted(failed):
        cue = cues[i]
        text = tts_text_from_cue(cue)
        mp3 = os.path.join(work_dir, f'cue_{i:04d}.mp3')
        ok = False
        if text:
            try:
                ok = await synth_one_kokoro(
                    text, kokoro_voice, rate, mp3, i)
            except Exception:
                ok = False
            if not ok:
                ok = await loop.run_in_executor(
                    None, synth_one_local, text, mp3, rate)
        if ok and os.path.exists(mp3) and os.path.getsize(mp3) > 500:
            _apply_cue_speed_adjust(mp3, cue)
            recovered.append((i, cue['start'], cue['end'], mp3))
            print(f'    第 {i + 1} 条 末轮补全 OK')
        else:
            still.append(i)
    return recovered, still


def _is_hybrid_engine() -> bool:
    engine, _ = _load_tts_engine_config()
    return engine in ('edge+kokoro', 'hybrid')


def _should_use_edge_scheduler() -> bool:
    """纯 Edge 路径走 rolling 调度器（无 Azure / Kokoro / local）。"""
    engine, _ = _load_tts_engine_config()
    if engine in ('kokoro', 'local'):
        return False
    if engine in ('edge+kokoro', 'hybrid'):
        return False
    azure_key, _ = _load_azure_config()
    return not azure_key


async def synth_one(text: str, voice_id: str, rate: str,
                    out_path: str, retries: int = 3,
                    use_local_fallback: bool = True,
                    cue_index: int = -1) -> bool:
    """根据配置自动选择 Kokoro / Azure / Edge TTS。"""

    _engine, _kokoro_voice = _load_tts_engine_config()
    if _engine == 'kokoro':
        return await synth_one_kokoro(text, _kokoro_voice, rate, out_path, cue_index)

    if _engine == 'local':
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, synth_one_local, text, out_path, rate)

    # ── Azure 引擎 ──────────────────────────────────────────
    azure_key, azure_region = _load_azure_config()
    if azure_key:
        return await synth_one_azure(text, voice_id, rate, out_path,
                                     retries=retries, cue_index=cue_index)

    # ── Edge 引擎 ──────────────────────────────────────────
    from edge_tts_scheduler import edge_communicate
    last_err = None
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            comm = edge_communicate(text, voice_id, rate=rate)
            await comm.save(out_path)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 500:
                return True
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                await asyncio.sleep(attempt * 2 + 1)

    if use_local_fallback:
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(
            None, synth_one_local, text, out_path, rate)
        if ok:
            label = f'第 {cue_index + 1} 条' if cue_index >= 0 else '本条'
            print(f'\n    [OK] {label} 已用本地语音补全（Edge 失败）')
            return True

    if last_err:
        label = f'第 {cue_index + 1} 条' if cue_index >= 0 else '本条'
        print(f'\n    [失败] {label} 合成失败（含本地补全）: {last_err}')
    return False


def collect_results_from_work_dir(work_dir: str, cues: list) -> list:
    """从缓存目录收集已成功的 cue 片段。"""
    out = []
    for i, cue in enumerate(cues):
        mp3 = os.path.join(work_dir, f'cue_{i:04d}.mp3')
        if os.path.exists(mp3) and os.path.getsize(mp3) > 500:
            out.append((i, cue['start'], cue['end'], mp3))
    out.sort(key=lambda x: x[0])
    return out



def get_audio_duration(path: str) -> float:
    """用 ffprobe 获取音频精确时长"""
    import subprocess
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, errors='replace')
        return float(r.stdout.strip())
    except Exception:
        return 0


def _build_atempo_filter_chain(tempo: float) -> str | None:
    """ffmpeg atempo 单次仅 0.5~2.0，更高/更低倍率用链式 filter。"""
    if abs(tempo - 1.0) < 0.02:
        return None
    tempo = max(0.25, min(4.0, tempo))
    parts: list[str] = []
    t = tempo
    while t > 2.0 + 0.001:
        parts.append('atempo=2.0')
        t /= 2.0
    while t < 0.5 - 0.001:
        parts.append('atempo=0.5')
        t /= 0.5
    if abs(t - 1.0) >= 0.02:
        parts.append(f'atempo={round(t, 3)}')
    return ','.join(parts) if parts else None


def _ffmpeg_atempo_chain(mp3_path: str, tempo: float) -> bool:
    """按倍率调整语速（0.25~4.0），不截断音频。"""
    import subprocess as _sp
    filter_str = _build_atempo_filter_chain(tempo)
    if not filter_str:
        return False
    tmp = mp3_path + '.adj.mp3'
    r = _sp.run(
        ['ffmpeg', '-y', '-i', mp3_path,
         '-filter:a', filter_str, '-b:a', '128k', tmp],
        capture_output=True, text=True, errors='replace')
    if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 100:
        os.replace(tmp, mp3_path)
        return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def _ffmpeg_atempo(mp3_path: str, tempo: float) -> bool:
    """兼容旧调用：链式 atempo，不截断。"""
    return _ffmpeg_atempo_chain(mp3_path, tempo)


def adjust_speed(
    mp3_path: str,
    target_duration: float,
    max_tempo: float = 1.5,
) -> bool:
    """配音超过目标时长时加速（最多 max_tempo 倍），绝不截断。"""
    actual = get_audio_duration(mp3_path)
    if actual <= 0 or actual <= target_duration:
        return False
    ratio = actual / target_duration
    tempo = min(ratio, max_tempo)
    if tempo <= 1.02:
        return False
    return _ffmpeg_atempo_chain(mp3_path, tempo)


def fit_audio_no_truncate(
    mp3_path: str,
    target_duration: float,
    *,
    max_tempo: float = 3.5,
) -> dict:
    """
    超窗只加速、绝不截断；在 max_tempo 内尽量压进字幕时间窗。
    仍略超窗则保留全长（语义完整优先于严格对齐）。
    """
    target_duration = max(0.3, target_duration)
    actual = get_audio_duration(mp3_path)
    if actual <= 0:
        return {
            'action': 'none', 'actual_sec': 0,
            'window_sec': round(target_duration, 3),
            'over_window': False, 'tempo_applied': 1.0,
        }
    if actual <= target_duration + 0.03:
        return {
            'action': 'unchanged', 'actual_sec': round(actual, 3),
            'window_sec': round(target_duration, 3),
            'over_window': False, 'tempo_applied': 1.0,
        }

    ratio = actual / target_duration
    tempo = min(ratio, max_tempo)
    action = 'unchanged'
    if tempo > 1.02 and _ffmpeg_atempo_chain(mp3_path, tempo):
        action = 'speedup'

    actual_after = get_audio_duration(mp3_path) or actual
    over = actual_after > target_duration + 0.05
    return {
        'action': action,
        'actual_sec': round(actual_after, 3),
        'window_sec': round(target_duration, 3),
        'over_window': over,
        'tempo_applied': round(tempo, 3) if action != 'unchanged' else 1.0,
    }


_speed_adjust_stats: list[dict] = []


def _reset_speed_adjust_stats() -> None:
    _speed_adjust_stats.clear()


def _record_speed_adjust(fit: dict) -> None:
    _speed_adjust_stats.append(fit)


def print_speed_adjust_summary(cfg: dict | None = None) -> None:
    if not _speed_adjust_stats:
        return
    c = cfg or _load_dub_pipeline_config()
    max_t = float(c.get('quick_dub_max_tempo', 3.5))
    sped = sum(1 for x in _speed_adjust_stats if x.get('action') == 'speedup')
    still_over = sum(1 for x in _speed_adjust_stats if x.get('over_window'))
    print(f'  超窗加速：{sped} 条（最高 {max_t:g}×，不截断音频）')
    if still_over:
        print(
            f'  仍略超窗：{still_over} 条（保留全文朗读；'
            f'正式流程请缩短字幕/Gemini 压缩后再配）')


def fit_audio_to_window(
    mp3_path: str,
    target_duration: float,
    *,
    max_tempo: float = 1.5,
    min_tempo: float = 0.82,
    fill_window: bool = True,
    never_truncate: bool = False,
) -> None:
    """
    把单条配音拉伸/压缩到字幕时间窗内均匀播完。
    过长则加速；never_truncate 时只加速、不截断，且可更高倍率。
    """
    if never_truncate:
        fit = fit_audio_no_truncate(
            mp3_path, target_duration, max_tempo=max(max_tempo, 3.5))
        _record_speed_adjust(fit)
        return
    if target_duration <= 0.3:
        return
    actual = get_audio_duration(mp3_path)
    if actual <= 0:
        return
    ratio = actual / target_duration
    if ratio > 1.02:
        adjust_speed(mp3_path, target_duration, max_tempo=max_tempo)
        return
    if fill_window and ratio < 0.88:
        tempo = max(min_tempo, ratio)
        _ffmpeg_atempo_chain(mp3_path, tempo)


def _apply_cue_speed_adjust(mp3_path: str, cue: dict) -> None:
    cfg = _load_dub_pipeline_config()
    if bool(cfg.get('speech_window_soft', True)) and bool(
            cfg.get('speech_activity_align')):
        return
    target_dur = cue['end'] - cue['start']
    never_truncate = bool(cfg.get('dub_never_truncate', False))
    if never_truncate or os.environ.get(
            'TUBEGET_QUICK_BILINGUAL', '').strip().lower() in (
            '1', 'true', 'yes'):
        max_t = float(cfg.get('quick_dub_max_tempo', 3.5))
        fit = fit_audio_no_truncate(mp3_path, target_dur, max_tempo=max_t)
        _record_speed_adjust(fit)
        return
    if _speech_strict_window_enabled(cfg):
        wp = cue.get('window_params') or {}
        end_buf = float(cfg.get('speech_activity_end_buffer_sec', 0.0))
        speak = float(wp.get('speak_duration') or max(0.25, target_dur - end_buf))
        fit_audio_strict_window(
            mp3_path, target_dur,
            speak_duration=speak,
            max_tempo=float(cfg.get('speech_activity_max_tempo', 1.5)),
            hard_max_tempo=float(cfg.get('speech_activity_hard_max_tempo', 2.0)),
        )
        return
    fit_audio_to_window(
        mp3_path, target_dur,
        fill_window=bool(cfg.get('dub_fill_window', True)),
        never_truncate=never_truncate,
    )


async def synth_all(cues: list, voice_id: str, rate: str,
                    tmp_dir: str, use_local_fallback: bool = True,
                    only_indices: set | None = None,
                    subtitle_path: str | None = None,
                    status_meta: dict | None = None) -> tuple[list, list[int]]:
    from cue_synth_eligibility import (
        scan_cues_for_synth,
        print_unsynth_report,
        apply_unsynth_cues,
        is_synth_eligible,
        extract_chinese,
        tts_text_from_cue,
    )

    if _is_hybrid_engine():
        from run_batch_course import synth_all_hybrid
        print('  引擎：Edge 多轮 → Kokoro 补全（失败句写入 kokoro-samples 试听目录）')
        all_results = await synth_all_hybrid(
            cues, voice_id, rate, tmp_dir,
            subtitle_path=subtitle_path,
            status_meta=status_meta,
        )
        results = [
            (i, s, e, p) for i, s, e, p in all_results
            if p and os.path.exists(p) and os.path.getsize(p) > 500
        ]
        failed = [
            i for i, s, e, p in all_results
            if not p or not os.path.exists(p) or os.path.getsize(p) <= 500
        ]
        results.sort(key=lambda x: x[0])
        return results, failed

    if os.environ.get('TUBEGET_QUICK_BILINGUAL', '').strip().lower() in (
            '1', 'true', 'yes'):
        _fit_dub_sub = os.environ.get('TUBEGET_FIT_DUB', '').strip().lower() in (
            '1', 'true', 'yes')
        if not _fit_dub_sub and subtitle_path and 'fit-dub' in Path(
                subtitle_path).name.lower():
            _fit_dub_sub = True
        if not _fit_dub_sub:
            # 路线 A：逐句 Edge（合并后的每条字幕一次 TTS），禁止 420 字大段直送
            print_edge_scheduler_config(load_edge_scheduler_config())

            def _on_edge_success(_idx: int, mp3_path: str, cue: dict) -> None:
                _apply_cue_speed_adjust(mp3_path, cue)

            def _tts_text(c: dict) -> str:
                return tts_text_from_cue(c)

            results, failed, _stats = await run_edge_scheduler(
                cues,
                voice_id,
                rate,
                tmp_dir,
                only_indices=only_indices,
                subtitle_path=subtitle_path,
                status_meta=status_meta,
                text_extractor=_tts_text,
                on_success=_on_edge_success,
            )
            return results, failed

    if _should_use_edge_scheduler():
        unsynth_scan = scan_cues_for_synth(
            cues, text_extractor=lambda c: c.get('text', ''))
        print_unsynth_report(unsynth_scan)
        unsynth_results: list[tuple] = []

        def _mp3(i: int) -> str:
            return os.path.join(tmp_dir, f'cue_{i:04d}.mp3')

        def _on_unsynth(i: int, mp3: str, _reason: str) -> None:
            c = cues[i]
            unsynth_results.append((i, c['start'], c['end'], mp3))

        apply_unsynth_cues(
            cues, unsynth_scan, tmp_dir,
            mp3_path_fn=_mp3,
            on_applied=_on_unsynth,
        )

        print_edge_scheduler_config(load_edge_scheduler_config())

        def _on_edge_success(_idx: int, mp3_path: str, cue: dict) -> None:
            _apply_cue_speed_adjust(mp3_path, cue)

        def _tts_text(c: dict) -> str:
            from cue_synth_eligibility import tts_text_from_cue
            return tts_text_from_cue(c)

        results, failed, _stats = await run_edge_scheduler(
            cues,
            voice_id,
            rate,
            tmp_dir,
            only_indices=only_indices,
            subtitle_path=subtitle_path,
            status_meta=status_meta,
            text_extractor=_tts_text,
            on_success=_on_edge_success,
            skip_indices=frozenset(unsynth_scan.unsynth.keys()),
        )
        results = list(results) + unsynth_results
        for i in failed:
            if i in unsynth_scan.unsynth:
                continue
            if not use_local_fallback:
                continue
            if not is_synth_eligible(cues[i].get('text', '')):
                continue
            cue = cues[i]
            mp3 = os.path.join(tmp_dir, f'cue_{i:04d}.mp3')
            tts_text = tts_text_from_cue(cue)
            if not tts_text:
                continue
            loop = asyncio.get_event_loop()
            ok = await loop.run_in_executor(
                None, synth_one_local, tts_text, mp3, rate)
            if ok:
                _apply_cue_speed_adjust(mp3, cue)
                results.append((i, cue['start'], cue['end'], mp3))
                failed = [x for x in failed if x != i]
                print(f'\n    第 {i + 1} 条 本地语音补全 OK')
        results.sort(key=lambda x: x[0])
        failed = [
            i for i in failed
            if i not in unsynth_scan.unsynth
            and is_synth_eligible(cues[i].get('text', ''))
        ]
        return results, failed

    semaphore = asyncio.Semaphore(2)
    results   = []
    failed    = []
    lock      = asyncio.Lock()
    done_count = [0]
    total_jobs = len(cues)

    async def worker(i, cue):
        mp3 = os.path.join(tmp_dir, f'cue_{i:04d}.mp3')
        if only_indices is not None and i not in only_indices:
            if os.path.exists(mp3) and os.path.getsize(mp3) > 500:
                async with lock:
                    done_count[0] += 1
                return ('ok', i, cue['start'], cue['end'], mp3)
            return ('skip', i, None, None, None)

        tts_text = tts_text_from_cue(cue)
        if not tts_text:
            return ('fail', i, None, None, None)

        async with semaphore:
            ok = await synth_one(
                tts_text, voice_id, rate, mp3,
                use_local_fallback=use_local_fallback,
                cue_index=i,
            )
            async with lock:
                done_count[0] += 1
                pct = done_count[0] / max(total_jobs, 1) * 100
                bar = '█' * int(pct / 5) + '░' * (20 - int(pct / 5))
                print(f'\r  [{bar}] {pct:.0f}%  {done_count[0]}/{total_jobs}',
                      end='', flush=True)
            if ok:
                _apply_cue_speed_adjust(mp3, cue)
                return ('ok', i, cue['start'], cue['end'], mp3)
            return ('fail', i, None, None, None)

    tasks = [worker(i, cue) for i, cue in enumerate(cues)]
    for coro in asyncio.as_completed(tasks):
        tag, i, start, end, mp3 = await coro
        if tag == 'ok':
            results.append((i, start, end, mp3))
        elif tag == 'fail':
            failed.append(i)

    print()

    for retry_round in range(2):
        if not failed:
            break
        print(f'  自动重试第 {retry_round + 1} 轮（{len(failed)} 条失败）...')
        await asyncio.sleep(3)
        still_failed = []
        for i in failed:
            cue = cues[i]
            mp3 = os.path.join(tmp_dir, f'cue_{i:04d}.mp3')
            tts_text = tts_text_from_cue(cue)
            if not tts_text:
                still_failed.append(i)
                continue
            ok = await synth_one(
                tts_text, voice_id, rate, mp3,
                use_local_fallback=False, cue_index=i)
            if ok:
                _apply_cue_speed_adjust(mp3, cue)
                results.append((i, cue['start'], cue['end'], mp3))
                print(f'    第 {i+1} 条重试成功')
            else:
                still_failed.append(i)
            await asyncio.sleep(1)
        failed = still_failed
        await asyncio.sleep(2)
    if use_local_fallback:
        print('  （Edge 失败时已尝试 Windows 本地语音补全）')
    results.sort(key=lambda x: x[0])
    return results, failed

# ── 音频合并（按时间轴放置）────────────────────────────────

def _prepare_cue_segment(seg, fade_ms: int | None = None):
    """合并叠加时不做淡入淡出，保持自然口播音量。"""
    _ = fade_ms
    return seg


def _resolve_playback_starts(
    results: list,
    min_gap: float = 0.08,
    anchor_to_window: bool | None = None,
    strict_window: bool | None = None,
) -> list[tuple[int, float, float, str, float]]:
    """
    计算每条 MP3 的播放起点。
    strict_window=True（Speech Activity）：始终从窗起点播，不顺延。
    anchor_to_window=True：从字幕窗起点开播，在 [start,end] 内均匀铺满。
    否则：若会抢话则顺延到上一句结束之后。
    返回 (idx, orig_start, orig_end, mp3_path, play_start)。
    """
    cfg = _load_dub_pipeline_config()
    if strict_window is None:
        strict_window = _speech_strict_window_enabled(cfg)
    if anchor_to_window is None:
        anchor_to_window = bool(cfg.get('dub_anchor_window', True))

    items = []
    for idx, start, end, mp3_path in results:
        if not mp3_path or not os.path.exists(mp3_path):
            continue
        dur = get_audio_duration(mp3_path)
        if dur <= 0:
            dur = max(0.4, end - start)
        items.append({
            'idx': idx, 'start': start, 'end': end,
            'mp3': mp3_path, 'dur': dur,
        })
    if not items:
        return []

    items.sort(key=lambda x: (x['start'], x['idx']))
    prev_play_end = 0.0
    shifted = 0
    anchored = 0
    out: list[tuple[int, float, float, str, float]] = []
    for it in items:
        window = it['end'] - it['start']
        if strict_window:
            play_start = it['start']
            anchored += 1
        elif anchor_to_window and window > 0.25:
            play_start = it['start']
            if prev_play_end + min_gap > play_start + 0.04:
                bleed = prev_play_end + min_gap - play_start
                if it['dur'] + bleed <= window + 0.15:
                    play_start = prev_play_end + min_gap
                    shifted += 1
                else:
                    anchored += 1
            else:
                anchored += 1
        else:
            # 软窗口：仅当本句时间窗起点已晚于上一句结束，才锚定窗起点；
            # 否则顺延，避免同一起点多句抢话。
            if it['start'] >= prev_play_end + min_gap - 0.02:
                play_start = it['start']
            else:
                play_start = prev_play_end + min_gap
                if play_start > it['start'] + 0.05:
                    shifted += 1
        prev_play_end = play_start + it['dur']
        out.append((it['idx'], it['start'], it['end'], it['mp3'], play_start))

    if strict_window:
        print(f'  窗口锚定：{len(items)} 条从原时间窗起点播放（超窗仅加速，不截断音频）')
    else:
        if anchored:
            print(f'  窗口锚定：{anchored} 条从字幕窗起点均匀播放')
        if shifted:
            print(f'  防重叠：{shifted} 条顺延播放（避免抢话）')
    return out


def merge_audio(results: list, skipped_beeps: list,
                total_dur: float, out_path: str,
                placements: list | None = None,
                dub_cues: list | None = None):
    try:
        from pydub import AudioSegment
    except ImportError:
        print('❌ 缺少 pydub：pip install pydub')
        sys.exit(1)

    print(f'  合并音频（总时长 {total_dur:.1f}s）...', flush=True)
    total_ms = int(total_dur * 1000) + 2000
    track    = AudioSegment.silent(duration=total_ms, frame_rate=44100)

    # 放置配音片段（完整 mp3 叠加，不因时间窗截断）
    skipped = 0
    cfg = _load_dub_pipeline_config()
    if placements is None:
        placements = _resolve_playback_starts(results)
    valid_placements = [
        p for p in placements
        if p[3] and os.path.exists(p[3])
    ]
    n_overlay = len(valid_placements)
    if n_overlay:
        print(
            f'  [进行中] 叠加 {n_overlay} 条配音到轨道'
            f'（长视频可能需数分钟）…',
            flush=True,
        )
    done = 0
    for (idx, orig_start, orig_end, mp3_path, play_start) in placements:
        if not os.path.exists(mp3_path):
            skipped += 1
            continue
        seg = AudioSegment.from_mp3(mp3_path)
        seg = seg.set_frame_rate(44100).set_channels(1)
        seg = _prepare_cue_segment(seg)
        track = track.overlay(seg, position=int(play_start * 1000))
        done += 1
        if n_overlay and (done % 10 == 0 or done == n_overlay):
            print(f'\r    叠加配音 {done}/{n_overlay}', end='', flush=True)
    if n_overlay:
        print(flush=True)

    _ = skipped_beeps  # 已废弃：敏感词改为直接删除，不再插入滴声

    print('  [进行中] 归一化音量并导出 MP3…', flush=True)
    # 归一化：目标约 -3 dB（避免 0 dB 削波失真）
    try:
        peak = track.max_dBFS
        if peak != float('-inf'):
            if peak < -3.0:
                track = track.apply_gain(min(15.0, -3.0 - peak))
            elif peak > -2.5:
                track = track.apply_gain(-3.0 - peak)
    except Exception:
        pass

    track = track.set_frame_rate(44100).set_channels(2)
    track.export(out_path, format='mp3', bitrate='192k')
    print(f'  ✅ 输出：{out_path}')
    if skipped:
        print(f'  ⚠️  {skipped} 条合成失败（位置为静音）')


def print_local_voices_help():
    """打印本机可用中文语音列表。"""
    setup_console_encoding()
    voices = find_local_voices(zh_only=True)
    if not voices:
        print('未找到中文本地语音')
        return
    print('本机中文语音：')
    for v in voices:
        g = {'male': '男', 'female': '女'}.get(v['gender'], '?')
        simp = '简' if v['simplified'] else '繁'
        print(f"  [{g}/{simp}] key={v['key']:8}  {v['name']}")
    vk, vg = _load_local_voice_prefs()
    import pyttsx3
    eng = pyttsx3.init()
    vid = resolve_local_voice_id(eng, voice_key=vk, gender=vg)
    try:
        eng.stop()
    except Exception:
        pass
    print(f'\n当前配置将使用：local_voice={vk}  gender={vg}')
    print(f'  voice id: {vid}')

# ── 主流程 ──────────────────────────────────────────────────

async def main():
    setup_console_encoding()
    parser = argparse.ArgumentParser(description='字幕 → 时间轴配音 MP3（含口水词过滤）')
    parser.add_argument('subtitle',       help='字幕文件（.srt 或 .vtt）')
    parser.add_argument('--voice',        default='晓晓', help='配音员（默认晓晓）')
    parser.add_argument('--rate',         default='+0%',  help='语速（默认+0%%）')
    parser.add_argument('--output',       default='',     help='输出路径')
    parser.add_argument('--no-filter',    action='store_true', help='关闭口水词过滤')
    parser.add_argument('--filler-words', default='',
                        help='追加口水词，逗号分隔，如 "对吧,是吧,好的好的"')
    parser.add_argument('--beep-words',   default='',
                        help='追加敏感词（直接删除），逗号分隔，如 "妈的,滚蛋"')
    parser.add_argument('--no-local-fallback', action='store_true', default=True,
                        help='关闭 Edge 失败时的 Windows 本地语音补全（默认关闭）')
    parser.add_argument('--require-complete', action='store_true',
                        help='须全部 Edge 成功才输出 MP3；否则保留缓存并退出码 2')
    parser.add_argument('--retry-failed', action='store_true',
                        help='仅重试上次未成功的句（需配合 --require-complete 缓存）')
    parser.add_argument('--no-gemini', action='store_true',
                        help='跳过 Gemini 字幕优化/翻译（直配中文）')
    args = parser.parse_args()

    try:
        import edge_tts
    except ImportError:
        print('❌ 请安装 edge-tts：pip install edge-tts')
        sys.exit(1)

    voice_id = VOICE_MAP.get(args.voice, args.voice)
    use_local_fallback = not args.no_local_fallback
    az_key, az_region = _load_azure_config()
    engine_name = f'Azure TTS（{az_region}）' if az_key else 'Edge TTS'
    print(f'\n  配音员：{args.voice}（{voice_id}）')
    print(f'  语速：{args.rate}')
    print(f'  引擎：{engine_name}')
    if use_local_fallback:
        try:
            import pyttsx3  # noqa: F401
            print('🔁  Edge 失败时将自动用 Windows 本地语音补全')
        except ImportError:
            print('⚠️  未安装 pyttsx3，失败句将留静音（pip install pyttsx3 可本地补全）')
            use_local_fallback = False

    # 合并自定义词表
    fillers   = list(FILLER_WORDS)
    sensitive = list(SENSITIVE_WORDS)
    if args.filler_words:
        fillers += [w.strip() for w in args.filler_words.split(',') if w.strip()]
    if args.beep_words:
        sensitive += [w.strip() for w in args.beep_words.split(',') if w.strip()]

    filter_on = not args.no_filter
    if filter_on:
        print(f'🔇  口水词过滤：开启（{len(fillers)} 个）')
        print(f'🔕  敏感词删除：开启（{len(sensitive)} 个，不插入滴声）')
    else:
        print('🔇  口水词过滤：关闭')

    # 解析字幕
    if not os.path.exists(args.subtitle):
        print(f'❌ 找不到字幕：{args.subtitle}')
        sys.exit(1)

    pipeline_t0 = time.perf_counter()

    raw_cues = load_subtitle(args.subtitle)
    if not raw_cues:
        print('❌ 字幕为空')
        sys.exit(1)

    if _needs_coalesce_fragmented_cues(raw_cues):
        raw_cues, co_stats = coalesce_fragmented_bilingual_cues(raw_cues)
        print(
            f'  en-zh-merge 分片合并：{co_stats["before"]} 条 → {co_stats["after"]} 条'
            f'（仅英文尾段丢弃 {co_stats.get("skipped_en_only_tail", 0)}）')
        if not raw_cues:
            print('❌ 合并后无含中文条目')
            sys.exit(1)

    if is_bilingual_subtitle_path(args.subtitle):
        from cue_synth_eligibility import chinese_speech_text
        print('  双语字幕：取中文行全文配音（保留 Bedrock/RAG 等术语，不剥英文词）')
        kept: list[dict] = []
        skipped_no_zh = 0
        for cue in raw_cues:
            zh = chinese_speech_text(cue.get('text', ''))
            if not zh.strip():
                skipped_no_zh += 1
                continue
            kept.append({**cue, 'text': zh, 'tts_text': zh})
        if skipped_no_zh:
            print(f'  无中文跳过：{skipped_no_zh} 条')
        raw_cues = kept
        if not raw_cues:
            print('❌ 双语字幕中未找到可配音的中文行')
            sys.exit(1)
        preview = raw_cues[0]['text'][:40]
        print(f'  配音样例（第1条仅中文）：{preview}{"…" if len(raw_cues[0]["text"]) > 40 else ""}')

    # 逐条清洗（不在此处做短句合并，避免与烧录字幕断句不一致）
    processed_list = []
    skipped_count  = 0
    for cue in raw_cues:
        processed = process_cue(cue, fillers, sensitive, filter_on)
        if processed is None:
            skipped_count += 1
        else:
            processed_list.append(processed)

    if filter_on:
        print(f'  过滤后：{len(processed_list)} 条片段 | '
              f'{skipped_count} 条跳过（口水词/敏感词删空）')

    pipeline_cfg = _load_dub_pipeline_config()
    if pipeline_cfg.get('generate_sync_subtitles'):
        base_out = video_base_from_zh_subtitle(args.subtitle)
        parent   = Path(args.subtitle).parent
        sync_zh_path  = str(parent / f'{base_out}.sync-zh.srt')
        sync_bil_path = str(parent / f'{base_out}.sync-bilingual.srt')
        print('\n  生成显示字幕（按标点断句，去标点）')
        display_cues = create_display_cues(processed_list)
        write_sync_zh_srt(display_cues, sync_zh_path)
        en_path = find_en_srt(args.subtitle)
        if en_path:
            print(f'  对齐英文字幕：{Path(en_path).name}')
            generate_sync_bilingual_srt(display_cues, en_path, sync_bil_path)
        else:
            print('  未找到 .en.srt，仅生成中文字幕')
    else:
        print('\n  仅用文件夹内字幕配音（不生成 sync-zh / sync-bilingual）')

    # 配音：合并相邻句 → 按 N 句一组一次 TTS（避免一句一顿）
    _sub_name = Path(args.subtitle).name.lower()
    _fit_dub = 'fit-dub' in _sub_name
    print('\n  生成配音时间轴（批量句合并）')
    if _fit_dub:
        dub_merged = processed_list
        print(f'  fit-dub：{len(dub_merged)} 条独立 TTS（短口播稿不合并）')
        os.environ['TUBEGET_FIT_DUB'] = '1'
    else:
        os.environ.pop('TUBEGET_FIT_DUB', None)
        dub_merged = merge_cues(
            processed_list,
            max_gap=float(pipeline_cfg.get('merge_max_gap', 1.2)),
            max_duration=float(pipeline_cfg.get('merge_max_duration', 7.0)),
            hard_max_duration=float(pipeline_cfg.get('merge_hard_max_duration', 12.0)),
            for_subtitle=False,
            break_on_sentence_end=bool(
                pipeline_cfg.get('merge_break_on_sentence_end', True)),
        )
    if pipeline_cfg.get('dub_split_by_sentence', False):
        dub_merged = split_cues_by_sentence_punctuation(dub_merged)
    batch_n = int(pipeline_cfg.get('dub_batch_merge_size', 3) or 0)
    if pipeline_cfg.get('edge_segment_mode', True):
        print('  Edge 分段模式：由 edge_segment_scheduler 按字数切段，跳过句数批量合并')
    elif batch_n >= 2:
        dub_merged = merge_cues_in_batches(dub_merged, batch_n)
    else:
        print('  Edge 逐句合成（预览：禁用分段切割，避免一词一段）')
    dub_merged = normalize_subtitle_timeline(dub_merged)
    dub_cues = dub_merged

    if os.environ.get('TUBEGET_QUICK_BILINGUAL', '').strip().lower() in (
            '1', 'true', 'yes'):
        print(
            f'  配音单元：{len(dub_cues)} 段'
            f'（由 {len(processed_list)} 条字幕合并，每段一次连贯 TTS）')

    if not dub_cues:
        print('❌ 没有可配音的字幕')
        sys.exit(1)

    subtitle_prepare_sec = time.perf_counter() - pipeline_t0

    if args.output:
        out_path = args.output
    else:
        out_path = str(dub_output_mp3_path(args.subtitle))

    require_complete = args.require_complete
    use_persistent   = require_complete or args.retry_failed
    work_dir         = dub_work_dir(args.subtitle)
    sub_mtime        = int(Path(args.subtitle).stat().st_mtime)
    only_indices     = None

    pipe_fp = dub_pipeline_fingerprint(pipeline_cfg)

    if use_persistent:
        work_dir.mkdir(parents=True, exist_ok=True)
        prev = load_dub_status(args.subtitle)
        if prev and prev.get('pipeline_fingerprint') not in (None, pipe_fp):
            print('  配音合并策略已变更，清空缓存并全量重合成')
            shutil.rmtree(work_dir, ignore_errors=True)
            work_dir.mkdir(parents=True, exist_ok=True)
            args.retry_failed = False
            prev = None
        if prev and prev.get('subtitle_mtime') not in (None, sub_mtime):
            print('  字幕已更新，清空配音缓存并全量重合成')
            shutil.rmtree(work_dir, ignore_errors=True)
            work_dir.mkdir(parents=True, exist_ok=True)
            args.retry_failed = False
        elif args.retry_failed and prev and not prev.get('complete'):
            only_indices = set(prev.get('failed_indices', []))
            from cue_synth_eligibility import scan_cues_for_synth, print_unsynth_report
            pre_scan = scan_cues_for_synth(
                dub_cues, text_extractor=lambda c: c.get('text', ''))
            if pre_scan.unsynth:
                print_unsynth_report(pre_scan)
                dropped = sorted(only_indices & set(pre_scan.unsynth.keys()))
                if dropped:
                    print(f'  重试列表剔除 {len(dropped)} 条不可合成句')
                only_indices -= set(pre_scan.unsynth.keys())
            if not only_indices:
                print('  无待重试条目，检查已有缓存…')
            else:
                print(f'  仅重试 {len(only_indices)} 条失败句：'
                      f'{sorted(only_indices)[:8]}'
                      f'{"…" if len(only_indices) > 8 else ""}')

    if require_complete:
        print('  模式：须全部合成成功才输出配音（未齐不进入合并/烧录）')

    from cue_synth_eligibility import scan_cues_for_synth, print_unsynth_report
    pre_scan = scan_cues_for_synth(
        dub_cues, text_extractor=lambda c: c.get('text', ''))
    if not _is_hybrid_engine():
        print_unsynth_report(pre_scan)

    skip_gemini = args.no_gemini or os.environ.get(
        'TUBEGET_NO_GEMINI', '').strip().lower() in ('1', 'true', 'yes')
    if skip_gemini:
        print('\n  [Gemini] 已跳过（直配模式，不翻译/不优化）')
    else:
        print('\n  [Gemini] 字幕优化…')
        from gemini_workflow import run_gemini_screen_and_rewrite
        try:
            dub_cues, _gem_summary = await run_gemini_screen_and_rewrite(
                dub_cues,
                subtitle_path=args.subtitle,
                speech_align=False,
                quiet=False,
            )
        except Exception as e:
            import traceback
            print(f'\n  [警告] Gemini 失败，将用原字幕继续配音：{e}')
            traceback.print_exc()

    print(f'\n⏳ 开始合成 {len(dub_cues)} 条配音...')
    _reset_speed_adjust_stats()

    tts_t0 = time.perf_counter()

    status_meta_pre = {
        'voice': args.voice,
        'voice_id': voice_id,
        'rate': args.rate,
        'total': len(dub_cues),
        'subtitle_mtime': sub_mtime,
        'pipeline_fingerprint': pipe_fp,
        'output_mp3': out_path,
    }

    async def run_synth(tmp_dir: str):
        return await synth_all(
            dub_cues, voice_id, args.rate, tmp_dir,
            use_local_fallback=use_local_fallback,
            only_indices=only_indices,
            subtitle_path=args.subtitle,
            status_meta=status_meta_pre if use_persistent else None,
        )

    if use_persistent:
        results, failed = await run_synth(str(work_dir))
    else:
        # 也用持久目录，确保合并音频时文件还在
        work_dir.mkdir(parents=True, exist_ok=True)
        results, failed = await run_synth(str(work_dir))

    print(f'  合成完成：{len(results)}/{len(dub_cues)} 条字幕单元')

    if failed:
        extra, failed = await last_chance_local_fill(
            dub_cues, failed, args.rate, str(work_dir))
        if extra:
            results.extend(extra)
            results.sort(key=lambda x: x[0])
            print(f'  末轮补全后：{len(results)}/{len(dub_cues)} 条成功')

    tts_sec = time.perf_counter() - tts_t0
    pipeline_perf = {
        'subtitle_prepare_sec': round(subtitle_prepare_sec, 2),
        'translation_sec': None,
        'tts_sec': round(tts_sec, 2),
        'audio_merge_sec': 0.0,
    }

    policy = _load_dub_policy_config()
    total_n = len(dub_cues)
    missing_ratio = len(failed) / max(total_n, 1)
    ignore_missing = can_ignore_missing_dub(len(failed), total_n, policy)

    status_base = {
        'voice': args.voice,
        'voice_id': voice_id,
        'rate': args.rate,
        'total': total_n,
        'success': len(results),
        'failed_indices': sorted(failed),
        'subtitle_mtime': sub_mtime,
        'pipeline_fingerprint': pipe_fp,
        'pipeline_perf': dict(pipeline_perf),
        'output_mp3': out_path,
    }
    if pre_scan.unsynth:
        status_base['unsynth_indices'] = sorted(pre_scan.unsynth.keys())
        status_base['unsynth_stats'] = pre_scan.count_by_reason()

    if require_complete and failed and not ignore_missing:
        prev_st = load_dub_status(args.subtitle)
        write_dub_status(args.subtitle, _merge_dub_status_extra(
            prev_st, {
                **status_base,
                'complete': False,
                'exit_reason': (
                    f'配音未齐：{len(failed)}/{total_n} 条失败，'
                    f'序号 {sorted(failed)[:8]}'),
            }))
        out_mp3 = Path(out_path)
        if out_mp3.exists():
            try:
                out_mp3.unlink()
            except OSError:
                pass
        print(f'\n⏸️  配音未齐（缺 {len(failed)} 条），已保留 {work_dir.name}/ 缓存')
        print('     请先处理其它视频，稍后再运行批量脚本自动补全失败句')
        print(f'     失败序号：{sorted(failed)[:12]}'
              f'{" …" if len(failed) > 12 else ""}')
        sys.exit(2)

    if ignore_missing:
        log_path = log_ignored_missing_dub(
            args.subtitle,
            total=total_n,
            failed=sorted(failed),
            missing_ratio=missing_ratio,
            voice=args.voice,
            note='after_edge_kokoro_local',
        )
        print(f'\n  [可忽略缺句] 缺 {len(failed)} 条（{missing_ratio * 100:.4f}% ≤ 千分之一），'
              f'缺句处静音，继续合并成片')
        print(f'     失败序号（第几句）：{sorted(i + 1 for i in failed)}')
        print(f'     已记入：{log_path.name}')

    if not results:
        prev_st = load_dub_status(args.subtitle)
        write_dub_status(args.subtitle, _merge_dub_status_extra(
            prev_st, {
                **status_base,
                'complete': False,
                'exit_reason': f'全部 TTS 失败（0/{total_n}），请检查 Edge 网络与 Kokoro',
            }))
        print('❌ 全部合成失败，请检查网络与 Python 3.12（py -3.12）')
        sys.exit(1)

    if require_complete:
        results = collect_results_from_work_dir(str(work_dir), dub_cues)
        if len(results) < total_n and not ignore_missing:
            prev_st = load_dub_status(args.subtitle)
            write_dub_status(args.subtitle, _merge_dub_status_extra(
                prev_st, {
                    **status_base,
                    'complete': False,
                    'exit_reason': (
                        f'缓存不完整（{len(results)}/{total_n}），'
                        f'可删除 .dub-work 后重跑'),
                }))
            print(f'❌ 缓存不完整（{len(results)}/{total_n}），请重新运行')
            sys.exit(1)

    end_times = [c['end'] for c in dub_cues]
    total_dur = max(end_times) if end_times else 0
    vid_for_dur = os.environ.get('TUBEGET_VIDEO_FOR_DURATION', '').strip()
    if vid_for_dur and os.path.isfile(vid_for_dur):
        try:
            vd = get_audio_duration(vid_for_dur)  # ffprobe works on video too
            if vd <= 0:
                import subprocess as _sp
                r = _sp.run(
                    ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', vid_for_dur],
                    capture_output=True, text=True, errors='replace')
                vd = float(r.stdout.strip() or 0)
            if vd > total_dur + 0.5:
                print(f'  配音轨长度：字幕末 {total_dur:.1f}s → 视频全长 {vd:.1f}s')
                total_dur = vd
        except Exception:
            pass
    print_speed_adjust_summary(pipeline_cfg)
    merge_t0 = time.perf_counter()
    merge_audio(results, [], total_dur, out_path)
    pipeline_perf['audio_merge_sec'] = round(time.perf_counter() - merge_t0, 2)
    status_base['pipeline_perf'] = dict(pipeline_perf)

    if require_complete:
        prev_st = load_dub_status(args.subtitle)
        final_status = {
            **status_base,
            'complete': True,
            'success': len(results),
        }
        if ignore_missing:
            final_status.update({
                'partial_accept': True,
                'ignored_missing_count': len(failed),
                'ignored_missing_ratio': missing_ratio,
                'failed_indices': sorted(failed),
            })
        else:
            final_status.update({
                'success': total_n,
                'failed_indices': [],
            })
        write_dub_status(args.subtitle, _merge_dub_status_extra(
            prev_st, final_status))
    # 清理缓存目录
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)

    print(f'\n✅ 完成！配音文件：{out_path}')
    print(f'   下一步：python step2_merge_video.py 视频文件 "{out_path}"')

if __name__ == '__main__':
    asyncio.run(main())
