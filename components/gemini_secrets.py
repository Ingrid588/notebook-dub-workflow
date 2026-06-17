#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gemini API Key：仅从系统/用户环境变量读取，不读 .env / bat / secrets 文件。
"""

from __future__ import annotations

import os
from pathlib import Path

# 按优先级（与 Google AI Studio 习惯一致）
GEMINI_API_KEY_ENV_VARS = (
    'GEMINI_API_KEY',
    'GOOGLE_API_KEY',
    'TUBEGET_GEMINI_API_KEY',
)

SECRET_KEYS_NEVER_SAVE = frozenset({
    'gemini_api_key',
    'google_api_key',
})

ENV_SETUP_DOC = 'components\\GEMINI_API_KEY_SETUP.txt'


def gemini_api_key_from_env() -> str:
    """返回 Gemini API Key；未配置则空字符串。"""
    for name in GEMINI_API_KEY_ENV_VARS:
        val = (os.environ.get(name) or '').strip()
        if val:
            return val
    return ''


def gemini_api_key_status() -> str:
    """供 UI 显示，不泄露完整 Key。"""
    key = gemini_api_key_from_env()
    if not key:
        return (
            '未设置（请在 Windows 用户环境变量中配置 GEMINI_API_KEY，'
            f'见 {ENV_SETUP_DOC}）'
        )
    if len(key) <= 10:
        return '已设置（GEMINI_API_KEY，来自系统/用户环境变量）'
    return f'已设置 GEMINI_API_KEY（{key[:6]}…{key[-4:]}，环境变量）'


def sanitize_secrets_for_save(data: dict) -> dict:
    """写入文件前剥离 API Key 字段。"""
    return {
        k: v for k, v in (data or {}).items()
        if k not in SECRET_KEYS_NEVER_SAVE
    }


def merge_secrets_with_env(file_data: dict | None) -> dict:
    """内存中的 secrets：文件配置 + 环境变量 Key。"""
    out = dict(file_data or {})
    out.pop('gemini_api_key', None)
    out.pop('google_api_key', None)
    key = gemini_api_key_from_env()
    if key:
        out['gemini_api_key'] = key
    return out


def strip_legacy_key_from_secrets_file(secrets_path: Path) -> bool:
    """
    若 tubeget.secrets.json 仍含 gemini_api_key，删除并写回。
    返回 True 表示曾迁移。
    """
    if not secrets_path.is_file():
        return False
    try:
        import json
        raw = json.loads(secrets_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return False
    if 'gemini_api_key' not in raw:
        return False
    raw.pop('gemini_api_key', None)
    try:
        secrets_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
    except OSError:
        return False
    return True
