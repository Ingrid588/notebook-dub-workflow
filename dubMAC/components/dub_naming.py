#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配音输出命名：在原视频/字幕主名后追加 _C（ASCII，无中文）。"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

DUB_TAG = '_C'
_DUB_SUFFIX_RE = re.compile(r'_C(\d*)$')
# 启动前由用户设置的固定后缀（bat 里 set TUBEGET_DUB_TAG=_C2）；未设置则每次自动递增
_EXPLICIT_DUB_TAG: str | None = os.environ.get('TUBEGET_DUB_TAG', '').strip() or None


def active_dub_tag() -> str:
    """当前配音后缀：ensure_dub_tag 写入的环境变量，或用户指定的 TUBEGET_DUB_TAG。"""
    t = os.environ.get('TUBEGET_DUB_TAG', '').strip()
    return t if t else DUB_TAG


def dub_tag_from_stem(stem: str) -> str | None:
    """从文件名主名解析配音后缀（_C / _C2 / _C3 …），非配音则 None。"""
    m = _DUB_SUFFIX_RE.search(stem)
    if not m:
        return None
    num = m.group(1)
    return DUB_TAG if num == '' else f'_C{num}'


def version_to_dub_tag(version: int) -> str:
    if version <= 1:
        return DUB_TAG
    return f'_C{version}'


def _dub_version_from_stem(stem: str, orig_stem: str) -> int | None:
    if not stem.startswith(orig_stem):
        return None
    suffix = stem[len(orig_stem):]
    m = _DUB_SUFFIX_RE.match(suffix)
    if not m:
        return None
    num = m.group(1)
    return 1 if num == '' else int(num)


def existing_dub_versions(folder: Path, video_stem: str) -> list[int]:
    """目录内已有配音版本号：1=_C，2=_C2，3=_C3 …"""
    versions: set[int] = set()
    if not folder.is_dir():
        return []
    for p in folder.iterdir():
        v = _dub_version_from_stem(p.stem, video_stem)
        if v is not None:
            versions.add(v)
    return sorted(versions)


def next_dub_tag(folder: Path, video_stem: str) -> str:
    """在已有 _C/_C2/… 基础上取下一个后缀（保留旧成片）。"""
    versions = existing_dub_versions(folder, video_stem)
    if not versions:
        return DUB_TAG
    return version_to_dub_tag(max(versions) + 1)


def ensure_dub_tag(folder: Path, video: Path, *, quiet: bool = False) -> str:
    """
    确定本次配音后缀并写入 TUBEGET_DUB_TAG（供子进程 step1 读取）。
    未在启动前指定 TUBEGET_DUB_TAG 时自动递增；指定则固定使用该后缀。
    """
    parent = video.parent if video.is_file() else folder
    if _EXPLICIT_DUB_TAG:
        tag = _EXPLICIT_DUB_TAG
        if not quiet:
            print(f'  [命名] 使用指定后缀 {tag}')
    else:
        tag = next_dub_tag(parent, video.stem)
        existing = existing_dub_versions(parent, video.stem)
        if not quiet:
            if existing:
                prev = ', '.join(version_to_dub_tag(v) for v in existing)
                print(f'  [命名] 已有 {prev}，保留旧版，本次 → {tag}')
            else:
                print(f'  [命名] 本次输出 → {tag}')
    os.environ['TUBEGET_DUB_TAG'] = tag
    return tag


def dub_tag_is_explicit() -> bool:
    """启动前是否已指定 TUBEGET_DUB_TAG（指定时同名成片已存在则跳过）。"""
    return _EXPLICIT_DUB_TAG is not None

