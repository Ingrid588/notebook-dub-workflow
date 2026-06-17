#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NotebookLM 半自动配音翻译（实验性）

全自动：用户一次提交整文件夹/整段课序号，脚本负责分批、分条、重试、断点续跑。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).parent.resolve()
ROOT = HERE.parent
PROMPT_CORE = (HERE / 'notebooklm_dub_prompt.txt').read_text(encoding='utf-8')
NOTEBOOK_TITLE = 'TubeGet-Dub-Work'
MAX_ROUNDS = 25
DEFAULT_UPLOAD_BATCH = 1
MAX_UPLOAD_BATCH = 2
DEFAULT_CHUNK_CUES = 40
MIN_CHUNK_CUES = 10
BATCH_DELAY_SEC = 5.0
MAX_API_RETRIES = 4
CONTINUE_HINT = '继续。从 [BATCH_CONTINUE] 标记处接着写，不要重复已输出条目。'
PROGRESS_FILE = '_notebooklm_progress.json'
_RUN_NOTEBOOK_TAG = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6]
_ACTIVE_NOTEBOOK_ID: str | None = None
_ACTIVE_NOTEBOOK_TITLE: str | None = None
_NOTEBOOK_ROTATE_N = 0
TEMP_DIR_NAME = 'temp'


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def auth_ok() -> bool:
    r = subprocess.run(
        [sys.executable, '-m', 'notebooklm', 'auth', 'check', '--test', '--json'],
        capture_output=True, text=True, errors='replace',
    )
    if r.returncode != 0:
        return False
    try:
        data = json.loads(r.stdout)
        return data.get('status') == 'ok'
    except json.JSONDecodeError:
        return False


def _playwright_browser_cache() -> Path:
    import os

    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Caches' / 'ms-playwright'
    if sys.platform == 'win32':
        local = os.environ.get('LOCALAPPDATA', '')
        if local:
            return Path(local) / 'ms-playwright'
    return Path.home() / '.cache' / 'ms-playwright'


def _playwright_chromium_ok() -> bool:
    base = _playwright_browser_cache()
    if not base.is_dir():
        return False
    if sys.platform == 'darwin':
        return any(
            p.is_file()
            for p in base.glob('chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium')
        )
    if sys.platform == 'win32':
        return any(p.is_file() for p in base.glob('chromium-*/chrome-win64/chrome.exe'))
    return any(p.is_file() for p in base.glob('chromium-*/chrome-linux/chrome'))


def ensure_notebooklm_ready() -> bool:
    print('[准备] 检查 NotebookLM 依赖…')
    r = subprocess.run(
        [sys.executable, '-m', 'pip', 'install', 'notebooklm-py[browser]', 'rookiepy', '-q'],
        capture_output=True, text=True, errors='replace',
    )
    if r.returncode != 0:
        print('[错误] 安装 notebooklm-py 失败')
        if r.stderr:
            print(r.stderr.strip()[:400])
        return False

    if not _playwright_chromium_ok():
        print('[准备] 安装 Playwright Chromium（仅首次，约 180MB）…')
        r = subprocess.run([sys.executable, '-m', 'playwright', 'install', 'chromium'])
        if r.returncode != 0:
            print('[错误] playwright install chromium 失败')
            return False

    if auth_ok():
        print('[准备] NotebookLM 已登录，继续…')
        return True

    print('[准备] 需要登录 NotebookLM（浏览器将打开，登录成功后一般不用重复）')
    r = subprocess.run([sys.executable, '-m', 'notebooklm', 'login'])
    if r.returncode != 0:
        print('[错误] 登录失败')
        return False
    if auth_ok():
        print('[准备] 登录成功')
        return True
    print('[错误] 登录后验证仍失败')
    return False


def find_bilingual_srts(
    folder: Path,
    *,
    lesson_from: int | None = None,
    lesson_to: int | None = None,
) -> list[Path]:
    from dub_naming import discover_bilingual_subtitles, filter_paths_by_lesson_range

    srts = discover_bilingual_subtitles(folder)
    return filter_paths_by_lesson_range(srts, lesson_from, lesson_to)


