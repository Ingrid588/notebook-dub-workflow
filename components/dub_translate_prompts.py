#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配音专用翻译提示词（前期进框，不靠后期加速补救）。

NotebookLM：无公开批量 API，请用手工 + notebooklm_dub_prompt.txt。
自动化：fit_translate_bilingual.py + GEMINI_API_KEY（与 NotebookLM 同属 Gemini 模型族）。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 口播语速：与 tubeget.config.json speech_activity_chars_per_sec 对齐
DEFAULT_CHARS_PER_SEC = 3.2
DEFAULT_TARGET_FILL = 0.68
DEFAULT_MAX_FILL = 0.88
DEFAULT_MIN_FILL = 0.55

NOTEBOOKLM_DUB_SYSTEM = """你是课程视频的中文【配音台词编辑】，不是字幕翻译员。

任务：每条英文字幕对应一条中文口播稿，必须在给定秒数内自然念完。
英文行保留；只写中文行。输出 SRT 双语块（英文一行 + 中文一行）。

【铁律：翻译时就必须进时间框，禁止指望后期加速】
- 每条单独计算：窗口秒数 = end − start
- 目标汉字 ≈ 窗口 × 3.2 × 0.62（宜短不宜长）
- 上限汉字 ≈ 窗口 × 3.2 × 0.82（绝对不可超过）
- 中文超过上限 = 失败，必须重写更短

【译法：口播摘要，不是完整字幕（参考短摘要风格）】
✓ 只留本句核心，像讲师口头概括一句
✓ 去掉 you know / like I said / actually / so 及英文填充
✓ 去掉「比如…就是一个…」「更好的写法是：」等套话
✓ 长英文可压到 1/4～1/5 长度（例：19 词 → 8 字「它也许会用 Python 求解。」）
✓ 保留 AWS、Bedrock、RAG、Python、思维链、Token 等术语
✓ 句中每个产品/服务专有名词必须在中文点名（CloudWatch、Glue、S3…），禁止泛化为「指标/该服务」
✓ 形容词/副词修饰不能整段省略（high quality→高质/高质量，critical→关键，production ready→生产级）；窗紧缩字不丢义

✗ 禁止逐句译满英文结构
✗ 禁止中文比英文口播量更长
✗ 禁止扩写、禁止完整复述引号内示例（除非本句就是在念例句）

【极短窗 ≤1.2s】本条中文最多 1 个汉字（或仅「。」）；语义全部上提到上一条，上条末尾 ——。
禁止在短窗写 4 字以上（例：✗「和 AWS Glue 进行准备」→ ✓上条含 Glue，本条「。」）。

【格式】
序号
00:00:00,000 --> 00:00:05,000
English unchanged
中文口播
"""

GEMINI_FIT_DUB_INSTRUCTION = """你是专业【配音台词改编】专家。输入 JSON 数组，每条含：
index、en（整句英文）、duration（秒，硬约束）、target_chars、min_chars、max_chars；可选 zh_ref（原译参考）。

请根据 en 写 zh：这是要念出来的口播稿，不是给人看的字幕。

【最高优先级：语义完整 + 进框】
1. 必须是完整一句中文，禁止半句话、截断词（如「生活没意」「改变自」）。
2. 汉字数（不含标点）须落在 min_chars～max_chars；尽量接近 target_chars，避免明显过短留白。
3. 不可超过 max_chars；duration 秒内须能自然念完，禁止指望后期加速补救。
4. 一条一条独立输出，每条只一句中文。

【译法：口播摘要，含义准确】
5. 抓准 en 核心含义；可删填充词、举例、从句，但不可丢关键信息与修饰语（high quality 至少体现为「高质」或「高质量」）。
6. 禁止套话：「比如」「换句话说」「反面教材」。
7. 保留人名、术语与**全部**产品/服务专名（Glue Studio、CloudWatch、data quality 等）；禁止用泛称替代专名（✗仅「指标」✓CloudWatch 指标）。
8. 极短窗（≤1.2s）：本条最多 1 字或「。」，语义上提至上一条；禁止短窗写整句。

【语言】
9. 仅中文与常用标点（，。！？）；不要英文、括号说明。
10. 口语自然，适合课程旁白。
11. 若有 zh_ref：以原译含义为准，压缩/改写为口播稿并满足字数框。

严格只输出 JSON 数组，无 markdown：
[{"index":0,"zh":"..."},{"index":1,"zh":"..."}]"""