BILINGUAL_SUB_EXTS = frozenset({'.srt', '.txt'})
_NUMERIC_PREFIX_RE = re.compile(r'^\d{1,2}(?:-\s*|(?=\D))')
_LESSON_NUM_LEAD_RE = re.compile(r'^(\d{1,3})\s*-\s*')
# 双语字幕文件名尾（不含 .srt）；兼容 .en_US.bilingual.txt.srt / .c_US.bilingual.srt 等
_BILINGUAL_STEM_MARKERS = (
    '.en_us.bilingual.txt', '.en-us.bilingual.txt',
    '.en_us.bilingual', '.en-us.bilingual',
    '.c_us.bilingual', '.c-us.bilingual',  # 常见拼写变体
    '.sync-bilingual', '.sync-zh', '.zh-en-merge', '.en-zh-merge',
    '.bilingual.txt', '.bilingual',
    '.en_us.zh-cn', '.en-us.zh-cn',
    '.en_us.zh-hans', '.en-us.zh-hans',
)
_BILINGUAL_DIRECT_TAILS = (
    '.en_US.bilingual.txt', '.en-US.bilingual.txt',
    '.en_US.bilingual', '.en-US.bilingual',
    '.c_US.bilingual', '.c-US.bilingual',
    '.bilingual.txt', '.bilingual',
    '.sync-bilingual', '.en-zh-merge',
)


def dub_stem(original_stem: str) -> str:
    return f'{original_stem}{active_dub_tag()}'


def dub_mp3_path(video: Path) -> Path:
    return video.parent / f'{dub_stem(video.stem)}.mp3'


def dub_video_path(video: Path) -> Path:
    return video.parent / f'{dub_stem(video.stem)}{video.suffix}'


def is_dub_output(path: Path) -> bool:
    """是否为配音成片（原片同名 + _C / _C2 / _C3 … 且原片存在）。"""
    if not path.is_file():
        return False
    tag = dub_tag_from_stem(path.stem)
    if not tag:
        return False
    orig = path.parent / f'{path.stem[:-len(tag)]}{path.suffix}'
    return orig.is_file()


def _strip_dub_tag_from_stem(stem: str) -> str:
    m = _DUB_SUFFIX_RE.search(stem)
    if m:
        return stem[:m.start()]
    if stem.endswith('.quick-fit'):
        return stem[:-len('.quick-fit')]
    return stem


def is_bilingual_subtitle_file(path: Path) -> bool:
    """是否为 bilingual 字幕（含 .en_US.bilingual.txt.srt 等复合扩展）。"""
    if not path.is_file():
        return False
    name = path.name.lower()
    if 'bilingual' not in name:
        return False
    if path.suffix.lower() not in BILINGUAL_SUB_EXTS:
        return False
    return True


def is_bilingual_sub_dub_copy(path: Path) -> bool:
    """配音配套字幕（如 foo.en_US.bilingual.txt_C.srt），原版存在时应跳过。"""
    if not is_bilingual_subtitle_file(path):
        return False
    tag = dub_tag_from_stem(path.stem)
    if not tag:
        return False
    orig = path.parent / f'{path.stem[:-len(tag)]}{path.suffix}'
    return orig.is_file()


def video_stem_from_subtitle(sub_path: str | Path) -> str:
    """从字幕文件名推断原视频主名（与 *.en_US.bilingual.srt 同前缀规则）。"""
    stem = _strip_dub_tag_from_stem(Path(sub_path).stem)
    lower = stem.lower()
    for marker in _BILINGUAL_STEM_MARKERS:
        idx = lower.find(marker)
        if idx > 0:
            return stem[:idx]
    for suffix in (
        '.zh-hans', '.zh-cn', '.zh_cn', '.zh', '.chs',
        '.en_us.zh-cn', '.en-us.zh-cn', '.en_us.zh-hans', '.en-us.zh-hans',
        '.en_us', '.en', '.c_us',
    ):
        if lower.endswith(suffix):
            return stem[:-len(suffix)]
    return stem


def study_srt_path_for_subtitle(sub_path: str | Path) -> Path:
    """
    学习/配音用双语字幕：与视频主名相同，如
    1 - Course Overview.en_US.bilingual.srt → 1 - Course Overview.srt
    """
    p = Path(sub_path)
    return p.parent / f'{video_stem_from_subtitle(p)}.srt'


# NotebookLM 偶发写入的引用索引，如 [4-6]、[1]、[i]
_NLM_CITATION_RE = re.compile(
    r'[\[(（【]\s*(?:\d+\s*[-–—~至]\s*\d+|\d+|i)\s*[\])）】]',
    re.IGNORECASE,
)


