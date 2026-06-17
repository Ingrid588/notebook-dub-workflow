#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""加载「术语表」文件夹中的多语言术语，供 Gemini 口播优化注入。"""

from __future__ import annotations

import csv
import re
from pathlib import Path

HERE = Path(__file__).parent.resolve()
DEFAULT_GLOSSARY_DIR = HERE.parent / '术语表'

# CSV 列名 → 语言标签（用于匹配字幕原文）
_LANG_COLUMNS = {
    'English': 'en',
    '日本語': 'ja',
    '한국어': 'ko',
    'Русский': 'ru',
    'Deutsch': 'de',
    'Español': 'es',
    'Hindi': 'hi',
}


class TerminologyGlossary:
    """术语表：外文 → 标准中文译名。"""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self._lookup: dict[str, str] = {}
        self._loaded_files: list[str] = []

    @property
    def size(self) -> int:
        return len(self.entries)

    @property
    def loaded_files(self) -> list[str]:
        return list(self._loaded_files)

    def load_dir(self, directory: Path | str | None = None) -> int:
        """加载目录下所有 CSV，返回条目数。"""
        d = Path(directory) if directory else DEFAULT_GLOSSARY_DIR
        if not d.is_dir():
            return 0
        self.entries.clear()
        self._lookup.clear()
        self._loaded_files.clear()
        for csv_path in sorted(d.glob('*.csv')):
            n = self._load_csv(csv_path)
            if n:
                self._loaded_files.append(csv_path.name)
        return len(self.entries)

    def _load_csv(self, path: Path) -> int:
        before = len(self.entries)
        try:
            raw = path.read_text(encoding='utf-8-sig')
        except Exception:
            return 0
        reader = csv.DictReader(raw.splitlines())
        if not reader.fieldnames:
            return 0
        zh_col = '中文' if '中文' in reader.fieldnames else None
        if not zh_col:
            return 0
        cat_col = '分类' if '分类' in reader.fieldnames else None
        lang_cols = [c for c in reader.fieldnames if c in _LANG_COLUMNS]

        for row in reader:
            zh = (row.get(zh_col) or '').strip()
            if not zh:
                continue
            entry = {
                'category': (row.get(cat_col) or '').strip() if cat_col else '',
                'zh': zh,
                'terms': {},
            }
            for col in lang_cols:
                val = (row.get(col) or '').strip()
                if val:
                    entry['terms'][_LANG_COLUMNS[col]] = val
                    self._register_lookup(val, zh)
            if entry['terms']:
                self.entries.append(entry)
        return len(self.entries) - before

    def _register_lookup(self, foreign: str, zh: str) -> None:
        key = foreign.strip().lower()
        if key and key not in self._lookup:
            self._lookup[key] = zh

    def find_relevant(self, text: str, *, limit: int = 40) -> list[dict]:
        """在文本中匹配术语，返回相关条目（去重）。"""
        if not text or not self.entries:
            return []
        text_lower = text.lower()
        matched: list[dict] = []
        seen_zh: set[str] = set()
        for entry in self.entries:
            hit = False
            for lang, term in entry['terms'].items():
                if not term:
                    continue
                if self._term_in_text(term, text_lower):
                    hit = True
                    break
            if hit and entry['zh'] not in seen_zh:
                seen_zh.add(entry['zh'])
                matched.append(entry)
                if len(matched) >= limit:
                    break
        return matched

    @staticmethod
    def _term_in_text(term: str, text_lower: str) -> bool:
        t = term.strip().lower()
        if not t:
            return False
        if ' ' in t or len(t) > 4:
            return t in text_lower
        return bool(re.search(r'\b' + re.escape(t) + r'\b', text_lower))

    def format_for_prompt(self, entries: list[dict]) -> str:
        """格式化为 Gemini 系统提示词片段。"""
        if not entries:
            return ''
        lines = ['【专业术语表 — 译名必须保持一致，勿随意改写】']
        for e in entries:
            parts = [f'{lang}:{term}' for lang, term in sorted(e['terms'].items())]
            cat = f'（{e["category"]}）' if e.get('category') else ''
            lines.append(f'- {e["zh"]}{cat} ← ' + ' | '.join(parts))
        return '\n'.join(lines)

    def collect_for_cues(self, cues: list, *, limit: int = 50) -> str:
        """从多条字幕 cue 收集相关术语提示。"""
        from ai_shorten import split_bilingual_text

        all_entries: list[dict] = []
        seen: set[str] = set()
        for cue in cues:
            en, zh = split_bilingual_text(cue.get('text', ''))
            blob = f'{en} {zh}'
            for entry in self.find_relevant(blob, limit=limit):
                if entry['zh'] not in seen:
                    seen.add(entry['zh'])
                    all_entries.append(entry)
                    if len(all_entries) >= limit:
                        break
            if len(all_entries) >= limit:
                break
        return self.format_for_prompt(all_entries)


_glossary_cache: TerminologyGlossary | None = None


def get_glossary(directory: Path | str | None = None) -> TerminologyGlossary:
    """单例加载术语表。"""
    global _glossary_cache
    if _glossary_cache is None:
        _glossary_cache = TerminologyGlossary()
        _glossary_cache.load_dir(directory or DEFAULT_GLOSSARY_DIR)
    return _glossary_cache


def reload_glossary(directory: Path | str | None = None) -> TerminologyGlossary:
    global _glossary_cache
    _glossary_cache = TerminologyGlossary()
    _glossary_cache.load_dir(directory or DEFAULT_GLOSSARY_DIR)
    return _glossary_cache
