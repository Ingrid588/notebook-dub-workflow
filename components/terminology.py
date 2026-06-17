#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
加载「术语表」文件夹中的多语言术语。
用途：Gemini 提示词注入；本地双语字幕术语校正（零 API）。
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

HERE = Path(__file__).parent.resolve()
DEFAULT_GLOSSARY_DIR = HERE.parent / '术语表'

_LANG_COLUMNS = {
    'English': 'en',
    '日本語': 'ja',
    '한국어': 'ko',
    'Русский': 'ru',
    'Deutsch': 'de',
    'Español': 'es',
    'Hindi': 'hi',
}

_WRONG_ZH_COL = '常见误译'


class TerminologyGlossary:
    """术语表：外文 → 标准中文译名；常见误译 → 标准中文。"""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self._lookup: dict[str, str] = {}
        self._wrong_zh: list[tuple[str, str]] = []
        self._loaded_files: list[str] = []

    @property
    def size(self) -> int:
        return len(self.entries)

    @property
    def loaded_files(self) -> list[str]:
        return list(self._loaded_files)

    def load_dir(self, directory: Path | str | None = None) -> int:
        d = Path(directory) if directory else DEFAULT_GLOSSARY_DIR
        if not d.is_dir():
            return 0
        self.entries.clear()
        self._lookup.clear()
        self._wrong_zh.clear()
        self._loaded_files.clear()
        for csv_path in sorted(d.glob('*.csv')):
            if csv_path.name.startswith('-'):
                continue
            n = self._load_csv(csv_path)
            if n:
                self._loaded_files.append(csv_path.name)
        self._wrong_zh.sort(key=lambda x: len(x[0]), reverse=True)
        return len(self.entries)

    def _load_csv(self, path: Path) -> int:
        before = len(self.entries)
        try:
            raw = path.read_text(encoding='utf-8-sig')
        except OSError:
            return 0
        reader = csv.DictReader(raw.splitlines())
        if not reader.fieldnames or '中文' not in reader.fieldnames:
            return 0
        cat_col = '分类' if '分类' in reader.fieldnames else None
        lang_cols = [c for c in reader.fieldnames if c in _LANG_COLUMNS]
        has_wrong = _WRONG_ZH_COL in reader.fieldnames

        for row in reader:
            zh = (row.get('中文') or '').strip()
            if not zh:
                continue
            entry = {
                'category': (row.get(cat_col) or '').strip() if cat_col else '',
                'zh': zh,
                'terms': {},
                'wrong_zh': [],
            }
            for col in lang_cols:
                val = (row.get(col) or '').strip()
                if not val:
                    continue
                for part in re.split(r'[/|｜]', val):
                    part = part.strip()
                    if part:
                        entry['terms'][_LANG_COLUMNS[col]] = part
                        self._register_lookup(part, zh)
            if has_wrong:
                wrong_raw = (row.get(_WRONG_ZH_COL) or '').strip()
                for w in re.split(r'[|｜]', wrong_raw):
                    w = w.strip()
                    if w and w != zh:
                        entry['wrong_zh'].append(w)
                        self._wrong_zh.append((w, zh))
            if entry['terms'] or entry['wrong_zh']:
                self.entries.append(entry)
        return len(self.entries) - before

    def _register_lookup(self, foreign: str, zh: str) -> None:
        key = foreign.strip().lower()
        if key and key not in self._lookup:
            self._lookup[key] = zh

    def find_relevant(self, text: str, *, limit: int = 40) -> list[dict]:
        if not text or not self.entries:
            return []
        text_lower = text.lower()
        matched: list[dict] = []
        seen_zh: set[str] = set()
        for entry in self.entries:
            hit = False
            for term in entry['terms'].values():
                if term and self._term_in_text(term, text_lower):
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

    def apply_to_zh(self, en_text: str, zh_text: str) -> tuple[str, list[str]]:
        """英文行命中术语时，将中文行中的常见误译替换为标准译名。"""
        if not zh_text:
            return zh_text, []
        relevant = self.find_relevant(en_text or '', limit=80)
        if not relevant:
            return zh_text, []

        out = zh_text
        notes: list[str] = []
        for entry in relevant:
            target = entry['zh']
            for wrong in entry.get('wrong_zh') or []:
                if wrong in out and wrong != target:
                    out = out.replace(wrong, target)
                    notes.append(f'{wrong}→{target}')
        return out, notes

    def format_for_prompt(self, entries: list[dict]) -> str:
        if not entries:
            return ''
        lines = ['【专业术语表 — 译名必须保持一致，勿随意改写】']
        for e in entries:
            parts = [f'{lang}:{term}' for lang, term in sorted(e['terms'].items())]
            cat = f'（{e["category"]}）' if e.get('category') else ''
            wrong = ''
            if e.get('wrong_zh'):
                wrong = '；误译→' + '/'.join(e['wrong_zh'])
            lines.append(f'- {e["zh"]}{cat} ← ' + ' | '.join(parts) + wrong)
        return '\n'.join(lines)

    def collect_for_cues(self, cues: list, *, limit: int = 50) -> str:
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
