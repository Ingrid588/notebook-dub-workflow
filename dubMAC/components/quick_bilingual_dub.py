#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双语字幕直配预览：提取 bilingual.srt 中文行 TTS，合并 *_C.mp4。

默认：无 Gemini，直接念现有中文（最快试听）。
可选 --fit local / --fit gemini：口播压缩（图2 短摘要风格）后再配音。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = Path(__file__).parent.resolve()
VIDEO_EXT = {'.mp4', '.mkv', '.avi', '.mov', '.webm'}


def resolve_work_folder(raw: str) -> Path:
    """解析工作目录；Windows 超长路径加 \\\\?\\ 前缀。"""
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError:
        p = p.absolute()
    s = str(p)
    if os.name == 'nt' and len(s) >= 240 and not s.startswith('\\\\?\\'):
        p = Path('\\\\?\\' + s)
    return p


def _py_cmd() -> list[str]:
    if sys.platform == 'win32':
        try:
            r = subprocess.run(
                ['py', '-3.12', '--version'],
                capture_output=True, text=True, errors='replace',
            )
            if r.returncode == 0:
                return ['py', '-3.12']
        except OSError:
            pass
    return [sys.executable]


PY = _py_cmd()


def load_cfg() -> dict:
    defaults = {
        'voice': '晓晓',
        'rate': '+0%',
        'original_vol': 0.0,
        'dub_vol': 3.0,
        'no_original': True,
    }
    p = HERE / 'tubeget.config.json'
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            defaults.update({
                k: data[k] for k in defaults
                if k in data and not str(k).startswith('_')
            })
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


def load_tubeget_full() -> dict:
    p = HERE / 'tubeget.config.json'
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {}


def find_bilingual_sub(video: Path) -> Path | None:
    from dub_naming import find_bilingual_sub_for_video
    return find_bilingual_sub_for_video(video)


def list_videos(
    folder: Path,
    *,
    lesson_from: int | None = None,
    lesson_to: int | None = None,
) -> list[Path]:
    from dub_naming import filter_paths_by_lesson_range, is_dub_output

    out = []
    for f in folder.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in VIDEO_EXT:
            continue
        if is_dub_output(f):
            continue
        if '_字幕' in f.stem or '_预览' in f.stem:
            continue
        out.append(f)
    out = sorted(out, key=lambda p: p.name.lower())
    return filter_paths_by_lesson_range(out, lesson_from, lesson_to)


def clear_dub_cache(sub: Path, video: Path) -> None:
    """删除本次后缀对应的配音缓存（不删其它 _C/_C2/… 旧成片）。"""
    import shutil
    from dub_naming import dub_output_mp3_path, dub_video_path, ensure_dub_tag, kokoro_samples_dir_from_work
    from step1_subtitle_to_audio import dub_status_path, dub_work_dir

    ensure_dub_tag(video.parent, video)
    targets = [
        dub_work_dir(str(sub)),
        dub_status_path(str(sub)),
        dub_output_mp3_path(str(sub)),
        dub_video_path(video),
        kokoro_samples_dir_from_work(dub_work_dir(str(sub))),
    ]
    for p in targets:
        p = Path(p)
        if not p.exists():
            continue
        if p.is_dir():
            print(f'  删除缓存目录：{p.name}/')
            shutil.rmtree(p, ignore_errors=True)
        else:
            print(f'  删除旧文件：{p.name}')
            try:
                p.unlink()
            except OSError:
                pass


def _load_cues_quiet(sub: Path) -> list:
    from step1_subtitle_to_audio import parse_srt
    text = sub.read_text(encoding='utf-8-sig')
    return parse_srt(text)


def _attach_window_params(cues: list, tubeget: dict) -> None:
    from speech_activity import compute_window_params, speech_activity_config
    sac = speech_activity_config(tubeget)
    for cue in cues:
        if cue.get('window_params'):
            continue
        win = max(0.3, cue['end'] - cue['start'])
        cue['window_params'] = compute_window_params(win, sac)


