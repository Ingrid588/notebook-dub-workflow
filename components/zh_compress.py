#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配音中文本地压缩：内置规则 + 可选 AI 一次性生成的策略 JSON。

思路：前期用少量 token 让 AI 总结「口播压缩策略」，之后长句用脚本缩短，
不必每条都调 Gemini。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).parent.resolve()
DEFAULT_STRATEGY_PATH = HERE / 'tubeget.zh_strategy.json'
GEMINI_STRATEGY_YAML = HERE / 'gemini-code-1780751633188.yaml'

# 内置口播压缩规则（0 token，全课程通用）
BUILTIN_STRATEGY: dict = {
    'version': 1,
    'source': 'builtin',
    'drop_phrases': [
        '我们需要', '你可以', '您可以', '请您', '实际上', '基本上',
        '一般来说', '换句话说', '需要注意的是', '在这种情况下',
        '与此同时', '与此同时，', '因此，', '所以，',
    ],
    'literal_replacements': [
        ('进行一个', ''),
        ('进行', ''),
        ('来实现', ''),
        ('以便', ''),
        ('从而', ''),
        ('的话', ''),
        ('的就是', '是'),
        ('也就是说', ''),
        ('我们可以', ''),
        ('你需要', '你'),
        ('您需要', '您'),
    ],
    'regex_replacements': [
        (r'，并且', '，'),
        (r'，而且', '，'),
        (r'，同时', '，'),
        (r'的是', '是'),
        (r'了的', '的'),
        (r'[\s　]+', ''),
    ],
}

STRATEGY_BUILD_PROMPT = """你是课程配音字幕编辑。下面是一批「英文不长、中文偏长、念不完」的双语样本。

不要逐条重写。请总结一套可复用的「中文口播压缩策略」，供本地脚本批量处理类似长句。

要求：
1. drop_phrases：可安全删除的赘词/套话（字符串列表，从长到短）
2. literal_replacements：[[原短语, 替换为]]，替换可为空字符串表示删除
3. regex_replacements：[[正则, 替换]]，Python re 语法，用于去冗余结构
4. notes：一两句说明（中文）

策略应：保留核心信息、适合朗读、偏口语；不要破坏专有名词（AWS、API 等）。

严格只输出 JSON 对象，无 markdown：
{"drop_phrases":[],"literal_replacements":[],"regex_replacements":[],"notes":"..."}"""


def zh_char_count(text: str) -> int:
    return len(re.sub(r'\s+', '', text))


def merge_strategies(*parts: dict | None) -> dict:
    """合并多个策略（后者追加，去重）。"""
    out = {
        'version': 1,
        'source': 'merged',
        'drop_phrases': [],
        'literal_replacements': [],
        'regex_replacements': [],
        'notes': '',
    }
    seen_drop: set[str] = set()
    seen_lit: set[str] = set()
    seen_re: set[str] = set()
    for part in parts:
        if not part:
            continue
        for p in part.get('drop_phrases') or []:
            p = str(p).strip()
            if p and p not in seen_drop:
                seen_drop.add(p)
                out['drop_phrases'].append(p)
        for pair in part.get('literal_replacements') or []:
            if not pair or len(pair) < 2:
                continue
            key = str(pair[0])
            if key not in seen_lit:
                seen_lit.add(key)
                out['literal_replacements'].append([str(pair[0]), str(pair[1])])
        for pair in part.get('regex_replacements') or []:
            if not pair or len(pair) < 2:
                continue
            key = str(pair[0])
            if key not in seen_re:
                seen_re.add(key)
                out['regex_replacements'].append([str(pair[0]), str(pair[1])])
        if part.get('notes'):
            out['notes'] = str(part['notes'])
    out['drop_phrases'].sort(key=len, reverse=True)
    out['literal_replacements'].sort(key=lambda x: len(x[0]), reverse=True)
    return out


def load_strategy_data_from_text(raw: str, *, path_hint: str = '') -> dict:
    """从 JSON 或 YAML 文本解析策略对象。"""
    text = raw.strip()
    if path_hint.lower().endswith(('.yaml', '.yml')) or (
            not text.startswith('{') and 'drop_phrases:' in text
    ):
        try:
            import yaml
            data = yaml.safe_load(text)
        except ImportError as e:
            raise ImportError(
                '读取 YAML 策略需要 PyYAML：py -3.12 -m pip install pyyaml') from e
        if not isinstance(data, dict):
            raise ValueError('YAML 策略根节点必须是对象')
        return data
    return extract_json_object(text)


def resolve_strategy_path(path: Path | str | None) -> Path | None:
    if not path:
        return None
    p = Path(str(path))
    if not p.is_absolute():
        p = HERE / p
    return p


def load_strategy(path: Path | str | None = None) -> dict:
    p = resolve_strategy_path(path) if path else None
    if p is None:
        if GEMINI_STRATEGY_YAML.is_file():
            p = GEMINI_STRATEGY_YAML
        elif DEFAULT_STRATEGY_PATH.is_file():
            p = DEFAULT_STRATEGY_PATH
    if p and p.is_file():
        try:
            raw = p.read_text(encoding='utf-8-sig')
            data = load_strategy_data_from_text(raw, path_hint=p.name)
            normalized, errors = validate_strategy(data)
            if errors:
                raise ValueError('; '.join(errors))
            merged = merge_strategies(BUILTIN_STRATEGY, normalized)
            merged['strategy_file'] = str(p.name)
            return merged
        except Exception:
            pass
    return dict(BUILTIN_STRATEGY)