GEMINI_FIT_DUB_RETRY_NOTE = """
【重试】上一批部分 zh 仍超 max_chars。请压缩：只留一个核心信息点，删修饰，但须完整一句。"""

GEMINI_FIT_DUB_UNDERFLOW_NOTE = """
【重试】上一批部分 zh 过短（低于 min_chars）。请在不超过 max_chars 前提下写得更饱满，接近 target_chars，含义仍须完整。"""


def window_chars_for_duration(
    duration_sec: float,
    *,
    chars_per_sec: float = DEFAULT_CHARS_PER_SEC,
    target_fill: float = DEFAULT_TARGET_FILL,
    max_fill: float = DEFAULT_MAX_FILL,
    min_fill: float = DEFAULT_MIN_FILL,
) -> dict[str, Any]:
    """由字幕时间窗计算目标/上下限字数。"""
    dur = max(0.3, float(duration_sec))
    end_buf = 0.10 if dur >= 1.0 else 0.05
    speak = max(0.25, dur - end_buf)
    target = max(3, int(speak * chars_per_sec * target_fill))
    max_chars = max(target + 1, int(speak * chars_per_sec * max_fill))
    min_chars = max(2, int(speak * chars_per_sec * min_fill))
    if min_chars >= max_chars:
        min_chars = max(2, max_chars - 1)
    if dur < 1.2:
        max_chars = min(max_chars, max(3, int(dur * chars_per_sec * 0.85)))
        target = min(target, max(3, max_chars - 1))
        min_chars = min(min_chars, max(2, max_chars - 2))
    return {
        'duration': round(dur, 3),
        'speak_duration': round(speak, 3),
        'target_chars': target,
        'min_chars': min_chars,
        'max_chars': max_chars,
    }


def build_gemini_fit_payload_item(
    index: int,
    en: str,
    start: float,
    end: float,
    *,
    chars_per_sec: float = DEFAULT_CHARS_PER_SEC,
) -> dict[str, Any]:
    wp = window_chars_for_duration(end - start, chars_per_sec=chars_per_sec)
    return {
        'index': index,
        'en': (en or '').strip(),
        'duration': wp['duration'],
        'target_chars': wp['target_chars'],
        'max_chars': wp['max_chars'],
    }


def notebooklm_cheat_sheet_example() -> str:
    """供 NotebookLM 对话附带的正反例。"""
    return """
【正反例】
EN (3.8s): So a bad example of a prompt would be write something about books.
✗ 字幕译：比如，"写点关于书的内容"就是一个反面教材。（22字，太长）
✓ 口播：反面例：写点关于书。（9字）

EN (4.4s): It might go off and write some Python code to figure that out, but we've given it guidance on how to
✓ 口播：它也许会用 Python 求解。（8字）
"""


def format_notebooklm_user_message(cues_sample: list[dict] | None = None) -> str:
    """生成可粘贴到 NotebookLM 的单次任务说明。"""
    lines = [NOTEBOOKLM_DUB_SYSTEM, notebooklm_cheat_sheet_example()]
    if cues_sample:
        lines.append('\n【待处理条目（请按 SRT 格式返回全部）】\n')
        for c in cues_sample[:15]:
            wp = window_chars_for_duration(c['end'] - c['start'])
            lines.append(
                f"#{c.get('index', '?')} {c['duration']:.2f}s "
                f"目标≤{wp['target_chars']}字 上限≤{wp['max_chars']}字\n"
                f"EN: {c.get('en', '')[:200]}\n")
    return '\n'.join(lines)


def zh_len(text: str) -> int:
    return len(re.sub(r'\s+', '', text or ''))
