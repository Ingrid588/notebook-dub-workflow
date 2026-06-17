#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕句是否可合成语音：预检、统计、静音占位（跳过 Edge/Kokoro/本地）。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable

# 去掉后无可念内容的字符（空白、中英文标点、括号、emoji 私有区等）
_SPEAKABLE_STRIP_RE = re.compile(
    r'[\s\u3000'
    r'。，！？.!?,、；;：:…—\-–—'
    r'\"\"\'\'\[\]【】（）()《》<>「」『』'
    r'…·•*#@/\\|~`^&'
    r'\u200b-\u200d\ufeff\ue000-\uf8ff'
    r']+',
)

UNSYNTH_REASON_LABELS: dict[str, str] = {
    'empty': '空文本',
    'no_chinese': '无中文',
    'punctuation_only': '仅标点/符号',
}

# 块内是否含汉字（用于识别「中文行」，不用于剔除英文术语）
_HAS_CJK_RE = re.compile(r'[\u4e00-\u9fff]')


def chinese_speech_text(text: str) -> str:
    """
    配音专用：从双语字幕块取要念的正文。

    - 有中文行 → 中文行**整行原样**（保留 Bedrock、RAG、Playground 等英文术语）
    - 与中文行配对的纯英文展示行 → 不另念（避免英中各念一遍）
    - 整块无任何中文行 → 念全部行（仅术语等极少数情况）

    不按「是否英文字母」删减词，只按「有没有中文行」选行。
    """
    lines = [ln.strip() for ln in (text or '').split('\n') if ln.strip()]
    if not lines:
        return ''
    zh_lines = [ln for ln in lines if _HAS_CJK_RE.search(ln)]
    if zh_lines:
        from dub_naming import strip_notebooklm_citation_marks
        return ' '.join(strip_notebooklm_citation_marks(ln) for ln in zh_lines)
    return ' '.join(lines)


def tts_text_from_cue(cue: dict) -> str:
    """合成用文本：优先 cue['tts_text']，否则从 text 提取中文。"""
    raw = cue.get('tts_text')
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    return chinese_speech_text(cue.get('text', '')).strip()


def extract_chinese(text: str) -> str:
    """从字幕块中提取中文（配音/ eligibility 统一入口）。"""
    return chinese_speech_text(text)


def classify_cue_synth(
    text: str,
    *,
    extract_fn: Callable[[str], str] | None = None,
) -> tuple[str | None, str | None]:
    """
    判定一句是否应尝试 TTS。

    返回 (可用于合成的中文, 不可合成原因)。
    原因为 None 表示可合成；否则为 UNSYNTH_REASON_LABELS 的键。
    """
    extract_fn = extract_fn or extract_chinese
    raw = (text or '').strip()
    if not raw:
        return None, 'empty'

    zh = extract_fn(raw)
    if not zh.strip():
        return None, 'no_chinese'

    speakable = _SPEAKABLE_STRIP_RE.sub('', zh)
    if not speakable:
        return None, 'punctuation_only'

    if not re.search(r'[\u4e00-\u9fff]', speakable):
        if not re.search(r'[A-Za-z0-9]', speakable):
            return None, 'no_chinese'

    return zh, None


def is_synth_eligible(
    text: str,
    *,
    extract_fn: Callable[[str], str] | None = None,
) -> bool:
    return classify_cue_synth(text, extract_fn=extract_fn)[1] is None


