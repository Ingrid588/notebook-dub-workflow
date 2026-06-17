#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配音成本策略：翻译层一次 Gemini，配音阶段禁止 AI 微调。

原则：翻译解决 99%，脚本（本地压缩 + 加速）处理 1%，不再反复送 AI。
"""

from __future__ import annotations

import os
from pathlib import Path


def dub_translate_once_policy(cfg: dict | None = None) -> dict:
    """
    返回策略开关。默认开启 translate_once（商用推荐）。
    """
    c = cfg or {}
    translate_once = c.get('dub_translate_once')
    if translate_once is None:
        translate_once = True
    translate_once = bool(translate_once)
    if os.environ.get('TUBEGET_TRANSLATE_ONCE', '').strip().lower() in (
            '1', 'true', 'yes'):
        translate_once = True
    if os.environ.get('TUBEGET_TRANSLATE_ONCE', '').strip().lower() in (
            '0', 'false', 'no'):
        translate_once = False

    return {
        'translate_once': translate_once,
        'allow_gemini_dub_rewrite': not translate_once and bool(
            c.get('gemini_dub_rewrite', False)),
        'allow_gemini_post_tts': not translate_once and bool(
            c.get('gemini_post_tts_rewrite', False)),
        'allow_gemini_chunk': not translate_once and bool(
            c.get('gemini_chunk_translate', False)),
    }


def gemini_allowed_in_dub_pipeline(cfg: dict | None = None) -> bool:
    """配音主流程（Chunk / Speech 改写）是否允许调 Gemini。"""
    p = dub_translate_once_policy(cfg)
    if p['translate_once']:
        return False
    return p['allow_gemini_dub_rewrite'] or p['allow_gemini_chunk']


def gemini_allowed_post_tts(cfg: dict | None = None) -> bool:
    """TTS 后超窗是否允许 Gemini 压缩。"""
    p = dub_translate_once_policy(cfg)
    return p['allow_gemini_post_tts']


def subtitle_is_fit_dub(subtitle: Path | str | None) -> bool:
    if not subtitle:
        return False
    return '.fit-dub' in Path(subtitle).stem


def print_translate_once_banner(quiet: bool = False) -> None:
    if quiet:
        return
    print('  [策略] 翻译一次：Gemini 仅在 fit_translate.bat；配音阶段不送 AI')
    print('         超窗：本地规则压缩 + 加速；仍失败则保留全长或静音（不调 API）')