def strip_notebooklm_citation_marks(text: str) -> str:
    """去掉中文行尾的 NotebookLM 引用索引（如 [4-6]）。"""
    t = (text or '').strip()
    if not t:
        return t
    prev = None
    while prev != t:
        prev = t
        t = _NLM_CITATION_RE.sub('', t).rstrip()
    return t


def clean_notebooklm_srt_body(body: str) -> tuple[str, int]:
    """清理 SRT 正文中中文行的引用索引；返回 (正文, 删除次数)。"""
    lines = body.splitlines()
    out: list[str] = []
    removed = 0
    in_cue_text = False
    text_line = 0

    for line in lines:
        stripped = line.strip()
        if '-->' in line:
            in_cue_text = True
            text_line = 0
            out.append(line)
            continue
        if not stripped:
            in_cue_text = False
            text_line = 0
            out.append(line)
            continue
        if in_cue_text:
            text_line += 1
            if text_line >= 2 or re.search(r'[\u4e00-\u9fff]', line):
                cleaned = strip_notebooklm_citation_marks(line)
                if cleaned != line.strip():
                    removed += 1
                out.append(cleaned if line == stripped else
                           line[: len(line) - len(line.lstrip())] + cleaned)
                continue
        out.append(line)

    result = '\n'.join(out)
    if body.endswith('\n') and not result.endswith('\n'):
        result += '\n'
    return result, removed


def srt_has_bilingual_content(path: Path) -> bool:
    """是否为含中文的双语 SRT（用于识别 {视频主名}.srt）。"""
    if not path.is_file():
        return False
    try:
        sample = path.read_text(encoding='utf-8-sig')[:12000]
    except OSError:
        return False
    return '-->' in sample and bool(re.search(r'[\u4e00-\u9fff]', sample))


def srt_has_dual_line_cues(path: Path) -> bool:
    """正文是否为「英文行+中文行」双语块（非纯中文轨）。"""
    from step1_subtitle_to_audio import parse_srt

    try:
        cues = parse_srt(path.read_text(encoding='utf-8-sig')[:48000])
    except OSError:
        return False
    dual = 0
    for c in cues[:40]:
        lines = [ln.strip() for ln in str(c.get('text', '')).splitlines() if ln.strip()]
        if len(lines) >= 2:
            dual += 1
    return dual >= 2


def is_zh_only_subtitle_file(path: Path) -> bool:
    """TubeGet 等下载的中文轨：*.en_US.zh-CN.srt / *.zh-Hans.srt（非 bilingual 正文）。"""
    if not path.is_file() or path.suffix.lower() != '.srt':
        return False
    low = path.name.lower()
    if 'bilingual' in low or 'en-zh-merge' in low or 'fit-dub' in low:
        return False
    return any(x in low for x in ('.zh-cn', '.zh_cn', '.zh-hans'))


def is_en_us_subtitle_file(path: Path) -> bool:
    """TubeGet 英文轨：*.en_US.srt（不含 zh-CN / bilingual）。"""
    if not path.is_file() or path.suffix.lower() != '.srt':
        return False
    low = path.name.lower()
    if 'bilingual' in low or 'en-zh-merge' in low or 'fit-dub' in low:
        return False
    if any(x in low for x in ('.zh-cn', '.zh_cn', '.zh-hans')):
        return False
    return '.en_us' in low or '.en-us' in low


def zh_cn_pair_for_en_sub(en_path: Path) -> Path | None:
    """与 en_US.srt 配对的中文轨路径。"""
    folder = en_path.parent
    stem = en_path.stem
    base = video_stem_from_subtitle(en_path)
    for tail in (
        f'{stem}.zh-CN', f'{stem}.zh-cn',
        f'{base}.en_US.zh-CN', f'{base}.en-US.zh-CN',
    ):
        p = folder / f'{tail}.srt'
        if p.is_file():
            return p
    return None


def default_bilingual_path_for_lesson(folder: Path, lesson_stem: str) -> Path:
    return folder / f'{lesson_stem}.en_US.bilingual.srt'