def save_strategy(strategy: dict, path: Path | str | None = None) -> Path:
    p = Path(path) if path else DEFAULT_STRATEGY_PATH
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(strategy, f, ensure_ascii=False, indent=2)
    return p


def validate_strategy(data: dict) -> tuple[dict, list[str]]:
    """校验并规范化策略 JSON（NotebookLM / 手工编辑后导入用）。"""
    errors: list[str] = []
    if not isinstance(data, dict):
        return {}, ['根对象必须是 JSON 对象 {}']

    out = {
        'version': int(data.get('version') or 1),
        'source': str(data.get('source') or 'imported'),
        'drop_phrases': [],
        'literal_replacements': [],
        'regex_replacements': [],
        'notes': str(data.get('notes') or '').strip(),
    }
    if data.get('course') or data.get('title'):
        out['course'] = str(data.get('course') or data.get('title'))

    drops = data.get('drop_phrases') or []
    if not isinstance(drops, list):
        errors.append('drop_phrases 必须是字符串数组')
    else:
        for p in drops:
            p = str(p).strip()
            if p:
                out['drop_phrases'].append(p)

    lits = data.get('literal_replacements') or []
    if not isinstance(lits, list):
        errors.append('literal_replacements 必须是 [[原, 新], ...]')
    else:
        for pair in lits:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                out['literal_replacements'].append([str(pair[0]), str(pair[1])])
            elif isinstance(pair, dict) and pair.get('from') is not None:
                out['literal_replacements'].append([
                    str(pair['from']), str(pair.get('to', '')),
                ])
            else:
                errors.append(f'无效 literal_replacements 项：{pair!r}')

    regs = data.get('regex_replacements') or []
    if not isinstance(regs, list):
        errors.append('regex_replacements 必须是 [[正则, 替换], ...]')
    else:
        for pair in regs:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                pat, repl = str(pair[0]), str(pair[1])
                try:
                    re.compile(pat)
                except re.error as e:
                    errors.append(f'无效正则 {pat!r}: {e}')
                    continue
                out['regex_replacements'].append([pat, repl])
            else:
                errors.append(f'无效 regex_replacements 项：{pair!r}')

    out['drop_phrases'].sort(key=len, reverse=True)
    out['literal_replacements'].sort(key=lambda x: len(x[0]), reverse=True)
    return out, errors


def extract_json_object(text: str) -> dict:
    """从纯 JSON 或 Markdown 代码块中提取策略对象。"""
    text = text.strip()
    if text.startswith('{'):
        return json.loads(text)
    m = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if m:
        return json.loads(m.group(1))
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        raise ValueError('未找到 JSON 对象，请让 NotebookLM 输出 ```json ... ``` 代码块')
    return json.loads(m.group())


def import_strategy_file(path: Path | str) -> dict:
    """读取 NotebookLM / Gemini YAML / JSON 策略，校验并与内置规则合并。"""
    p = Path(path)
    raw = p.read_text(encoding='utf-8-sig')
    data = load_strategy_data_from_text(raw, path_hint=p.name)
    normalized, errors = validate_strategy(data)
    if errors:
        raise ValueError('策略校验失败：\n  - ' + '\n  - '.join(errors))
    return merge_strategies(BUILTIN_STRATEGY, normalized)


def preview_strategy_on_samples(
    strategy: dict,
    samples: list[dict],
) -> dict:
    """在样本句上试跑策略，供导入后自检。"""
    stats = {'tried': 0, 'fixed': 0, 'still_long': 0}
    for s in samples:
        zh = s.get('zh', '')
        zmax = int(s.get('zh_max') or 0)
        if not zh or zmax <= 0:
            continue
        if len(re.sub(r'\s+', '', zh)) <= zmax:
            continue
        stats['tried'] += 1
        new_zh, _ = compress_zh_for_dub(zh, zmax, strategy)
        if len(new_zh) <= zmax:
            stats['fixed'] += 1
        else:
            stats['still_long'] += 1
    return stats


def _apply_literal(text: str, old: str, new: str) -> str:
    if not old or old not in text:
        return text
    return text.replace(old, new)


def _apply_regex(text: str, pattern: str, repl: str) -> str:
    try:
        return re.sub(pattern, repl, text)
    except re.error:
        return text


def _clause_shorten(zh: str, max_chars: int) -> str:
    """按逗号/分号保留前半（最后手段前）。"""
    if len(zh) <= max_chars:
        return zh
    for sep in ('，', '；', '、', ',', ';'):
        parts = [p.strip() for p in zh.split(sep) if p.strip()]
        if len(parts) <= 1:
            continue
        buf = parts[0]
        for p in parts[1:]:
            trial = buf + sep + p
            if len(trial) <= max_chars:
                buf = trial
            else:
                break
        if buf and len(buf) <= max_chars:
            return buf
    try:
        from step1_subtitle_to_audio import _find_natural_zh_split
        return zh[:_find_natural_zh_split(zh, max_chars)]
    except Exception:
        return zh[:max_chars]


