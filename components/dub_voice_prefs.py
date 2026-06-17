#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按视频选择男/女配音员，写入 _dub_voice_map.json 供配音脚本读取。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VOICE_MAP_FILE = '_dub_voice_map.json'


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def edge_voice_map() -> dict[str, str]:
    from step1_subtitle_to_audio import VOICE_MAP
    return dict(VOICE_MAP)


def female_voice_names() -> list[str]:
    return [k for k in edge_voice_map() if k.startswith('晓')]


def male_voice_names() -> list[str]:
    return [k for k in edge_voice_map() if k.startswith('云')]


def load_tubeget_defaults() -> dict:
    here = Path(__file__).parent
    cfg: dict = {'voice': '晓晓', 'voice_female': '晓晓', 'voice_male': '云希'}
    p = here / 'tubeget.config.json'
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            for k in ('voice', 'voice_female', 'voice_male'):
                if k in data and str(data[k]).strip():
                    cfg[k] = str(data[k]).strip()
        except (json.JSONDecodeError, OSError):
            pass
    voices = edge_voice_map()
    if cfg['voice_female'] not in voices:
        cfg['voice_female'] = '晓晓'
    if cfg['voice_male'] not in voices:
        cfg['voice_male'] = '云希'
    if cfg['voice'] not in voices:
        cfg['voice'] = cfg['voice_female']
    return cfg


def voice_map_path(folder: Path) -> Path:
    return folder / VOICE_MAP_FILE


def load_voice_map(folder: Path) -> dict | None:
    p = voice_map_path(folder)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('items'), dict):
        return None
    return data


def save_voice_map(folder: Path, data: dict) -> Path:
    p = voice_map_path(folder)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return p


def gender_label(voice: str, defaults: dict) -> str:
    if voice == defaults.get('voice_male'):
        return '男'
    if voice == defaults.get('voice_female'):
        return '女'
    if voice.startswith('云'):
        return '男'
    if voice.startswith('晓'):
        return '女'
    return '?'


def list_dub_jobs(
    folder: Path,
    *,
    lesson_from: int | None = None,
    lesson_to: int | None = None,
) -> list[dict]:
    from quick_bilingual_dub import find_bilingual_sub, list_videos

    jobs: list[dict] = []
    for i, video in enumerate(list_videos(folder, lesson_from=lesson_from, lesson_to=lesson_to), 1):
        sub = find_bilingual_sub(video)
        jobs.append({
            'index': i,
            'video': video,
            'video_name': video.name,
            'sub': sub,
            'sub_name': sub.name if sub else None,
        })
    return jobs


def resolve_voice_for_video(
    video_name: str,
    folder: Path,
    *,
    defaults: dict | None = None,
    voice_map: dict | None = None,
) -> str:
    defaults = defaults or load_tubeget_defaults()
    if voice_map is None:
        voice_map = load_voice_map(folder)
    if voice_map:
        items = voice_map.get('items') or {}
        if video_name in items:
            v = str(items[video_name]).strip()
            if v in edge_voice_map():
                return v
        male = voice_map.get('male') or defaults['voice_male']
        female = voice_map.get('female') or defaults['voice_female']
    else:
        male = defaults['voice_male']
        female = defaults['voice_female']
    return defaults.get('voice') or female


def print_voice_catalog(defaults: dict) -> None:
    f_def = defaults['voice_female']
    m_def = defaults['voice_male']
    voices = edge_voice_map()
    print('  女声列表：', end='')
    print(', '.join(f'{n}({voices[n].split("-")[-1].replace("Neural", "")})' for n in female_voice_names()))
    print(f'    → 默认女声：{f_def} ({voices[f_def]})')
    print('  男声列表：', end='')
    print(', '.join(f'{n}({voices[n].split("-")[-1].replace("Neural", "")})' for n in male_voice_names()))
    print(f'    → 默认男声：{m_def} ({voices[m_def]})')


def _short_title(name: str, max_len: int = 58) -> str:
    if len(name) <= max_len:
        return name
    return name[: max_len - 3] + '...'


def _parse_pick(raw: str, *, female: str, male: str, current: str) -> str | None:
    t = (raw or '').strip().lower()
    if t in ('', 'c', 'keep', '保持'):
        return current
    if t in ('f', '女', '0', '2'):
        return female
    if t in ('m', '男', '1'):
        return male
    if t in edge_voice_map():
        return t
    for name in edge_voice_map():
        if t == name.lower():
            return name
    return None