def print_lesson_panel(folder: Path) -> None:
    from dub_naming import list_lesson_numbers_in_folder

    nums = list_lesson_numbers_in_folder(folder)
    if not nums:
        print('  [课序号] 本文件夹未识别到「N - 标题」格式，将处理全部双语字幕')
        return
    print(f'  [课序号] 本文件夹可识别：{nums[0]} ~ {nums[-1]}（共 {len(nums)} 课）')
    print(f'            列表：{", ".join(str(n) for n in nums)}')


def load_cues(path: Path) -> list[dict]:
    from step1_subtitle_to_audio import parse_srt

    return parse_srt(path.read_text(encoding='utf-8-sig'))


def temp_work_dir(folder: Path) -> Path:
    p = folder / TEMP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def cues_to_srt(cues: list[dict], *, start_index: int = 1) -> str:
    from dub_naming import format_srt_time

    blocks: list[str] = []
    for i, cue in enumerate(cues, start_index):
        blocks.append(
            f'{i}\n'
            f'{format_srt_time(cue["start"])} --> {format_srt_time(cue["end"])}\n'
            f'{cue["text"]}\n'
        )
    return '\n'.join(blocks)


def build_merged_upload(folder: Path, srts: list[Path], *, batch_tag: str = '') -> Path:
    parts: list[str] = []
    for s in srts:
        parts.append(f'===FILE: {s.name}===')
        parts.append(s.read_text(encoding='utf-8-sig').strip())
        parts.append('')
    name = '_notebooklm_upload_bundle.txt'
    if batch_tag:
        name = f'_notebooklm_upload_bundle_{batch_tag}.txt'
    bundle = temp_work_dir(folder) / name
    bundle.write_text('\n'.join(parts), encoding='utf-8')
    return bundle


def plan_upload_batches(
    srts: list[Path],
    *,
    max_files: int = DEFAULT_UPLOAD_BATCH,
) -> list[list[Path]]:
    """每批默认 1 个文件（整文件仍可能再按 cue 分段）。"""
    max_files = max(1, min(max_files, MAX_UPLOAD_BATCH))
    batches: list[list[Path]] = []
    buf: list[Path] = []
    for s in srts:
        if buf and len(buf) >= max_files:
            batches.append(buf)
            buf = []
        buf.append(s)
    if buf:
        batches.append(buf)
    return batches


def is_response_too_large(exc: BaseException) -> bool:
    name = type(exc).__name__
    msg = str(exc)
    return (
        'RPCResponseTooLarge' in name
        or '52428800' in msg
        or 'response exceeded' in msg.lower()
    )


def is_transient_error(exc: BaseException) -> bool:
    if is_response_too_large(exc):
        return True
    msg = str(exc).lower()
    return any(x in msg for x in ('timeout', '429', '503', '502', 'rate', 'temporarily'))


def is_source_limit_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        'source limit' in msg
        or '50/50 sources' in msg
        or 'failed to register file source' in msg
    )


def _next_notebook_title() -> str:
    global _NOTEBOOK_ROTATE_N
    _NOTEBOOK_ROTATE_N += 1
    return f'{NOTEBOOK_TITLE}-{_RUN_NOTEBOOK_TAG}-n{_NOTEBOOK_ROTATE_N:03d}'


async def _create_new_work_notebook(client) -> tuple[str, str]:
    global _ACTIVE_NOTEBOOK_ID, _ACTIVE_NOTEBOOK_TITLE
    title = _next_notebook_title()
    nb = await client.notebooks.create(title)
    _ACTIVE_NOTEBOOK_ID = nb.id
    _ACTIVE_NOTEBOOK_TITLE = title
    print(f'  新建笔记本：{title} ({nb.id[:8]}…)')
    return nb.id, title


async def _ensure_work_notebook(client) -> tuple[str, str]:
    global _ACTIVE_NOTEBOOK_ID, _ACTIVE_NOTEBOOK_TITLE
    if _ACTIVE_NOTEBOOK_ID and _ACTIVE_NOTEBOOK_TITLE:
        return _ACTIVE_NOTEBOOK_ID, _ACTIVE_NOTEBOOK_TITLE
    return await _create_new_work_notebook(client)