def fit_subtitle_for_dub(sub: Path, mode: str) -> Path:
    """
    口播压缩：local=规则；gemini=本地+Gemini Speech Activity（图2 短摘要风格）。
    写出 {原名}.quick-fit.srt，供 step1 使用。
    """
    if mode not in ('local', 'gemini'):
        return sub

    cues = _load_cues_quiet(sub)
    if not cues:
        print('  [压缩] 字幕为空，跳过')
        return sub

    tubeget = load_tubeget_full()
    prep = __import__('ai_shorten', fromlist=['dub_prep_config']).dub_prep_config(tubeget)
    ratio = float(tubeget.get('zh_en_char_ratio', 1.0))

    if mode in ('local', 'gemini'):
        from zh_compress import apply_local_compress_to_cues
        cues, lstats = apply_local_compress_to_cues(cues, ratio, prep)
        print(
            f'  [本地压缩] 超长 {lstats["tried"]} 条 → 改短 {lstats["fixed"]} 条'
            f'（策略 {lstats.get("strategy_source", "?")}）')

    if mode == 'gemini':
        from ai_shorten import (
            DEFAULT_GEMINI_MODEL,
            effective_gemini_model,
            rewrite_cues_speech_aligned,
        )
        from gemini_secrets import gemini_api_key_from_env

        api_key = gemini_api_key_from_env()
        model = effective_gemini_model(
            tubeget.get('ai_shorten_model_gemini') or DEFAULT_GEMINI_MODEL)

        async def _run_gemini() -> tuple[list, dict]:
            auth = await __import__(
                'ai_shorten', fromlist=['ensure_gemini_api']
            ).ensure_gemini_api(
                api_key, model, interactive=False, prompt_if_missing_key=False,
            )
            if not auth:
                return cues, {'skipped': True, 'reason': 'no_api_key'}
            key, use_model = auth
            _attach_window_params(cues, tubeget)
            return await rewrite_cues_speech_aligned(
                cues,
                api_key=key,
                model=use_model,
                only_problem_cues=False,
                prep_cfg=prep,
                zh_en_ratio=ratio,
                batch_size=int(tubeget.get('gemini_rewrite_batch', 20)),
            )

        cues, gstats = asyncio.run(_run_gemini())
        if gstats.get('skipped'):
            print('  [Gemini 压缩] 未配置 GEMINI_API_KEY，仅使用本地压缩')
            print('           见 components\\GEMINI_API_KEY_SETUP.txt')
        else:
            print(
                f'  [Gemini 口播压缩] 发送 {gstats.get("sent", 0)} 条，'
                f'改写 {gstats.get("changed", 0)} 条')

    out = sub.parent / f'{sub.stem}.quick-fit.srt'
    from dub_naming import write_bilingual_srt
    write_bilingual_srt(cues, out)
    print(f'  [压缩输出] {out.name}')
    return out


def print_subtitle_coverage(sub: Path, video: Path) -> None:
    """打印字幕时间轴覆盖与空档。"""
    from cue_synth_eligibility import chinese_speech_text
    from step1_subtitle_to_audio import load_subtitle, get_audio_duration

    cues = load_subtitle(str(sub))
    if not cues:
        print('  [字幕] 空文件')
        return

    with_zh = sum(
        1 for c in cues if chinese_speech_text(c.get('text', '')).strip())
    no_zh = len(cues) - with_zh
    first = cues[0]['start']
    last = cues[-1]['end']
    span = last - first

    gaps: list[tuple[float, float, float]] = []
    for a, b in zip(cues, cues[1:]):
        g = b['start'] - a['end']
        if g >= 2.0:
            gaps.append((a['end'], b['start'], g))

    vd = 0.0
    try:
        vd = get_audio_duration(str(video))
        if vd <= 0:
            r = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', str(video)],
                capture_output=True, text=True, errors='replace')
            vd = float(r.stdout.strip() or 0)
    except Exception:
        pass

    print(f'  [字幕轴] 共 {len(cues)} 条 | 含中文 {with_zh} | 无中文跳过 {no_zh}')
    print(f'  [字幕轴] 覆盖 {first:.1f}s ~ {last:.1f}s（跨度 {span:.1f}s）', end='')
    if vd > 0:
        tail = max(0.0, vd - last)
        print(f' | 视频 {vd:.1f}s', end='')
        if tail >= 1.0:
            print(f' | 片尾无字幕 {tail:.1f}s', end='')
    print()

    if gaps:
        print(f'  [字幕轴] 中间空档 ≥2s：{len(gaps)} 处')
        for s, e, g in gaps[:5]:
            print(f'      {s:.1f}s ~ {e:.1f}s  空 {g:.1f}s')
        if len(gaps) > 5:
            print(f'      … 另有 {len(gaps) - 5} 处')


