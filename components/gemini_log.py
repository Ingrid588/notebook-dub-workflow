#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemini 调用审计日志：请求内容、回复、逐句与时间窗统计。"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent.resolve()
PROJECT_ROOT = HERE.parent
DEFAULT_LOG_DIR = PROJECT_ROOT / 'logs' / 'gemini'


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _zh_len(text: str) -> int:
    return len(re.sub(r'\s+', '', text or ''))


def gemini_log_enabled(cfg: dict | None = None) -> bool:
    if cfg is None:
        cfg = {}
    return bool(cfg.get('gemini_log', True))


def gemini_log_paths(video: str | Path) -> tuple[Path, Path]:
    """返回 (json_path, md_path)，与 *_C.gemini-run.txt 同目录、同 _C 前缀。"""
    from dub_naming import dub_stem

    video = Path(video)
    base = video.parent / dub_stem(video.stem)
    return Path(f'{base}.gemini_log.json'), Path(f'{base}.gemini_log.md')


def gemini_compare_txt_path(video: str | Path) -> Path:
    from dub_naming import dub_stem

    video = Path(video)
    return video.parent / f'{dub_stem(video.stem)}.gemini-compare.txt'


def gemini_zh_txt_path(video: str | Path) -> Path:
    from dub_naming import dub_stem

    video = Path(video)
    return video.parent / f'{dub_stem(video.stem)}.gemini-zh.txt'


def render_gemini_compare_text(cues_data: list[dict]) -> str:
    """改前/改后对照，记事本可直接打开比较。"""
    if not cues_data:
        return ''
    changed_n = sum(1 for c in cues_data if c.get('changed'))
    lines = [
        '=' * 58,
        '  Gemini 字幕对照（改前 → 改后）',
        '  说明：API 仅收原语言原文（source/en），不含现有中文；',
        '        「改前」为本地字幕，供你对照 Gemini 新译文是否值得替换。',
        '=' * 58,
        f'共 {len(cues_data)} 组送 Gemini，其中 {changed_n} 组有改写',
        '',
    ]
    for c in cues_data:
        no = c.get('cue_no', c.get('index', 0) + 1)
        start = c.get('start')
        end = c.get('end')
        ts = ''
        if start is not None and end is not None:
            ts = f'  {float(start):.1f}s → {float(end):.1f}s'
        kind = c.get('kind') or ''
        tag = '【有改】' if c.get('changed') else '【未改】'
        lines.append('-' * 58)
        lines.append(f'#{int(no):03d}{ts}  {tag}  {kind}')
        en = (c.get('en') or '').replace('\n', ' ').strip()
        if en:
            lines.append(f'原文：{en}')
        zb = (c.get('zh_before') or '').replace('\n', ' ').strip()
        za = (c.get('zh_after') or '').replace('\n', ' ').strip()
        lines.append(f'改前：{zb}')
        lines.append(f'改后：{za}')
        lines.append('')
    lines.append('=' * 58)
    return '\n'.join(lines)


def render_gemini_zh_only_text(cues_data: list[dict]) -> str:
    """仅 Gemini 返回的新中文字幕（一行一组）。"""
    lines = [
        '# Gemini 新字幕（每组一行，#序号 开头）',
        '',
    ]
    for c in cues_data:
        no = c.get('cue_no', c.get('index', 0) + 1)
        zh = (c.get('zh_after') or '').replace('\n', ' ').strip()
        if zh:
            lines.append(f'#{int(no):03d}  {zh}')
    return '\n'.join(lines) + '\n'