def progress_path(folder: Path) -> Path:
    return folder / PROGRESS_FILE


def load_progress(folder: Path) -> dict:
    p = progress_path(folder)
    if not p.is_file():
        return {'completed': [], 'failed': []}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {'completed': [], 'failed': []}


def save_progress(folder: Path, data: dict) -> None:
    data['updated'] = datetime.now(timezone.utc).isoformat()
    progress_path(folder).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def mark_file_done(folder: Path, name: str) -> None:
    prog = load_progress(folder)
    done = set(prog.get('completed', []))
    done.add(name)
    prog['completed'] = sorted(done)
    prog['failed'] = [f for f in prog.get('failed', []) if f.get('file') != name]
    save_progress(folder, prog)


def mark_file_failed(folder: Path, name: str, error: str) -> None:
    prog = load_progress(folder)
    failed = [f for f in prog.get('failed', []) if f.get('file') != name]
    failed.append({'file': name, 'error': error[:500], 'at': datetime.now(timezone.utc).isoformat()})
    prog['failed'] = failed
    save_progress(folder, prog)


def file_already_done(folder: Path, srt: Path, *, force: bool) -> bool:
    if force:
        return False
    return srt.name in load_progress(folder).get('completed', [])


def build_task_prompt(
    srts: list[Path],
    *,
    chunk_note: str = '',
) -> str:
    file_lines = '\n'.join(f'  {i}. {p.name}' for i, p in enumerate(srts, 1))
    chunk_block = f'\n{chunk_note}\n' if chunk_note else ''
    batch_rules = textwrap.dedent(f"""
        【批量任务】请按顺序处理上传包中的全部字幕，不要中途问我是否继续。
        {chunk_block}
        处理顺序：
        {{files}}

        【SRT 翻译与格式准则 · 必须严格执行】
        1. 总原则：忠实原意、精炼表达；中文长度不超过对应英文行字符数的一半。
        2. 文件名：每个文件块用 ===FILE_START: 原文件名.srt=== … ===FILE_END===；
        3. 格式纯净：中文行绝对禁止引用索引 [4-6]、[1]、[i] 及任何括号注释。
        4. 长度：中文严控在英文字数 1/2 以内；删除口水词。
        5. 本行归属：本条英文的名词、动词须在本条中文表达；禁止把本行语义压到下一行。
        6. 跨行借字（仅下提上）：下条极短时可上提；本条≤1.2s 最多 1 字或「。」。
        7. 气口：借字长行末尾用「——」。
        8. 专有名词必留：CloudWatch/Glue/Transcribe 等必须点名；禁止泛化为「指标/分析」。

        【执行规则】
        - 每个文件：===FILE_START: 原文件名.srt=== … ===FILE_END: 原文件名.srt===
        - 只改中文行；英文、序号、时间轴不改；禁止省略条目。
        - 写不下时在完整条目处停，末尾 [BATCH_CONTINUE n/{{total}} 文件名 从第__条继续]
        - 全部完成后：[BATCH_DONE {{total}}/{{total}}]

        请从第 1 个文件第 1 条开始。
    """).format(files=file_lines, total=len(srts)).strip()
    core = PROMPT_CORE.split('===')[0].strip()
    return core + '\n\n' + batch_rules