def interactive_pick_voices(
    folder: Path,
    *,
    lesson_from: int | None = None,
    lesson_to: int | None = None,
    allow_saved: bool = True,
) -> dict | None:
    defaults = load_tubeget_defaults()
    female = defaults['voice_female']
    male = defaults['voice_male']
    jobs = list_dub_jobs(folder, lesson_from=lesson_from, lesson_to=lesson_to)

    print('=' * 54)
    print('  配音员选择（每条视频可选男/女，对应 Edge 神经语音）')
    print_voice_catalog(defaults)
    print('=' * 54)

    if not jobs:
        print('  [错误] 文件夹内未找到可配音视频')
        return None

    dub_jobs = [j for j in jobs if j['sub']]
    if not dub_jobs:
        print('  [错误] 有视频但未找到双语字幕（en-zh-merge / bilingual 等）')
        return None

    if allow_saved and voice_map_path(folder).is_file():
        print(f'\n  已存在配置：{VOICE_MAP_FILE}')
        print('  [Enter] 逐项指定  |  f 全部女  |  m 全部男  |  s 沿用已保存')
        mode = input('  模式: ').strip().lower()
        if mode == 's':
            data = load_voice_map(folder)
            if data:
                print('  沿用已保存配音员配置')
                return data
        elif mode == 'f':
            items = {j['video_name']: female for j in dub_jobs}
            data = {'version': 1, 'female': female, 'male': male, 'items': items}
            save_voice_map(folder, data)
            print(f'  已全部设为女声 {female} → {VOICE_MAP_FILE}')
            return data
        elif mode == 'm':
            items = {j['video_name']: male for j in dub_jobs}
            data = {'version': 1, 'female': female, 'male': male, 'items': items}
            save_voice_map(folder, data)
            print(f'  已全部设为男声 {male} → {VOICE_MAP_FILE}')
            return data
    else:
        print('\n  [Enter] 逐项指定  |  f 全部女  |  m 全部男')
        mode = input('  模式: ').strip().lower()
        if mode == 'f':
            items = {j['video_name']: female for j in dub_jobs}
            data = {'version': 1, 'female': female, 'male': male, 'items': items}
            save_voice_map(folder, data)
            return data
        if mode == 'm':
            items = {j['video_name']: male for j in dub_jobs}
            data = {'version': 1, 'female': female, 'male': male, 'items': items}
            save_voice_map(folder, data)
            return data

    existing = (load_voice_map(folder) or {}).get('items') or {}
    items: dict[str, str] = {}

    print(f'\n  共 {len(dub_jobs)} 个待配音（f=女/{female}  m=男/{male}  Enter=保持默认女）\n')

    for j in dub_jobs:
        name = j['video_name']
        cur = str(existing.get(name) or female)
        g = gender_label(cur, defaults)
        print(f"  [{j['index']}] {_short_title(name)}")
        print(f"       字幕: {j['sub_name']}")
        raw = input(f'       配音 [{g}/{cur}] (f/m/Enter): ').strip()
        picked = _parse_pick(raw, female=female, male=male, current=cur)
        if picked is None:
            print(f'       无效输入，使用 {cur}')
            picked = cur
        items[name] = picked
        print(f'       → {gender_label(picked, defaults)}/{picked}')
        print()

    data = {'version': 1, 'female': female, 'male': male, 'items': items}
    out = save_voice_map(folder, data)
    print(f'  已保存：{out.name}')
    return data


def main() -> int:
    _configure_stdio()
    ap = argparse.ArgumentParser(description='按视频选择男/女配音员')
    ap.add_argument('folder', type=Path)
    ap.add_argument('--from-lesson', type=int, default=None)
    ap.add_argument('--to-lesson', type=int, default=None)
    ap.add_argument('--list-only', action='store_true', help='仅列出视频与字幕')
    ap.add_argument('--no-saved', action='store_true', help='不提示沿用已保存配置')
    args = ap.parse_args()

    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f'[错误] 不是文件夹：{folder}')
        return 1

    lesson_from, lesson_to = args.from_lesson, args.to_lesson
    if lesson_from is not None and lesson_to is not None and lesson_from > lesson_to:
        lesson_from, lesson_to = lesson_to, lesson_from

    if args.list_only:
        defaults = load_tubeget_defaults()
        print_voice_catalog(defaults)
        jobs = list_dub_jobs(folder, lesson_from=lesson_from, lesson_to=lesson_to)
        if not jobs:
            print('  （无视频）')
            return 1
        saved = (load_voice_map(folder) or {}).get('items') or {}
        for j in jobs:
            sub = j['sub_name'] or '（无字幕）'
            v = saved.get(j['video_name'], defaults['voice_female'])
            print(f"  [{j['index']}] {j['video_name']}")
            print(f"       字幕: {sub}  配音: {gender_label(v, defaults)}/{v}")
        return 0

    data = interactive_pick_voices(
        folder,
        lesson_from=lesson_from,
        lesson_to=lesson_to,
        allow_saved=not args.no_saved,
    )
    return 0 if data else 1


if __name__ == '__main__':
    raise SystemExit(main())