def save_gemini_rewrite_texts(
    video: str | Path,
    cues_data: list[dict],
    *,
    mirror: bool = True,
) -> tuple[Path | None, Path | None]:
    """写入 _C.gemini-compare.txt 与 _C.gemini-zh.txt。"""
    if not cues_data:
        return None, None
    video = Path(video)
    compare_path = gemini_compare_txt_path(video)
    zh_path = gemini_zh_txt_path(video)
    compare_path.write_text(render_gemini_compare_text(cues_data), encoding='utf-8')
    zh_path.write_text(render_gemini_zh_only_text(cues_data), encoding='utf-8')
    if mirror:
        try:
            DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
            from dub_naming import dub_stem
            stem = dub_stem(video.stem)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            (DEFAULT_LOG_DIR / f'{stem}_{ts}.gemini-compare.txt').write_text(
                compare_path.read_text(encoding='utf-8'), encoding='utf-8')
            (DEFAULT_LOG_DIR / f'{stem}_{ts}.gemini-zh.txt').write_text(
                zh_path.read_text(encoding='utf-8'), encoding='utf-8')
            shutil.copy2(compare_path, DEFAULT_LOG_DIR / 'latest_gemini_compare.txt')
            shutil.copy2(zh_path, DEFAULT_LOG_DIR / 'latest_gemini_zh.txt')
        except OSError:
            pass
    return compare_path, zh_path


def cues_data_from_stats(gem_stats: dict | None) -> list[dict]:
    rows = (gem_stats or {}).get('compare_rows') or []
    return list(rows)


def cues_data_from_log_json(log_json: str | Path | None) -> list[dict]:
    if not log_json:
        return []
    path = Path(log_json)
    if not path.is_file():
        return []
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f).get('cues') or []
    except (OSError, json.JSONDecodeError):
        return []