def run_step1(sub: Path, video: Path, cfg: dict) -> int:
    from dub_naming import dub_output_mp3_path
    mp3 = dub_output_mp3_path(str(sub), cfg['voice'])
    print('\n  [清理] 本次配音缓存…')
    clear_dub_cache(sub, video)
    cmd = PY + [
        '-u', str(HERE / 'step1_subtitle_to_audio.py'),
        str(sub),
        '--voice', str(cfg['voice']),
        '--rate', str(cfg['rate']),
        '--no-gemini',
        '--no-filter',
        '--no-local-fallback',
    ]
    print(f'\n  [1/2] 配音 MP3 → {mp3.name}')
    env = os.environ.copy()
    env['TUBEGET_QUICK_BILINGUAL'] = '1'
    env['TUBEGET_NO_GEMINI'] = '1'
    env['TUBEGET_NEVER_TRUNCATE_DUB'] = '1'
    env['TUBEGET_VIDEO_FOR_DURATION'] = str(video.resolve())
    from dub_naming import active_dub_tag
    env['TUBEGET_DUB_TAG'] = active_dub_tag()
    return subprocess.run(cmd, cwd=str(sub.parent), env=env).returncode


def run_step2(video: Path, mp3: Path, cfg: dict) -> int:
    from dub_naming import dub_video_path
    out = dub_video_path(video)
    cmd = PY + [
        '-u', str(HERE / 'step2_merge_video.py'),
        str(video), str(mp3),
        '--dub-vol', str(cfg['dub_vol']),
        '--output', str(out),
    ]
    if cfg.get('no_original') or float(cfg.get('original_vol', 0)) <= 0:
        cmd.append('--no-original')
    else:
        cmd.extend(['--original-vol', str(cfg['original_vol'])])
    print(f'\n  [2/2] 合并视频 → {out.name}')
    return subprocess.run(cmd, cwd=str(video.parent)).returncode