def merge_en_zh_to_bilingual_srt(
    en_path: Path,
    zh_path: Path,
    out_path: Path | None = None,
) -> Path:
    """将 en_US.srt + zh-CN.srt 合并为 bilingual.srt（英+中两行）。"""
    from step1_subtitle_to_audio import parse_srt

    en_cues = parse_srt(en_path.read_text(encoding='utf-8-sig'))
    zh_cues = parse_srt(zh_path.read_text(encoding='utf-8-sig'))
    n = min(len(en_cues), len(zh_cues))
    if n == 0:
        raise ValueError(f'字幕为空：{en_path.name} / {zh_path.name}')

    merged: list[dict] = []
    for i in range(n):
        en = ' '.join(
            ln.strip() for ln in str(en_cues[i].get('text', '')).splitlines() if ln.strip()
        )
        zh_raw = str(zh_cues[i].get('text', '')).strip()
        zh_lines = [ln.strip() for ln in zh_raw.splitlines() if ln.strip()]
        if len(zh_lines) >= 2 and re.search(r'[\u4e00-\u9fff]', zh_lines[-1]):
            zh = zh_lines[-1]
        else:
            zh = zh_lines[0] if zh_lines else zh_raw
        merged.append({
            'start': en_cues[i]['start'],
            'end': en_cues[i]['end'],
            'text': f'{en}\n{zh}' if en else zh,
        })

    out = out_path or default_bilingual_path_for_lesson(
        en_path.parent, video_stem_from_subtitle(en_path),
    )
    write_bilingual_srt(merged, out)
    return out


def discover_bilingual_subtitles(
    folder: Path,
    *,
    quiet: bool = False,
) -> list[Path]:
    """
    发现可用于翻译/配音的双语字幕：
    - *.bilingual.srt / en-zh-merge
    - 正文已是英+中两行
    - en_US.srt + zh-CN.srt 自动合并
    """
    if not folder.is_dir():
        return []

    seen: set[str] = set()
    out: list[Path] = []

    def add(p: Path) -> None:
        key = match_compact_stem(video_stem_from_subtitle(p))
        if not key or key in seen:
            return
        seen.add(key)
        out.append(p)

    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.suffix.lower() != '.srt':
            continue
        if is_bilingual_sub_dub_copy(f):
            continue
        low = f.name.lower()
        if is_bilingual_subtitle_file(f) or 'en-zh-merge' in low:
            add(f)
            continue
        if f.stem == video_stem_from_subtitle(f):
            if srt_has_dual_line_cues(f):
                add(f)

    for f in sorted(folder.iterdir()):
        if not is_en_us_subtitle_file(f):
            continue
        key = match_compact_stem(video_stem_from_subtitle(f))
        if not key or key in seen:
            continue
        zh = zh_cn_pair_for_en_sub(f)
        if not zh:
            continue
        bil = default_bilingual_path_for_lesson(f.parent, video_stem_from_subtitle(f))
        need_merge = (
            not bil.is_file()
            or max(f.stat().st_mtime, zh.stat().st_mtime) > bil.stat().st_mtime
        )
        if need_merge:
            merge_en_zh_to_bilingual_srt(f, zh, bil)
            if not quiet:
                print(f'  [合并双语] {f.name} + {zh.name} → {bil.name}')
        add(bil)

    return sorted(
        out,
        key=lambda p: (lesson_number_from_filename(p.name) or 9999, p.name.lower()),
    )


def write_subtitle_with_study_sync(
    sub_path: Path,
    body: str,
    *,
    clean_citations: bool = True,
) -> tuple[Path, Path | None, int]:
    """
    写回 bilingual 源文件，并同步 {视频主名}.srt。
    返回 (源路径, 学习字幕路径, 清理引用数)。
    """
    removed = 0
    if clean_citations:
        body, removed = clean_notebooklm_srt_body(body)
    sub_path.write_text(body, encoding='utf-8')
    study = study_srt_path_for_subtitle(sub_path)
    study.write_text(body, encoding='utf-8')
    return sub_path, study, removed


def lesson_number_from_filename(name: str | Path) -> int | None:
    """从文件名 leading 解析课序号，如 ``3 - Title.mp4`` → 3。"""
    m = _LESSON_NUM_LEAD_RE.match(Path(name).name)
    if not m:
        return None
    return int(m.group(1))