def compress_zh_for_dub(
    zh: str,
    max_chars: int,
    strategy: dict | None = None,
) -> tuple[str, str]:
    """
    按策略压缩中文到 max_chars 以内。
    返回 (结果, 来源标签 strategy|clause|unchanged|truncate)。
    """
    zh = re.sub(r'\s+', '', (zh or '').strip())
    if not zh or max_chars <= 0:
        return zh, 'unchanged'
    if len(zh) <= max_chars:
        return zh, 'unchanged'

    strat = strategy or BUILTIN_STRATEGY

    text = zh
    for phrase in strat.get('drop_phrases') or []:
        if phrase and phrase in text:
            text = text.replace(phrase, '')
    for old, new in strat.get('literal_replacements') or []:
        text = _apply_literal(text, str(old), str(new))
    for pattern, repl in strat.get('regex_replacements') or []:
        text = _apply_regex(text, str(pattern), str(repl))

    if len(text) <= max_chars:
        return text, 'strategy'

    text = _clause_shorten(text, max_chars)
    if len(text) <= max_chars:
        return text, 'clause'

    if len(text) > max_chars:
        try:
            from step1_subtitle_to_audio import _find_natural_zh_split
            text = text[:_find_natural_zh_split(text, max_chars)]
        except Exception:
            text = text[:max_chars]
        return text, 'truncate'
    return text, 'strategy'


def apply_local_compress_to_cues(
    cues: list,
    ratio: float,
    cfg: dict,
    *,
    strategy: dict | None = None,
) -> tuple[list, dict]:
    """
    对超长句尝试本地策略压缩；成功则更新 cue 文本，减少后续 Gemini 调用。
    """
    from ai_shorten import (
        cue_zh_max,
        is_long_cue,
        split_bilingual_text,
    )

    path = resolve_strategy_path(cfg.get('zh_strategy_path'))
    strat = strategy or load_strategy(path)
    stats = {
        'tried': 0,
        'fixed': 0,
        'still_long': 0,
        'strategy_source': strat.get('source', 'builtin'),
    }

    for cue in cues:
        en, zh = split_bilingual_text(cue.get('text', ''))
        if not zh:
            continue
        win = cue['end'] - cue['start']
        if not is_long_cue(en, zh, win, ratio, cfg):
            continue
        stats['tried'] += 1
        zmax = cue_zh_max(en, win, ratio, cfg)
        new_zh, how = compress_zh_for_dub(zh, zmax, strat)
        if new_zh != zh and len(new_zh) <= zmax:
            cue['text'] = f'{en}\n{new_zh}' if en else new_zh
            cue['zh_compressed'] = how
            stats['fixed'] += 1
        elif is_long_cue(en, new_zh, win, ratio, cfg):
            stats['still_long'] += 1

    return cues, stats


def sample_long_cues_for_strategy(
    cues: list,
    ratio: float,
    cfg: dict,
    *,
    limit: int = 25,
) -> list[dict]:
    """抽取典型长句样本，供一次性生成策略。"""
    from ai_shorten import cue_zh_max, is_long_cue, split_bilingual_text

    samples: list[dict] = []
    for i, cue in enumerate(cues):
        en, zh = split_bilingual_text(cue.get('text', ''))
        if not zh:
            continue
        win = cue['end'] - cue['start']
        if not is_long_cue(en, zh, win, ratio, cfg):
            continue
        zmax = cue_zh_max(en, win, ratio, cfg)
        overflow = len(zh) - zmax
        samples.append({
            'index': i,
            'en': en[:400],
            'zh': zh,
            'zh_len': len(zh),
            'zh_max': zmax,
            'overflow': overflow,
            'window_sec': round(win, 2),
        })
    samples.sort(key=lambda x: x['overflow'], reverse=True)
    return samples[:limit]


def build_strategy_with_gemini(
    samples: list[dict],
    api_key: str,
    model: str,
) -> dict:
    """一次性调用 Gemini，从长句样本归纳压缩策略 JSON。"""
    from ai_shorten import _gemini_generate, _parse_json_array

    if not samples:
        raise ValueError('无长句样本，无法生成策略')
    payload = json.dumps(samples, ensure_ascii=False, indent=2)
    raw = _gemini_generate(
        STRATEGY_BUILD_PROMPT + '\n\n样本：\n' + payload,
        api_key,
        model,
    )
    text = raw.strip()
    if text.startswith('{'):
        data = json.loads(text)
    else:
        m = re.search(r'\{[\s\S]*\}', text)
        if not m:
            raise RuntimeError('Gemini 未返回有效 JSON 策略')
        data = json.loads(m.group())
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise RuntimeError('策略格式无效')
    data['version'] = 1
    data['source'] = 'gemini'
    data['sample_count'] = len(samples)
    return merge_strategies(BUILTIN_STRATEGY, data)