def extract_srt_blocks(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    pattern = re.compile(
        r'===FILE_START:\s*(?P<name>.+?)\s*===\s*(?P<body>.*?)\s*===FILE_END:\s*\1\s*===',
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        name = m.group('name').strip()
        body = m.group('body').strip()
        body = re.sub(r'^```(?:srt)?\s*', '', body, flags=re.IGNORECASE)
        body = re.sub(r'\s*```$', '', body)
        if body:
            out[name] = body + '\n'
    return out


def needs_continue(text: str) -> bool:
    if '[BATCH_DONE' in text:
        return False
    return '[BATCH_CONTINUE' in text or 'FILE_END' not in text


def apply_translated_chunk(orig_cues: list[dict], translated_body: str) -> list[dict]:
    from ai_shorten import split_bilingual_text

    trans = load_cues_from_text(translated_body)
    if not trans:
        raise ValueError('分段回复未解析到有效 SRT')

    n = min(len(orig_cues), len(trans))
    if n < len(orig_cues):
        print(f'  [警告] 分段条目数 {len(trans)} < 期望 {len(orig_cues)}，按前 {n} 条合并')

    out: list[dict] = []
    for i in range(n):
        orig = orig_cues[i]
        en, _ = split_bilingual_text(orig.get('text', ''))
        tr_text = trans[i].get('text', '')
        tr_lines = [ln.strip() for ln in tr_text.splitlines() if ln.strip()]
        if len(tr_lines) >= 2:
            new_body = f'{tr_lines[0]}\n{tr_lines[1]}'
        elif len(tr_lines) == 1:
            _, zh = split_bilingual_text(orig.get('text', ''))
            if en and re.search(r'[\u4e00-\u9fff]', tr_lines[0]):
                new_body = f'{en}\n{tr_lines[0]}'
            elif en:
                new_body = f'{en}\n{tr_lines[0]}'
            else:
                new_body = tr_lines[0]
        else:
            new_body = orig.get('text', '')
        out.append({**orig, 'text': new_body})
    return out


def load_cues_from_text(text: str) -> list[dict]:
    from step1_subtitle_to_audio import parse_srt

    return parse_srt(text)


async def _ask_with_continue(client, nb_id: str, prompt: str) -> str:
    result = await client.chat.ask(nb_id, prompt)
    combined = result.answer or ''
    for rnd in range(2, MAX_ROUNDS + 1):
        if not needs_continue(combined):
            break
        print(f'  自动「继续」第 {rnd} 轮…')
        await asyncio.sleep(2.0)
        result = await client.chat.ask(nb_id, CONTINUE_HINT)
        combined += '\n\n' + (result.answer or '')
        if '[BATCH_DONE' in (result.answer or ''):
            break
    return combined


async def run_notebooklm_chat(
    folder: Path,
    srts: list[Path],
    prompt: str,
    *,
    batch_tag: str,
) -> str:
    from notebooklm import NotebookLMClient

    bundle = build_merged_upload(folder, srts, batch_tag=batch_tag)
    tag = batch_tag or 'single'
    (temp_work_dir(folder) / f'_notebooklm_task_prompt_{tag}.txt').write_text(prompt, encoding='utf-8')

    async with NotebookLMClient.from_storage() as client:
        nb_id, nb_title = await _ensure_work_notebook(client)
        print(f'  使用笔记本：{nb_title} ({nb_id[:8]}…)')
        src_title = f'dub_bundle_{batch_tag}' if batch_tag else 'dub_subtitle_bundle'
        try:
            await client.sources.add_file(nb_id, bundle, title=src_title, wait=True)
        except Exception as e:
            if is_source_limit_error(e):
                print('  [自动切换] 当前 notebook source 已达上限，创建新 notebook 重试…')
                nb_id, nb_title = await _create_new_work_notebook(client)
                await client.sources.add_file(nb_id, bundle, title=src_title, wait=True)
            else:
                raise
        print(f'  已上传：{bundle.name}（{len(srts)} 个文件）')

        combined = await _ask_with_continue(client, nb_id, prompt)
        print(f'  回复：{len(combined)} 字符')
        return combined


async def api_call_with_retry(coro_factory, *, label: str) -> str:
    delay = BATCH_DELAY_SEC
    last_exc: Exception | None = None
    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            if attempt > 1:
                print(f'  [重试 {attempt}/{MAX_API_RETRIES}] {label}，等待 {delay:.0f}s…')
                await asyncio.sleep(delay)
            return await coro_factory()
        except Exception as e:
            last_exc = e
            if attempt >= MAX_API_RETRIES or not is_transient_error(e):
                raise
            delay = min(delay * 1.8, 45.0)
    raise last_exc  # type: ignore[misc]


async def translate_files_once(
    folder: Path,
    srts: list[Path],
    *,
    batch_tag: str,
    chunk_note: str = '',
) -> str:
    prompt = build_task_prompt(srts, chunk_note=chunk_note)

    async def _run() -> str:
        return await run_notebooklm_chat(folder, srts, prompt, batch_tag=batch_tag)

    return await api_call_with_retry(_run, label=batch_tag)


async def translate_cue_range(
    folder: Path,
    srt: Path,
    cues: list[dict],
    start: int,
    end: int,
    *,
    tag: str,
    part: int,
    total_parts: int,
) -> str:
    chunk_cues = cues[start:end]
    chunk_srt = cues_to_srt(chunk_cues, start_index=start + 1)
    chunk_file = temp_work_dir(folder) / f'_notebooklm_chunk_{tag}.srt'
    chunk_file.write_text(chunk_srt, encoding='utf-8')

    note = textwrap.dedent(f"""
        【分段】文件 {srt.name} 第 {part}/{total_parts} 段（原字幕第 {start + 1}～{end} 条，共 {len(chunk_cues)} 条）。
        必须输出全部 {len(chunk_cues)} 条；序号与时间轴与输入完全一致；只改中文行。
    """).strip()

    prompt = build_task_prompt([chunk_file], chunk_note=note)

    async def _run() -> str:
        return await run_notebooklm_chat(
            folder, [chunk_file], prompt, batch_tag=f'{tag}_p{part}',
        )

    response = await api_call_with_retry(_run, label=f'{srt.name} 段 {part}/{total_parts}')

    blocks = extract_srt_blocks(response)
    body = blocks.get(chunk_file.name) or blocks.get(srt.name) or ''
    if not body:
        m = re.search(r'(\d+\s*\n\d{2}:\d{2}:\d{2},\d{3}\s*-->)', response, re.MULTILINE)
        if m:
            body = response[m.start():]
    if not body:
        raise ValueError(f'分段 {part} 未解析到 SRT：{srt.name}')
    return body


async def translate_srt_by_cues(
    folder: Path,
    srt: Path,
    *,
    tag: str,
    chunk_cues: int = DEFAULT_CHUNK_CUES,
) -> str:
    cues = load_cues(srt)
    if not cues:
        raise ValueError(f'字幕为空：{srt.name}')

    merged = [dict(c) for c in cues]
    ranges: list[tuple[int, int, int]] = []
    i = 0
    while i < len(cues):
        size = min(chunk_cues, len(cues) - i)
        ranges.append((i, i + size, size))
        i += size

    total_parts = len(ranges)
    print(f'  [分段] {srt.name}：{len(cues)} 条 → {total_parts} 段（每段约 {chunk_cues} 条）')

    idx = 0
    while idx < len(ranges):
        start, end, size = ranges[idx]
        part = idx + 1
        try:
            body = await translate_cue_range(
                folder, srt, cues, start, end,
                tag=tag, part=part, total_parts=total_parts,
            )
            updated = apply_translated_chunk(cues[start:end], body)
            for j, cue in enumerate(updated):
                merged[start + j] = cue
            idx += 1
            await asyncio.sleep(BATCH_DELAY_SEC)
        except Exception as e:
            if is_response_too_large(e) and size > MIN_CHUNK_CUES:
                half = max(MIN_CHUNK_CUES, size // 2)
                print(f'  [自动拆段] 第 {part} 段 {size} 条仍超 50MB → 拆为 {half}+{size - half}')
                ranges[idx:idx + 1] = [
                    (start, start + half, half),
                    (start + half, end, size - half),
                ]
                total_parts = len(ranges)
                continue
            raise

    final_body = cues_to_srt(merged)
    from dub_naming import write_subtitle_with_study_sync

    _, study, _ = write_subtitle_with_study_sync(srt, final_body)
    print(f'  [OK] 已写回：{srt.name}（{total_parts} 段合并）')
    if study:
        print(f'  [OK] 学习/配音字幕：{study.name}')
    return f'===FILE_START: {srt.name}===\n{final_body}\n===FILE_END: {srt.name}===\n'


async def translate_one_srt(folder: Path, srt: Path, *, tag: str) -> str:
    cues = load_cues(srt)
    use_chunks = len(cues) > DEFAULT_CHUNK_CUES or srt.stat().st_size > 120_000

    if not use_chunks:
        try:
            response = await translate_files_once(folder, [srt], batch_tag=tag)
            ok = write_srts(folder, [srt], response)
            if ok:
                return response
            print(f'  [警告] 整文件解析失败，改按 cue 分段：{srt.name}')
        except Exception as e:
            if not is_response_too_large(e):
                raise
            print(f'  [自动分段] 整文件超 50MB：{srt.name}')

    return await translate_srt_by_cues(folder, srt, tag=tag)


async def run_notebooklm_translate(
    folder: Path,
    srts: list[Path],
    *,
    batch_size: int = DEFAULT_UPLOAD_BATCH,
    force: bool = False,
) -> tuple[str, int, int]:
    staged = stage_temp_artifacts(folder)
    if staged:
        print(f'  [整理] 已移动 {staged} 个历史临时文件到 {TEMP_DIR_NAME}/')
    batches = plan_upload_batches(srts, max_files=batch_size)
    todo = [s for batch in batches for s in batch if not file_already_done(folder, s, force=force)]
    skipped = len(srts) - len(todo)

    if skipped:
        print(f'  [断点续跑] 跳过已完成 {skipped} 个，待处理 {len(todo)} 个')

    print(
        f'  [计划] 共 {len(todo)} 个字幕；'
        f'每批 {batch_size} 文件；长字幕自动按 ~{DEFAULT_CHUNK_CUES} 条分段；'
        f'批间间隔 {BATCH_DELAY_SEC:.0f}s'
    )

    combined_parts: list[str] = []
    ok_n = skipped
    fail_n = 0

    for i, srt in enumerate(todo, 1):
        print(f'\n  ── [{i}/{len(todo)}] {srt.name} ──')
        tag = f'{i:03d}'
        try:
            part = await translate_one_srt(folder, srt, tag=tag)
            combined_parts.append(part)
            mark_file_done(folder, srt.name)
            ok_n += 1
        except Exception as e:
            fail_n += 1
            print(f'  [失败] {srt.name}：{e}')
            mark_file_failed(folder, srt.name, str(e))
            if is_response_too_large(e):
                print('  [提示] 已尝试拆段；若仍失败请稍后重跑（会自动跳过已完成）')
        await asyncio.sleep(BATCH_DELAY_SEC)

    combined = '\n\n'.join(combined_parts)
    log_path = folder / '_notebooklm_last_response.txt'
    if combined:
        log_path.write_text(combined, encoding='utf-8')
        print(f'\n  回复日志：{log_path.name}（{len(combined)} 字符）')
    print(f'  累计成功：{ok_n}/{len(srts)}，失败：{fail_n}')
    return combined, ok_n, fail_n


def stage_temp_artifacts(folder: Path) -> int:
    tdir = temp_work_dir(folder)
    moved = 0
    patterns = (
        '_notebooklm_chunk_*.srt',
        '_notebooklm_upload_bundle*.txt',
        '_notebooklm_task_prompt_*.txt',
    )
    for pat in patterns:
        for src in folder.glob(pat):
            if not src.is_file():
                continue
            dst = tdir / src.name
            try:
                if dst.exists():
                    dst.unlink()
                src.replace(dst)
                moved += 1
            except OSError:
                pass
    return moved


def write_srts(folder: Path, srts: list[Path], response: str) -> int:
    from dub_naming import write_subtitle_with_study_sync

    blocks = extract_srt_blocks(response)
    if not blocks:
        return 0
    n = 0
    total_citations = 0
    for src in srts:
        body = blocks.get(src.name)
        if not body:
            print(f'  [跳过] 未在回复中找到：{src.name}')
            continue
        _, study, removed = write_subtitle_with_study_sync(src, body)
        total_citations += removed
        print(f'  [OK] 已写回：{src.name}')
        if study:
            print(f'  [OK] 学习/配音字幕：{study.name}')
        n += 1
    if total_citations:
        print(f'  [清理] 引用索引 {total_citations} 处')
    return n


def run_dub(
    folder: Path,
    *,
    lesson_from: int | None = None,
    lesson_to: int | None = None,
) -> int:
    script = HERE / 'quick_bilingual_dub.py'
    cmd = [sys.executable, '-u', str(script), str(folder)]
    if lesson_from is not None:
        cmd.extend(['--from-lesson', str(lesson_from)])
    if lesson_to is not None:
        cmd.extend(['--to-lesson', str(lesson_to)])
    return subprocess.run(cmd).returncode


def main() -> int:
    _configure_stdio()
    ap = argparse.ArgumentParser(description='NotebookLM 半自动配音翻译（实验）')
    ap.add_argument('folder', type=Path, nargs='?', default=None)
    ap.add_argument('--ensure-ready', action='store_true')
    ap.add_argument('--no-dub', action='store_true')
    ap.add_argument('--from-lesson', type=int, default=None)
    ap.add_argument('--to-lesson', type=int, default=None)
    ap.add_argument('--list-lessons', action='store_true')
    ap.add_argument(
        '--batch-size', type=int, default=DEFAULT_UPLOAD_BATCH,
        help=f'每批文件数（默认 {DEFAULT_UPLOAD_BATCH}，建议保持 1）',
    )
    ap.add_argument(
        '--force', action='store_true',
        help='忽略断点进度，全部重翻',
    )
    ap.add_argument(
        '--pick-voices', action='store_true',
        help='配音前交互选择每条视频的男/女声',
    )
    ap.add_argument(
        '--no-voice-pick', action='store_true',
        help='配音时不弹出配音员选择（仍读取已保存 _dub_voice_map.json）',
    )
    args = ap.parse_args()

    if args.ensure_ready:
        return 0 if ensure_notebooklm_ready() else 1

    if args.folder is None:
        print('[错误] 请指定文件夹')
        return 1

    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f'[错误] 不是文件夹：{folder}')
        return 1

    if args.list_lessons:
        print_lesson_panel(folder)
        return 0

    lesson_from = args.from_lesson
    lesson_to = args.to_lesson
    if lesson_from is not None and lesson_to is not None and lesson_from > lesson_to:
        lesson_from, lesson_to = lesson_to, lesson_from

    if not ensure_notebooklm_ready():
        return 1

    srts = find_bilingual_srts(folder, lesson_from=lesson_from, lesson_to=lesson_to)
    if not srts:
        print(f'[错误] 未找到双语字幕：{folder}')
        print('  [提示] 需要 bilingual，或 en_US.srt + en_US.zh-CN.srt 成对')
        return 1

    print('=' * 50)
    print('  NotebookLM 全自动翻译（实验 · 分批/分段/断点续跑）')
    print(f'  文件夹：{folder}')
    if lesson_from is not None or lesson_to is not None:
        lo = lesson_from if lesson_from is not None else '—'
        hi = lesson_to if lesson_to is not None else '—'
        print(f'  课序号：{lo} ~ {hi}')
    print(f'  字幕：{len(srts)} 个（一次提交，脚本自动拆分，无需手工分段）')
    print('=' * 50)

    batch_size = max(1, min(args.batch_size, MAX_UPLOAD_BATCH))

    try:
        _combined, ok_n, fail_n = asyncio.run(
            run_notebooklm_translate(
                folder, srts, batch_size=batch_size, force=args.force,
            ),
        )
    except Exception as e:
        print(f'[失败] {e}')
        print('  见 https://github.com/teng-lin/notebooklm-py/issues')
        return 1

    if ok_n == 0:
        return 1
    if fail_n:
        print(f'\n  [部分失败] {fail_n} 个未成功；重跑同一命令将跳过已完成（断点续跑）')

    if not args.no_dub:
        print('\n  [配音] 启动 quick_bilingual_dub…')
        if args.pick_voices:
            from dub_voice_prefs import interactive_pick_voices
            if interactive_pick_voices(
                folder, lesson_from=lesson_from, lesson_to=lesson_to,
            ) is None:
                return 1
        return run_dub(
            folder,
            lesson_from=lesson_from,
            lesson_to=lesson_to,
        )
    return 0 if fail_n == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