@dataclass
class UnsynthScan:
    total: int
    unsynth: dict[int, str] = field(default_factory=dict)
    samples: dict[str, list[tuple[int, str]]] = field(default_factory=dict)

    @property
    def synth_indices(self) -> list[int]:
        skip = set(self.unsynth)
        return [i for i in range(self.total) if i not in skip]

    def count_by_reason(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for reason in self.unsynth.values():
            out[reason] = out.get(reason, 0) + 1
        return out


def scan_cues_for_synth(
    cues: list,
    *,
    text_extractor: Callable[[dict], str] | None = None,
    extract_fn: Callable[[str], str] | None = None,
    max_sample_per_reason: int = 6,
) -> UnsynthScan:
    """扫描全部 cue，找出不可合成语音的序号及原因。"""
    get_text = text_extractor or (lambda c: c.get('text', ''))
    scan = UnsynthScan(total=len(cues))

    for i, cue in enumerate(cues):
        _, reason = classify_cue_synth(get_text(cue), extract_fn=extract_fn)
        if not reason:
            continue
        scan.unsynth[i] = reason
        samples = scan.samples.setdefault(reason, [])
        if len(samples) < max_sample_per_reason:
            preview = get_text(cue).replace('\n', ' ')[:48]
            samples.append((i, preview))

    return scan


def format_unsynth_line(cue_no: int, preview: str) -> str:
    p = preview.strip() or '（空）'
    if len(p) > 36:
        p = p[:36] + '…'
    return f'第 {cue_no} 句「{p}」'


def print_unsynth_report(scan: UnsynthScan) -> None:
    """打印不可合成句统计（供批量日志）。"""
    n = len(scan.unsynth)
    if n <= 0:
        return
    print(f'\n【不可合成句】共 {n} 条（跳过 Edge/Kokoro/本地，成片处为静音）')
    for reason, count in sorted(
        scan.count_by_reason().items(),
        key=lambda x: (-x[1], x[0]),
    ):
        label = UNSYNTH_REASON_LABELS.get(reason, reason)
        print(f'  · {label}：{count} 条')
        for idx, preview in scan.samples.get(reason, [])[:5]:
            print(f'      - {format_unsynth_line(idx + 1, preview)}')
        extra = count - len(scan.samples.get(reason, []))
        if extra > 0:
            print(f'      … 另有 {extra} 条')


def write_silence_cue_mp3(out_path: str, duration_sec: float) -> bool:
    """为不可合成句写入静音占位 mp3（合并时间轴用）。"""
    try:
        from pydub import AudioSegment
    except ImportError:
        return False

    ms = max(500, min(int(max(duration_sec, 0.5) * 1000), 30000))
    try:
        seg = AudioSegment.silent(duration=ms, frame_rate=24000)
        seg.export(out_path, format='mp3', bitrate='128k')
    except Exception:
        return False
    return os.path.exists(out_path) and os.path.getsize(out_path) > 80


def apply_unsynth_cues(
    cues: list,
    scan: UnsynthScan,
    work_dir: str,
    *,
    mp3_path_fn: Callable[[int], str],
    on_applied: Callable[[int, str, str], None] | None = None,
    skip_if_mp3_exists: bool = True,
) -> int:
    """
    为不可合成句写入静音 mp3。
    on_applied(index, mp3_path, reason) 可选回调（用于更新 by_i / status）。
    返回新处理的条数。
    """
    os.makedirs(work_dir, exist_ok=True)
    applied = 0
    for i, reason in sorted(scan.unsynth.items()):
        mp3 = mp3_path_fn(i)
        if skip_if_mp3_exists and os.path.exists(mp3) and os.path.getsize(mp3) > 500:
            if on_applied:
                on_applied(i, mp3, reason)
            continue
        cue = cues[i]
        dur = max(0.08, float(cue.get('end', 0)) - float(cue.get('start', 0)))
        if not write_silence_cue_mp3(mp3, dur):
            continue
        applied += 1
        if on_applied:
            on_applied(i, mp3, reason)
    return applied


def filter_synth_eligible_indices(
    indices: list[int],
    cues: list,
    *,
    text_extractor: Callable[[dict], str] | None = None,
) -> tuple[list[int], list[int]]:
    """从待处理序号中剔除不可合成句。返回 (可合成, 不可合成)。"""
    get_text = text_extractor or (lambda c: c.get('text', ''))
    ok: list[int] = []
    skip: list[int] = []
    for i in indices:
        if is_synth_eligible(get_text(cues[i])):
            ok.append(i)
        else:
            skip.append(i)
    return ok, skip