def process_folder(
    folder: Path,
    fit_mode: str = '',
    *,
    lesson_from: int | None = None,
    lesson_to: int | None = None,
) -> int:
    cfg = load_cfg()
    os.environ['TUBEGET_QUICK_BILINGUAL'] = '1'
    os.environ['TUBEGET_NO_GEMINI'] = '1'

    print('=' * 50)
    if fit_mode == 'gemini':
        title = '双语直配 + Gemini 口播压缩（实验用，路线 A 一般不需要）'
    elif fit_mode == 'local':
        title = '双语直配 + 本地口播压缩'
    else:
        title = '双语直配预览（路线 A：NotebookLM 实验，无商用管线）'
    print(f'  {title}')
    print(f'  文件夹：{folder}')
    if lesson_from is not None or lesson_to is not None:
        lo = lesson_from if lesson_from is not None else '—'
        hi = lesson_to if lesson_to is not None else '—'
        print(f'  课序号：{lo} ~ {hi}')
    print(f'  配音员：{cfg["voice"]}  语速：{cfg["rate"]}')
    if not fit_mode:
        print('  商用请走：fit_translate + commercial_dub_test.bat（见 WORKFLOWS.txt）')
        print('  NotebookLM 提示词：components\\notebooklm_dub_prompt.txt')
    print('  原声：预览默认静音')
    print('  引擎：仅 Edge 中文')
    print('  命名：自动递增 _C → _C2 → _C3 …（保留旧成片；指定 set TUBEGET_DUB_TAG=_C2 可固定后缀）')
    print('=' * 50)

    videos = list_videos(folder, lesson_from=lesson_from, lesson_to=lesson_to)
    if not videos:
        print('  [错误] 未找到视频文件')
        if lesson_from is not None or lesson_to is not None:
            from dub_naming import list_lesson_numbers_in_folder
            nums = list_lesson_numbers_in_folder(folder)
            if nums:
                print(f'  [提示] 本文件夹课序号：{", ".join(str(n) for n in nums)}')
        return 1

    ok_n = 0
    fail_n = 0
    for video in videos:
        sub = find_bilingual_sub(video)
        print(f'\n── {video.name} ──')
        if not sub:
            print('  [跳过] 未找到 bilingual 字幕')
            fail_n += 1
            continue
        print(f'  字幕：{sub.name}')
        if '.fit-dub' in sub.stem:
            print('  [进框翻译] 使用 fit-dub 字幕（短口播稿，推荐）')
        elif 'merge' in sub.name.lower() and 'fit-dub' not in sub.name.lower():
            print('  [提示] en-zh-merge 未进框时易偏长/中英混杂，建议先 fit_translate.bat')

        dub_sub = sub
        if fit_mode:
            print(f'\n  [口播压缩] 模式={fit_mode}')
            dub_sub = fit_subtitle_for_dub(sub, fit_mode)

        print_subtitle_coverage(dub_sub, video)

        code1 = run_step1(dub_sub, video, cfg)
        if code1 != 0:
            print(f'  [失败] 步骤一退出码 {code1}')
            fail_n += 1
            continue

        from dub_naming import dub_output_mp3_path
        mp3 = dub_output_mp3_path(str(dub_sub), cfg['voice'])
        if not mp3.is_file() or mp3.stat().st_size < 1000:
            print('  [失败] 未生成有效 MP3')
            fail_n += 1
            continue

        code2 = run_step2(video, mp3, cfg)
        if code2 != 0:
            print(f'  [失败] 步骤二退出码 {code2}')
            fail_n += 1
            continue
        ok_n += 1
        print('  [OK] 完成')

    print(f'\n{"=" * 50}')
    print(f'  完成：成功 {ok_n}  失败/跳过 {fail_n}')
    print('  输出：*_C.mp4 / *_C2.mp4 / *_C3.mp4 …（旧版保留）')
    if fit_mode:
        print('  压缩字幕：*.quick-fit.srt（可对照原 bilingual）')
    return 0 if ok_n else 1


def _parse_lesson_arg(rest: list[str], flag: str) -> tuple[int | None, list[str]]:
    if flag not in rest:
        return None, rest
    idx = rest.index(flag)
    if idx + 1 >= len(rest):
        raise ValueError(f'缺少 {flag} 的参数')
    val = int(rest[idx + 1])
    rest = rest[:idx] + rest[idx + 2:]
    return val, rest


def main() -> int:
    fit_mode = ''
    lesson_from: int | None = None
    lesson_to: int | None = None
    rest = sys.argv[1:]
    try:
        lesson_from, rest = _parse_lesson_arg(rest, '--from-lesson')
        lesson_to, rest = _parse_lesson_arg(rest, '--to-lesson')
    except ValueError as e:
        print(f'[错误] {e}')
        return 1
    if lesson_from is not None and lesson_to is not None and lesson_from > lesson_to:
        lesson_from, lesson_to = lesson_to, lesson_from

    if '--fit' in rest:
        idx = rest.index('--fit')
        if idx + 1 < len(rest):
            fit_mode = rest[idx + 1].strip().lower()
        rest = rest[:idx] + rest[idx + 2:]
    elif len(rest) >= 2 and rest[-1].strip().lower() in ('local', 'gemini'):
        fit_mode = rest[-1].strip().lower()
        rest = rest[:-1]

    if rest:
        folder = resolve_work_folder(rest[0])
    else:
        folder = resolve_work_folder(str(Path.cwd()))

    if fit_mode not in ('', 'local', 'gemini'):
        print(f'[错误] 未知压缩模式：{fit_mode}（可用 local / gemini）')
        return 1
    if not folder.is_dir():
        print(f'[错误] 不是文件夹：{folder}')
        return 1
    return process_folder(
        folder, fit_mode,
        lesson_from=lesson_from, lesson_to=lesson_to,
    )


if __name__ == '__main__':
    raise SystemExit(main())