def filter_paths_by_lesson_range(
    paths: list[Path],
    lesson_from: int | None = None,
    lesson_to: int | None = None,
) -> list[Path]:
    """按课序号区间筛选（含端点）；无编号文件在指定区间时排除。"""
    if lesson_from is None and lesson_to is None:
        return paths
    lo = lesson_from if lesson_from is not None else 0
    hi = lesson_to if lesson_to is not None else 99999
    if lo > hi:
        lo, hi = hi, lo
    out: list[Path] = []
    for p in paths:
        n = lesson_number_from_filename(p.name)
        if n is None:
            continue
        if lo <= n <= hi:
            out.append(p)
    return sorted(
        out,
        key=lambda x: (lesson_number_from_filename(x.name) or 0, x.name.lower()),
    )


def list_lesson_numbers_in_folder(folder: Path) -> list[int]:
    """扫描文件夹内带 ``N - Title`` 编号的双语字幕/视频序号。"""
    nums: set[int] = set()
    if not folder.is_dir():
        return []
    for f in folder.iterdir():
        if not f.is_file():
            continue
        n = lesson_number_from_filename(f.name)
        if n is None:
            continue
        low = f.name.lower()
        if f.suffix.lower() in {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v'}:
            nums.add(n)
        elif f.suffix.lower() == '.srt' and (
            'bilingual' in low or 'en-zh-merge' in low
            or is_en_us_subtitle_file(f) or is_zh_only_subtitle_file(f)
        ):
            nums.add(n)
    return sorted(nums)


def match_compact_stem(stem: str) -> str:
    """配对用：去下载序号前缀、空白/下划线/连字符，英文小写。"""
    s = _strip_dub_tag_from_stem(stem.strip())
    s = _NUMERIC_PREFIX_RE.sub('', s, count=1)
    s = re.sub(r'[\s_\-.]+', '', s)
    return s.casefold()


def _iter_bilingual_subtitle_files(folder: Path) -> list[Path]:
    out: list[Path] = []
    for f in folder.iterdir():
        if not is_bilingual_subtitle_file(f):
            continue
        if is_bilingual_sub_dub_copy(f):
            continue
        out.append(f)
    return out


def prefer_fit_dub_sub(sub: Path | None) -> Path | None:
    """若存在 *.fit-dub.srt（进框翻译产物），优先于原 bilingual。"""
    if not sub:
        return None
    fit = sub.parent / f'{sub.stem}.fit-dub{sub.suffix}'
    if fit.is_file():
        return fit
    return sub


def find_bilingual_sub_for_video(
    video: Path,
    folder: Path | None = None,
) -> Path | None:
    """
    查找视频对应的 bilingual 字幕。
    支持：
      1 - Title.mp4 ↔ Title.en_US.bilingual.txt.srt
      1 - Title.mp4 ↔ Title.c_US.bilingual.srt（拼写变体）
      序号/空格/下划线差异的模糊配对。
    """
    folder = folder or video.parent
    base = video.stem

    study = folder / f'{base}.srt'
    if (
        study.is_file()
        and not is_bilingual_sub_dub_copy(study)
        and srt_has_bilingual_content(study)
    ):
        return prefer_fit_dub_sub(study)

    for tail in _BILINGUAL_DIRECT_TAILS:
        c = folder / f'{base}{tail}.srt'
        if c.is_file() and not is_bilingual_sub_dub_copy(c):
            return prefer_fit_dub_sub(c)
        for ext in BILINGUAL_SUB_EXTS:
            if ext == '.srt':
                continue
            c = folder / f'{base}{tail}{ext}'
            if c.is_file() and not is_bilingual_sub_dub_copy(c):
                return prefer_fit_dub_sub(c)

    en_c = folder / f'{base}.en_US.srt'
    if en_c.is_file():
        zh = zh_cn_pair_for_en_sub(en_c)
        if zh:
            bil = default_bilingual_path_for_lesson(folder, base)
            if not bil.is_file() or max(en_c.stat().st_mtime, zh.stat().st_mtime) > bil.stat().st_mtime:
                merge_en_zh_to_bilingual_srt(en_c, zh, bil)
            return prefer_fit_dub_sub(bil)

    video_key = match_compact_stem(base)
    if not video_key:
        return None

    scored: list[tuple[int, Path]] = []
    for f in _iter_bilingual_subtitle_files(folder):
        sub_key = match_compact_stem(video_stem_from_subtitle(f))
        if not sub_key:
            continue
        if sub_key == video_key:
            scored.append((100, f))
        elif sub_key.startswith(video_key) or video_key.startswith(sub_key):
            scored.append((80 - abs(len(sub_key) - len(video_key)), f))
        elif base.lower()[: min(24, len(base))] in f.name.lower():
            scored.append((50, f))

    if not scored:
        bilinguals = _iter_bilingual_subtitle_files(folder)
        videos = [
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in {
                '.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v',
            } and not is_dub_output(f)
        ]
        if len(bilinguals) == 1 and len(videos) == 1 and videos[0] == video:
            return prefer_fit_dub_sub(bilinguals[0])
        return None

    scored.sort(key=lambda x: (-x[0], x[1].name.lower()))
    best_score = scored[0][0]
    best = [p for s, p in scored if s == best_score]
    if len(best) == 1:
        return prefer_fit_dub_sub(best[0])
    return prefer_fit_dub_sub(min(best, key=lambda p: len(p.name)))


def dub_subtitle_path(video: Path, subtitle: Path) -> Path:
    """
    配套双语字幕：在原字幕文件名末尾加 _C（扩展名之前）。
    foo.en_US.bilingual.srt → foo.en_US.bilingual_C.srt
    """
    _ = video  # 保留参数以兼容现有调用
    return subtitle.parent / f'{subtitle.stem}{active_dub_tag()}{subtitle.suffix}'


def dub_output_mp3_path(sub_path: str | Path, voice: str = '') -> Path:
    """从字幕路径得到配音 MP3 路径：video_stem_C.mp3"""
    parent = Path(sub_path).parent
    base = video_stem_from_subtitle(sub_path)
    return parent / f'{dub_stem(base)}.mp3'


def kokoro_samples_dir_from_work(work_dir: str | Path) -> Path:
    """
    Kokoro 试听目录：与 dub-work 同级。
    例：foo.dub-work → foo.kokoro-samples/
    """
    p = Path(work_dir)
    name = p.name
    if name.endswith('.dub-work'):
        base = name[: -len('.dub-work')]
    else:
        base = name
    return p.parent / f'{base}.kokoro-samples'


def save_kokoro_sample(
    work_dir: str | Path,
    index: int,
    mp3_path: str | Path,
    text: str,
) -> Path:
    """复制 Kokoro 单句 mp3 到试听目录，并写对应文本说明。"""
    dest_dir = kokoro_samples_dir_from_work(work_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = f'cue_{index:04d}'
    dest_mp3 = dest_dir / f'{stem}.mp3'
    shutil.copy2(mp3_path, dest_mp3)
    meta = dest_dir / f'{stem}.txt'
    meta.write_text(
        f'index={index}\n'
        f'cue_no={index + 1}\n'
        f'text={text.strip()}\n',
        encoding='utf-8',
    )
    return dest_mp3


def format_srt_time(sec: float) -> str:
    """秒 → SRT 时间格式 00:00:00,000"""
    if sec < 0:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        ms = 0
        s += 1
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def write_bilingual_srt(cues: list, path: Path) -> None:
    """将 cues 写成双语 SRT（每块：英文行 + 中文行）。"""
    from step1_subtitle_to_audio import strip_continuation_mark

    blocks: list[str] = []
    n = 0
    for cue in cues:
        raw = str(cue.get('text', '')).strip()
        if not raw:
            continue
        lines = raw.splitlines()
        if len(lines) >= 2:
            text = f'{lines[0]}\n{strip_notebooklm_citation_marks(strip_continuation_mark(lines[1]))}'
        else:
            text = strip_notebooklm_citation_marks(strip_continuation_mark(raw))
        n += 1
        blocks.append(
            f'{n}\n'
            f'{format_srt_time(cue["start"])} --> {format_srt_time(cue["end"])}\n'
            f'{text}\n'
        )
    path.write_text('\n'.join(blocks), encoding='utf-8')


def sync_cues_to_dub_playback(
    cues: list,
    results: list,
    *,
    min_gap: float = 0.08,
) -> tuple[list, list]:
    """
    以中文配音实际播放为准，更新每条 cue 的 start/end。
    与 merge_audio 使用同一套防重叠 play_start 逻辑。
    返回 (同步后的 cues, placements)。
    """
    from step1_subtitle_to_audio import (
        _resolve_playback_starts,
        _speech_strict_window_enabled,
        get_audio_duration,
    )
    import os

    placements = _resolve_playback_starts(results, min_gap=min_gap)
    if not placements:
        return cues, placements

    timeline: dict[int, tuple[float, float]] = {}
    shifted = 0
    for idx, orig_s, orig_e, mp3_path, play_start in placements:
        if mp3_path and os.path.exists(mp3_path):
            dur = get_audio_duration(mp3_path)
        else:
            dur = max(0.4, orig_e - orig_s)
        if dur <= 0:
            dur = max(0.4, orig_e - orig_s)
        play_end = play_start + dur
        timeline[idx] = (play_start, play_end)
        if not _speech_strict_window_enabled() and play_start > orig_s + 0.05:
            shifted += 1

    synced: list[dict] = []
    for i, cue in enumerate(cues):
        c = dict(cue)
        if i in timeline:
            ps, pe = timeline[i]
            c['start'] = ps
            c['end'] = max(pe, ps + 0.35)
        synced.append(c)

    if shifted:
        print(f'  字幕同步：{shifted} 条已对齐到实际配音时刻（中文视角）')
    elif _speech_strict_window_enabled():
        print(f'  字幕同步：{len(timeline)} 条对齐实际配音时长（不截断）')
    return synced, placements


def _display_limits(
    max_zh: int | None = None,
    max_en: int | None = None,
) -> tuple[int, int]:
    """读取单行字数上限（可被 tubeget.config.json 覆盖）。"""
    from step1_subtitle_to_audio import (
        MAX_EN_CHARS_PER_LINE,
        MAX_ZH_CHARS_PER_LINE,
    )
    return (
        max_zh if max_zh is not None else MAX_ZH_CHARS_PER_LINE,
        max_en if max_en is not None else MAX_EN_CHARS_PER_LINE,
    )


def save_bilingual_sub_for_dub(
    video: Path,
    subtitle: Path,
    cues: list | None = None,
    *,
    gemini: bool = False,
    quiet: bool = False,
    display_max_zh: int | None = None,
    display_max_en: int | None = None,
) -> Path | None:
    """
    保存 _C 配套双语字幕，供 PotPlayer 等自动加载。

    传入 cues 时：先按句拆成「一行英文 + 一行中文」分时显示，再写入；
    否则复制原字幕。
    """
    out = dub_subtitle_path(video, subtitle)
    if cues:
        from step1_subtitle_to_audio import split_bilingual_cues_for_display
        zh_cap, en_cap = _display_limits(display_max_zh, display_max_en)
        # 已按句独立配音并同步的轴，直接写入；否则才做显示拆行
        if cues and all(
            c.get('speech_unit') for c in cues if str(c.get('text', '')).strip()
        ):
            display_cues = cues
        else:
            display_cues = split_bilingual_cues_for_display(
                cues, max_zh=zh_cap, max_en=en_cap)
        write_bilingual_srt(display_cues, out)
        if not quiet:
            if any(c.get('speech_unit') for c in cues):
                desc = '每句独立配音，字幕与中文声同步'
            elif gemini:
                desc = '一句一行（合并/优化中文 + 英文跟随）'
            else:
                desc = '一句一行（中文整句合并 + 英文跟随）'
            print(f'  配套字幕：{out.name}（{desc}）')
        return out

    if out.exists() and out.stat().st_mtime >= subtitle.stat().st_mtime:
        return out
    shutil.copy2(subtitle, out)
    if not quiet:
        print(f'  配套字幕：{out.name}（复制自 {subtitle.name}）')
    return out


def copy_bilingual_sub_for_dub(
    video: Path, subtitle: Path, *, quiet: bool = False,
) -> Path | None:
    """兼容旧调用：无 Gemini 改写时复制原字幕。"""
    return save_bilingual_sub_for_dub(
        video, subtitle, quiet=quiet, gemini=False)