class GeminiLogSession:
    """单次视频配音过程中的 Gemini 调用记录。"""

    def __init__(
        self,
        *,
        video: str | Path | None = None,
        subtitle: str | Path | None = None,
        model: str = '',
        mode: str = 'rewrite',
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.video = str(video) if video else ''
        self.subtitle = str(subtitle) if subtitle else ''
        self.model = model
        self.mode = mode
        self.started_at = _utc_now_iso()
        self.ended_at = ''
        self.batches: list[dict] = []
        self.cues: list[dict] = []
        self.skipped: list[dict] = []
        self._batch_no = 0

    def record_skipped(
        self,
        index: int,
        *,
        reason: str,
        zh: str = '',
        en: str = '',
        start: float = 0,
        end: float = 0,
        kind: str = '',
    ) -> None:
        if not self.enabled:
            return
        self.skipped.append({
            'index': index,
            'cue_no': index + 1,
            'start': round(start, 3),
            'end': round(end, 3),
            'window_sec': round(max(0, end - start), 3),
            'kind': kind,
            'en': en,
            'zh': zh,
            'reason': reason,
        })

    def record_batch(
        self,
        *,
        items: list[dict],
        instruction: str,
        payload: list[dict],
        user_message: str,
        raw_response: str,
        mapping: dict[int, str],
        duration_ms: float,
        error: str | None = None,
        glossary_included: bool = False,
    ) -> int:
        """记录一批 API 调用，并写入逐句结果。返回 batch_no。"""
        if not self.enabled:
            return 0
        self._batch_no += 1
        batch_no = self._batch_no
        parsed_rows = []
        for idx, zh_out in sorted(mapping.items()):
            parsed_rows.append({'index': idx, 'zh': zh_out})

        self.batches.append({
            'batch_no': batch_no,
            'cue_count': len(items),
            'duration_ms': round(duration_ms, 1),
            'error': error,
            'glossary_included': glossary_included,
            'instruction_preview': instruction[:800],
            'instruction_chars': len(instruction),
            'request_payload': payload,
            'user_message_chars': len(user_message),
            'response_raw': raw_response,
            'response_parsed': parsed_rows,
        })

        item_by_idx = {it['index']: it for it in items}
        for item in items:
            i = item['index']
            zh_before = item.get('zh', '')
            zh_after = mapping.get(i, zh_before)
            wp = item.get('window_params') or {}
            window = float(
                wp.get('duration')
                or item.get('window_sec')
                or 0)
            zmax = int(wp.get('max_chars') or item.get('zh_max') or 0)
            ztarget = int(wp.get('target_chars') or 0)
            start = float(item.get('start', 0))
            end = float(item.get('end', 0))
            self.cues.append({
                'index': i,
                'cue_no': i + 1,
                'batch_no': batch_no,
                'kind': item.get('kind', self.mode),
                'start': round(start, 3) if start else None,
                'end': round(end, 3) if end else None,
                'window_sec': round(window, 3),
                'target_chars': ztarget or None,
                'max_chars': zmax or None,
                'zh_len_before': _zh_len(zh_before),
                'zh_len_after': _zh_len(zh_after),
                'en': item.get('en', ''),
                'zh_before': zh_before,
                'zh_after': zh_after,
                'changed': zh_after != zh_before,
                'gemini_applied': i in mapping,
                'error': error,
            })
        return batch_no

    def record_chunk_batch(
        self,
        *,
        chunk_id: int,
        lines: list,
        instruction: str,
        input_srt: str,
        user_message: str,
        raw_response: str,
        mapping: dict[int, str],
        cues: list[dict],
        duration_ms: float,
        total_duration: float,
        max_chars: int,
        error: str | None = None,
    ) -> int:
        """记录 Chunk 语义块翻译的一批 API 调用。"""
        if not self.enabled:
            return 0
        from ai_shorten import split_bilingual_text

        self._batch_no += 1
        batch_no = self._batch_no
        parsed_rows = [
            {'index': idx, 'zh': zh_out}
            for idx, zh_out in sorted(mapping.items())
        ]
        self.batches.append({
            'batch_no': batch_no,
            'chunk_id': chunk_id,
            'cue_count': len(lines),
            'duration_ms': round(duration_ms, 1),
            'error': error,
            'total_duration_sec': round(total_duration, 3),
            'max_chinese_chars': max_chars,
            'instruction_preview': instruction[:800],
            'instruction_chars': len(instruction),
            'input_srt': input_srt,
            'user_message_chars': len(user_message),
            'response_raw': raw_response,
            'response_parsed': parsed_rows,
            'request_payload': [],
        })
        for ln in lines:
            i = ln.cue_index
            if i < 0 or i >= len(cues):
                continue
            _, zh_before = split_bilingual_text(cues[i].get('text', ''))
            zh_after = mapping.get(i, zh_before)
            start = float(ln.start)
            end = float(ln.end)
            self.cues.append({
                'index': i,
                'cue_no': i + 1,
                'batch_no': batch_no,
                'chunk_id': chunk_id,
                'kind': 'chunk_translate',
                'start': round(start, 3),
                'end': round(end, 3),
                'window_sec': round(max(0, end - start), 3),
                'target_chars': None,
                'max_chars': max_chars,
                'zh_len_before': _zh_len(zh_before),
                'zh_len_after': _zh_len(zh_after),
                'en': ln.foreign,
                'zh_before': zh_before,
                'zh_after': zh_after,
                'changed': zh_after != zh_before,
                'gemini_applied': i in mapping,
                'error': error,
            })
        return batch_no

    def record_batch_error(
        self,
        *,
        items: list[dict],
        error: str,
        duration_ms: float = 0,
    ) -> None:
        if not self.enabled:
            return
        self._batch_no += 1
        self.batches.append({
            'batch_no': self._batch_no,
            'cue_count': len(items),
            'duration_ms': round(duration_ms, 1),
            'error': error,
            'request_payload': [],
            'response_raw': '',
            'response_parsed': [],
        })
        for item in items:
            i = item['index']
            self.cues.append({
                'index': i,
                'cue_no': i + 1,
                'batch_no': self._batch_no,
                'kind': item.get('kind', self.mode),
                'zh_before': item.get('zh', ''),
                'zh_after': item.get('zh', ''),
                'changed': False,
                'gemini_applied': False,
                'error': error,
            })

    def build_window_stats(self) -> list[dict]:
        """按时间窗长度分桶统计。"""
        buckets = [
            ('<2s', 0, 2),
            ('2-4s', 2, 4),
            ('4-7s', 4, 7),
            ('7-12s', 7, 12),
            ('>12s', 12, 9999),
        ]
        stats: list[dict] = []
        for label, lo, hi in buckets:
            rows = [
                c for c in self.cues
                if lo <= float(c.get('window_sec') or 0) < hi
            ]
            if not rows and not any(
                lo <= float(s.get('window_sec') or 0) < hi for s in self.skipped
            ):
                continue
            changed = sum(1 for r in rows if r.get('changed'))
            stats.append({
                'window_bucket': label,
                'sent_to_gemini': len(rows),
                'changed': changed,
                'avg_window_sec': round(
                    sum(float(r.get('window_sec') or 0) for r in rows)
                    / max(len(rows), 1), 3),
                'avg_zh_before': round(
                    sum(r.get('zh_len_before') or 0 for r in rows)
                    / max(len(rows), 1), 1),
                'avg_zh_after': round(
                    sum(r.get('zh_len_after') or 0 for r in rows)
                    / max(len(rows), 1), 1),
            })
        return stats

    def build_summary(self, extra: dict | None = None) -> dict:
        sent = len(self.cues)
        changed = sum(1 for c in self.cues if c.get('changed'))
        failed_batches = sum(1 for b in self.batches if b.get('error'))
        total_ms = sum(float(b.get('duration_ms') or 0) for b in self.batches)
        s = {
            'total_cues_in_subtitle': (extra or {}).get('total_cues'),
            'sent_to_gemini': sent,
            'skipped_count': len(self.skipped),
            'changed_count': changed,
            'unchanged_count': sent - changed,
            'batch_count': len(self.batches),
            'failed_batches': failed_batches,
            'total_api_ms': round(total_ms, 1),
            'model': self.model,
            'mode': self.mode,
        }
        if extra:
            s.update({k: v for k, v in extra.items() if k != 'total_cues'})
        return s

    def to_dict(self, extra_summary: dict | None = None) -> dict:
        self.ended_at = self.ended_at or _utc_now_iso()
        return {
            'version': 1,
            'video': self.video,
            'subtitle': self.subtitle,
            'model': self.model,
            'mode': self.mode,
            'started_at': self.started_at,
            'ended_at': self.ended_at,
            'summary': self.build_summary(extra_summary),
            'window_stats': self.build_window_stats(),
            'batches': self.batches,
            'cues': self.cues,
            'skipped': self.skipped,
        }

    def _resolved_model(self) -> str:
        from ai_shorten import effective_gemini_model
        return effective_gemini_model(self.model)

    def render_markdown(self, extra_summary: dict | None = None) -> str:
        gem_model = self._resolved_model()
        if gem_model != self.model:
            self.model = gem_model
        data = self.to_dict(extra_summary)
        sm = data['summary']
        sm['model'] = gem_model
        lines = [
            '# Gemini 调用日志',
            '',
            f'- 视频：`{Path(self.video).name if self.video else "?"}`',
            f'- 字幕：`{Path(self.subtitle).name if self.subtitle else "?"}`',
            f'- 模型：`{gem_model}`',
            f'- 模式：`{self.mode}`',
            f'- 开始：{self.started_at}',
            f'- 结束：{self.ended_at}',
            '',
            '## 汇总',
            '',
            '| 指标 | 数值 |',
            '|------|------|',
            f'| 模型 | {gem_model} |',
            f'| 字幕总句数 | {sm.get("total_cues_in_subtitle", "-")} |',
            f'| 发送 Gemini | {sm.get("sent_to_gemini", 0)} |',
            f'| 跳过（未送 API） | {sm.get("skipped_count", 0)} |',
            f'| 有改写 | {sm.get("changed_count", 0)} |',
            f'| 未改写 | {sm.get("unchanged_count", 0)} |',
            f'| API 批次数 | {sm.get("batch_count", 0)} |',
            f'| 失败批次 | {sm.get("failed_batches", 0)} |',
            f'| API 总耗时(ms) | {sm.get("total_api_ms", 0)} |',
            '',
        ]

        if data['window_stats']:
            lines.extend([
                '## 时间窗统计',
                '',
                '| 窗口 | 送 Gemini 句数 | 有改写 | 平均窗口(s) | 改前均字 | 改后均字 |',
                '|------|-------------|--------|------------|---------|---------|',
            ])
            for w in data['window_stats']:
                lines.append(
                    f'| {w["window_bucket"]} | {w["sent_to_gemini"]} | {w["changed"]} '
                    f'| {w["avg_window_sec"]} | {w["avg_zh_before"]} | {w["avg_zh_after"]} |'
                )
            lines.append('')

        if data['cues']:
            lines.extend([
                '## 逐句明细（送 Gemini）',
                '',
                '| # | 起(s) | 止(s) | 窗口(s) | 目标字 | 上限 | 改前字 | 改后字 | 类型 | 改动 | 原中文 | Gemini 回复 |',
                '|---|------|------|---------|-------|------|-------|-------|------|------|--------|------------|',
            ])
            for c in data['cues']:
                zh_b = (c.get('zh_before') or '').replace('|', '\\|')[:40]
                zh_a = (c.get('zh_after') or '').replace('|', '\\|')[:40]
                if len(c.get('zh_before') or '') > 40:
                    zh_b += '…'
                if len(c.get('zh_after') or '') > 40:
                    zh_a += '…'
                lines.append(
                    f'| {c.get("cue_no", "")} '
                    f'| {c.get("start") or "-"} '
                    f'| {c.get("end") or "-"} '
                    f'| {c.get("window_sec") or "-"} '
                    f'| {c.get("target_chars") or "-"} '
                    f'| {c.get("max_chars") or "-"} '
                    f'| {c.get("zh_len_before", "")} '
                    f'| {c.get("zh_len_after", "")} '
                    f'| {c.get("kind", "")} '
                    f'| {"是" if c.get("changed") else "否"} '
                    f'| {zh_b} '
                    f'| {zh_a} |'
                )
            lines.append('')

        for batch in data['batches']:
            bno = batch['batch_no']
            chunk_tag = ''
            if batch.get('chunk_id') is not None:
                chunk_tag = f'（Chunk #{int(batch["chunk_id"]) + 1}）'
            lines.extend([
                f'## 批次 {bno}{chunk_tag}',
                '',
                f'- 句数：{batch.get("cue_count", 0)}',
                f'- 耗时(ms)：{batch.get("duration_ms", 0)}',
                f'- 错误：{batch.get("error") or "无"}',
            ])
            if batch.get('total_duration_sec') is not None:
                lines.append(
                    f'- 块时长：{batch.get("total_duration_sec")}s，'
                    f'字数上限：{batch.get("max_chinese_chars", "-")}')
            lines.append('')
            if batch.get('input_srt'):
                lines.extend([
                    '### 发送给 Gemini 的外语 SRT',
                    '',
                    '```',
                    (batch.get('input_srt') or '')[:12000],
                    '```',
                    '',
                ])
            elif batch.get('request_payload'):
                lines.extend([
                    '### 发送给 Gemini 的输入（JSON）',
                    '',
                    '```json',
                    json.dumps(batch.get('request_payload') or [], ensure_ascii=False, indent=2),
                    '```',
                    '',
                ])
            lines.extend([
                '### Gemini 原始回复',
                '',
                '```',
                (batch.get('response_raw') or '(空)')[:12000],
                '```',
                '',
            ])

        if data['skipped']:
            lines.extend([
                '## 未送 Gemini 的句子（抽样前 50 条）',
                '',
                '| # | 窗口(s) | 原因 | 中文 |',
                '|---|---------|------|------|',
            ])
            for s in data['skipped'][:50]:
                zh = (s.get('zh') or '').replace('|', '\\|')[:36]
                lines.append(
                    f'| {s.get("cue_no")} | {s.get("window_sec", "-")} '
                    f'| {s.get("reason", "")} | {zh} |'
                )
            if len(data['skipped']) > 50:
                lines.append(f'\n*… 共 {len(data["skipped"])} 条，完整见 JSON*')
            lines.append('')

        return '\n'.join(lines)

    def estimate_billable_chars(self) -> dict[str, int]:
        """估算本视频 Gemini 处理字符（输入+输出，供费用参考）。"""
        from gemini_usage_tracker import estimate_chars_from_session
        return estimate_chars_from_session(self)

    def save(
        self,
        video: str | Path,
        *,
        extra_summary: dict | None = None,
        mirror_to_project_logs: bool = True,
    ) -> tuple[Path | None, Path | None]:
        if not self.enabled:
            return None, None, None, None
        self.ended_at = _utc_now_iso()
        self.model = self._resolved_model()
        json_path, md_path = gemini_log_paths(video)
        payload = self.to_dict(extra_summary)
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        md_path.write_text(self.render_markdown(extra_summary), encoding='utf-8')

        compare_path, zh_path = save_gemini_rewrite_texts(
            video, self.cues, mirror=mirror_to_project_logs)

        if mirror_to_project_logs:
            try:
                DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
                stem = Path(video).stem
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                mirror_json = DEFAULT_LOG_DIR / f'{stem}_{ts}.gemini_log.json'
                mirror_md = DEFAULT_LOG_DIR / f'{stem}_{ts}.gemini_log.md'
                shutil.copy2(json_path, mirror_json)
                shutil.copy2(md_path, mirror_md)
                latest_json = DEFAULT_LOG_DIR / 'latest.gemini_log.json'
                latest_md = DEFAULT_LOG_DIR / 'latest.gemini_log.md'
                shutil.copy2(json_path, latest_json)
                shutil.copy2(md_path, latest_md)
            except OSError:
                pass

        return json_path, md_path, compare_path, zh_path


def save_gemini_skipped_report(
    video: str | Path,
    subtitle: str | Path,
    reason: str,
    *,
    mirror: bool = True,
) -> Path:
    """Gemini 未执行时仍写入说明文件，避免用户看到「空白 log」。"""
    from ai_shorten import effective_gemini_model
    from dub_naming import dub_stem

    video = Path(video)
    gem_model = effective_gemini_model()
    md_path = video.parent / f'{dub_stem(video.stem)}.gemini_log.md'
    body = (
        f'# Gemini 未在本视频执行\n\n'
        f'- 视频：`{video.name}`\n'
        f'- 字幕：`{Path(subtitle).name}`\n'
        f'- 配置模型：`{gem_model}`\n'
        f'- 原因：**{reason}**\n\n'
        f'## 如何启用\n\n'
        f'1. 双击运行：`配置.bat` 填写 API Key，并选择 Gemini 范围\n'
        f'2. 当前配置模型为 `{gem_model}`（以 tubeget.secrets.json 为准）\n'
        f'3. 确认已开启 Speech Activity 或「优化字幕」\n'
    )
    md_path.write_text(body, encoding='utf-8')
    if mirror:
        try:
            DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
            skipped = DEFAULT_LOG_DIR / 'skipped.gemini_log.md'
            skipped.write_text(body, encoding='utf-8')
            shutil.copy2(md_path, DEFAULT_LOG_DIR / 'latest.gemini_log.md')
        except OSError:
            pass
    return md_path


def save_dub_run_summary(
    video: str | Path,
    lines: list[str],
    *,
    mirror: bool = True,
) -> Path:
    """每次配音结束写入纯文本摘要（记事本可开）。"""
    video = Path(video)
    out = video.parent / f'{video.stem}.dub-run.txt'
    text = '\n'.join(lines) + '\n'
    out.write_text(text, encoding='utf-8')
    if mirror:
        try:
            DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
            (DEFAULT_LOG_DIR / 'latest_dub_run.txt').write_text(text, encoding='utf-8')
        except OSError:
            pass
    return out


def attach_cue_times(items: list[dict], cues: list) -> None:
    """为 log items 附加 start/end。"""
    for item in items:
        i = item.get('index', -1)
        if 0 <= i < len(cues):
            item['start'] = cues[i].get('start', 0)
            item['end'] = cues[i].get('end', 0)
