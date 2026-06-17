#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
递归合并视频 + 配音 → 原名_C.mp4（仅中文配音轨，不含原声）

用法：
    py -3.12 merge_dub_videos.py --pick-folder
    py -3.12 merge_dub_videos.py 课程根目录
    py -3.12 merge_dub_videos.py 课程根目录 --dry-run

依赖：Python 3.10+（仅标准库）+ ffmpeg/ffprobe（需在 PATH）

规则：
    - 遍历根目录及所有子文件夹
    - 去掉 _C、1～2 位下载序号（6- / 6标题）后，再忽略开头 5 个字符，然后比较剩余部分
      （前 4 个词一致、或较短名为较长名前缀，也可配对）
    - 配音可能是 .mp3 / .wav / .m4a，也可能是 .mp4 / .webm（录制音轨，可无画面）
    - 合并时以视频时长 + 0.2 秒为上限：裁掉超出部分，短于视频则末尾补静音
    - 输出：原视频主名 + _C.mp4，保存在同一文件夹
    - 已存在且比源文件新则跳过
"""

from __future__ import annotations

MATCH_TOKEN_COUNT = 4
MIN_CJK_SUBSTR = 10
MIN_PREFIX_LEN = 12  # 录制配音短文件名前缀匹配（英文等）
COMPARE_HEAD_SKIP = 5  # 归一化后再忽略开头若干字符，抵消下载序号/括号等差异
AUDIO_TAIL_PAD_SEC = 0.2  # 视频结束后多留 0.2s，避免最后一句话被截断

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DUB_TAG = '_C'
# 下载序号：1～2 位数字，后接「-」或直接接非数字标题（如 2-标题、2标题）
_NUMERIC_PREFIX_RE = re.compile(r'^\d{1,2}(?:-\s*|(?=\D))')
VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v'}
AUDIO_EXTS = {'.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg'}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS

# 扫描时跳过的系统/回收站目录（Windows exFAT/NTFS 常见）
SKIP_DIR_NAMES = frozenset({
    '$recycle.bin',
    'recycler',
    'system volume information',
    '$winreagent',
    'recovery',
    'config.msi',
    'msocache',
    '.trash',
    '.trashes',
    '@eadir',
    'found.000',
    'found.001',
})


def is_skipped_dir_name(name: str) -> bool:
    lower = name.lower().strip()
    if lower.startswith('.'):
        return True
    return lower in SKIP_DIR_NAMES


def is_recycle_or_system_path(path: Path) -> bool:
    lowered = {part.lower() for part in path.parts}
    return bool(lowered & {'$recycle.bin', 'recycler', 'system volume information'})


def filter_walk_dirnames(dirnames: list[str]) -> None:
    """原地过滤 os.walk 的 dirnames，避免进入回收站等目录。"""
    dirnames[:] = [d for d in dirnames if not is_skipped_dir_name(d)]

_scan_warnings: list[str] = []


def win_long_path(path: Path) -> str:
    """Windows 超长路径（>260 字符）使用 \\\\?\\ 前缀。"""
    if os.name != 'nt':
        return str(path)
    try:
        s = str(path.resolve())
    except OSError:
        s = str(path)
    if s.startswith('\\\\?\\'):
        return s
    # 长路径或长文件名时启用扩展路径
    if len(s) >= 240 or len(path.name) >= 180:
        if s.startswith('\\\\'):
            return '\\\\?\\UNC\\' + s.lstrip('\\')
        return '\\\\?\\' + s
    return s


def reset_scan_warnings() -> None:
    _scan_warnings.clear()


def note_scan_warning(path: Path | str, err: BaseException) -> None:
    target = str(path)
    # 已生成的 _C 输出在复查时找不到，多为路径过长或文件已移动，不必刷屏
    if isinstance(path, Path) and path.stem.endswith(DUB_TAG):
        winerr = getattr(err, 'winerror', None)
        if winerr in (2, 3):
            return
    msg = f'{target}（{err}）'
    if msg not in _scan_warnings:
        _scan_warnings.append(msg)


def safe_is_file(path: Path) -> bool:
    try:
        return os.path.isfile(win_long_path(path))
    except OSError as err:
        note_scan_warning(path, err)
        return False


def safe_is_dir(path: Path) -> bool:
    try:
        return os.path.isdir(win_long_path(path))
    except OSError as err:
        note_scan_warning(path, err)
        return False


def safe_stat(path: Path) -> os.stat_result | None:
    try:
        return os.stat(win_long_path(path))
    except OSError as err:
        note_scan_warning(path, err)
        return None


def safe_iterdir(directory: Path) -> list[Path]:
    try:
        with os.scandir(win_long_path(directory)) as it:
            return sorted(Path(entry.path) for entry in it)
    except OSError as err:
        note_scan_warning(directory, err)
        return []


def walk_tree(root: Path) -> list[Path]:
    """递归列出子目录；无法访问的路径会跳过并记录。"""
    return list(iter_walk_tree(root))


def iter_walk_tree(
    root: Path,
    on_dir: Callable[[Path], None] | None = None,
) -> Iterator[Path]:
    def _onerror(err: OSError) -> None:
        note_scan_warning(getattr(err, 'filename', None) or root, err)

    if not safe_is_dir(root):
        note_scan_warning(root, OSError('根目录不可访问'))
        return
    walk_root = win_long_path(root) if os.name == 'nt' else root
    for dirpath, dirnames, _filenames in os.walk(walk_root, onerror=_onerror):
        filter_walk_dirnames(dirnames)
        directory = Path(dirpath)
        if is_recycle_or_system_path(directory):
            continue
        if on_dir is not None:
            on_dir(directory)
        yield directory


def explain_access_error(err: BaseException) -> str:
    winerr = getattr(err, 'winerror', None)
    if winerr == 433 or '433' in str(err):
        return (
            '磁盘/设备不可用（WinError 433）。'
            '常见于移动硬盘 U 盘 E: 断开、休眠或路径失效。'
            '请重新连接磁盘，或将整个文件夹复制到 C:\\video\\ 等本地目录后再试。'
        )
    return str(err)


def print_scan_warnings() -> None:
    if not _scan_warnings:
        return
    print(f'\n【无法访问的路径】共 {len(_scan_warnings)} 个')
    log_report(f'\n【无法访问的路径】共 {len(_scan_warnings)} 个')
    for msg in _scan_warnings[:25]:
        print(f'  • {msg}')
        log_report(f'  • {msg}')
    if len(_scan_warnings) > 25:
        extra = f'  … 另有 {len(_scan_warnings) - 25} 条'
        print(extra)
        log_report(extra)
    hint = '  提示：若为 E: / U 盘，请确认已连接且资源管理器能正常打开该文件夹。'
    print(hint)
    log_report(hint)


SHUTDOWN_DELAY_SEC = 60
_STATE_DIR_NAME = 'merge_dub_tool'
_RUNNING_LOCK = 'running.json'
_SHUTDOWN_REQUEST = 'shutdown.request'


def _detect_workspace_root() -> Path | None:
    """向上查找含 components 或 测试本地配音.bat 的项目根目录。"""
    root = Path(__file__).resolve().parent
    for _ in range(5):
        if (root / 'components').is_dir() or (root / '测试本地配音.bat').exists():
            return root
        if root.parent == root:
            break
        root = root.parent
    return None


def merge_state_dir() -> Path:
    """
    状态与运行报告目录。
    优先：环境变量 YOUTUBE_DUB_LOG_DIR → 项目 logs/merge_dub_tool → %LOCALAPPDATA%\\merge_dub_tool
    """
    for key in ('YOUTUBE_DUB_LOG_DIR', 'MERGE_DUB_STATE_DIR'):
        env = os.environ.get(key, '').strip()
        if env:
            d = Path(env)
            d.mkdir(parents=True, exist_ok=True)
            return d
    ws = _detect_workspace_root()
    if ws is not None:
        d = ws / 'logs' / 'merge_dub_tool'
        d.mkdir(parents=True, exist_ok=True)
        return d
    base = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
    d = Path(base) / _STATE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_merge_running() -> bool:
    lock = merge_state_dir() / _RUNNING_LOCK
    if not lock.is_file():
        return False
    try:
        data = json.loads(lock.read_text(encoding='utf-8'))
        pid = int(data.get('pid') or 0)
        if not _is_process_alive(pid):
            lock.unlink(missing_ok=True)
            return False
        return True
    except (OSError, ValueError, json.JSONDecodeError):
        return lock.is_file()


def set_running_lock(root: Path) -> None:
    data = {
        'pid': os.getpid(),
        'root': str(root),
        'started': time.time(),
    }
    (merge_state_dir() / _RUNNING_LOCK).write_text(
        json.dumps(data, ensure_ascii=False), encoding='utf-8',
    )


def clear_running_lock() -> None:
    try:
        (merge_state_dir() / _RUNNING_LOCK).unlink(missing_ok=True)
    except OSError:
        pass


def shutdown_is_requested() -> bool:
    return (merge_state_dir() / _SHUTDOWN_REQUEST).is_file()


def clear_shutdown_request() -> None:
    try:
        (merge_state_dir() / _SHUTDOWN_REQUEST).unlink(missing_ok=True)
    except OSError:
        pass


def cmd_request_shutdown() -> int:
    (merge_state_dir() / _SHUTDOWN_REQUEST).write_text(
        json.dumps({'requested_at': time.time()}), encoding='utf-8',
    )
    print('=' * 50)
    if is_merge_running():
        print('✓ 已登记：当前合并任务全部完成后，约 '
              f'{SHUTDOWN_DELAY_SEC} 秒后自动关机。')
        print('  不会打断正在进行的合并窗口。')
    else:
        print('✓ 已登记：下一次「正式合并」全部完成后，约 '
              f'{SHUTDOWN_DELAY_SEC} 秒后自动关机。')
        print('  请先运行「合并配音视频.bat」并开始正式合并。')
    print('  取消请双击：取消合并后关机.bat')
    print('=' * 50)
    return 0


def cmd_cancel_shutdown() -> int:
    had_request = shutdown_is_requested()
    clear_shutdown_request()
    if os.name == 'nt':
        subprocess.run(['shutdown', '/a'], capture_output=True)
    print('=' * 50)
    if had_request:
        print('✓ 已取消「合并完成后自动关机」登记。')
    else:
        print('（当前没有待执行的关机登记）')
    if os.name == 'nt':
        print('  已尝试取消 Windows 系统中已排期的关机。')
    print('=' * 50)
    return 0


def maybe_shutdown_after_merge(
    *,
    formal_merge_ran: bool,
    dry_run: bool,
    completed_normally: bool,
) -> None:
    if dry_run or not formal_merge_ran or not completed_normally:
        return
    if not shutdown_is_requested():
        return
    clear_shutdown_request()
    print('\n' + '!' * 50)
    print('🔌 合并已全部完成，即将按您的登记自动关机')
    print(f'   {SHUTDOWN_DELAY_SEC} 秒后关机；取消请运行「取消合并后关机.bat」')
    print('   或在 CMD 执行：shutdown /a')
    print('!' * 50)
    if os.name == 'nt':
        subprocess.run([
            'shutdown', '/s', '/t', str(SHUTDOWN_DELAY_SEC),
            '/c', '视频合并已完成，系统即将关机',
        ], check=False)
    else:
        print('（当前为非 Windows 系统，请手动关机）')


_session_report: SessionReport | None = None


class SessionReport:
    """将扫描/合并统计写入本地报告，关闭窗口后仍可查阅。"""

    def __init__(self, root: Path, *, dry_run: bool) -> None:
        self.root = root
        self.dry_run = dry_run
        reports_dir = merge_state_dir() / 'reports'
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.path = reports_dir / f'merge_{stamp}.txt'
        self.latest_path = reports_dir / 'latest.txt'
        self._fh = open(self.path, 'w', encoding='utf-8', newline='\n')
        mode = '预览（不写文件）' if dry_run else '正式合并'
        started = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.line('=' * 50)
        self.line('合并视频 + 配音 运行报告')
        self.line(f'开始时间：{started}')
        self.line(f'根目录：{root}')
        self.line(f'模式：{mode}')
        self.line('=' * 50)
        self.line()

    def line(self, text: str = '') -> None:
        self._fh.write(text + '\n')
        self._fh.flush()

    def close(self, *, completed_normally: bool) -> None:
        self.line()
        self.line('=' * 50)
        if completed_normally:
            self.line(f'结束时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
            self.line('状态：正常结束')
        else:
            self.line('状态：未正常结束（可能中途关闭窗口或取消）')
        self.line('=' * 50)
        self._fh.close()
        try:
            shutil.copy2(self.path, self.latest_path)
        except OSError:
            pass


def begin_session_report(root: Path, *, dry_run: bool) -> Path:
    global _session_report
    _session_report = SessionReport(root, dry_run=dry_run)
    return _session_report.path


def log_report(text: str = '') -> None:
    if _session_report is not None:
        _session_report.line(text)


def finish_session_report(*, completed_normally: bool) -> None:
    global _session_report
    if _session_report is None:
        return
    path = _session_report.path
    latest = _session_report.latest_path
    _session_report.close(completed_normally=completed_normally)
    _session_report = None
    print(f'\n📄 运行报告已保存（关闭窗口后仍可查阅）：')
    print(f'   {path}')
    print(f'   最近一次：{latest}')


def setup_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def check_ffmpeg() -> bool:
    try:
        return subprocess.run(
            ['ffmpeg', '-version'], capture_output=True,
        ).returncode == 0
    except FileNotFoundError:
        return False


def run_ffprobe(path: Path) -> dict:
    try:
        r = subprocess.run(
            [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_streams', '-show_format', win_long_path(path),
            ],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
        )
        if r.returncode != 0:
            return {}
        return json.loads(r.stdout or '{}')
    except Exception:
        return {}


def probe_media(path: Path) -> dict:
    """返回 has_video / has_audio / duration / size。"""
    info = run_ffprobe(path)
    streams = info.get('streams') or []
    has_video = any(s.get('codec_type') == 'video' for s in streams)
    has_audio = any(s.get('codec_type') == 'audio' for s in streams)
    dur = 0.0
    try:
        dur = float((info.get('format') or {}).get('duration') or 0)
    except (TypeError, ValueError):
        pass
    try:
        st = safe_stat(path)
        size = st.st_size if st else 0
    except OSError:
        size = 0
    return {
        'has_video': has_video,
        'has_audio': has_audio,
        'duration': dur,
        'size': size,
    }


def get_video_duration(path: Path) -> float:
    """优先取视频流 duration，否则用容器 duration。"""
    info = run_ffprobe(path)
    for stream in info.get('streams') or []:
        if stream.get('codec_type') != 'video':
            continue
        try:
            dur = float(stream.get('duration') or 0)
            if dur > 0:
                return dur
        except (TypeError, ValueError):
            pass
    return probe_media(path)['duration']


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return '?'
    return f'{seconds:.1f}s'


def format_video_length(seconds: float) -> str:
    """视频内容时长：x小时x分x秒。"""
    if seconds <= 0:
        return '?'
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f'{hours}小时')
    if minutes or hours:
        parts.append(f'{minutes}分')
    parts.append(f'{secs}秒')
    return ''.join(parts)


def format_elapsed_hm(seconds: float) -> str:
    """合并耗时：秒四舍五入到分，显示 x小时x分。"""
    if seconds <= 0:
        return '0分'
    total_min = round(seconds / 60)
    hours, minutes = divmod(total_min, 60)
    if hours:
        return f'{hours}小时{minutes}分'
    return f'{minutes}分'


def render_progress_bar(pct: float, width: int = 28) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(width * pct / 100)
    bar = '█' * filled + '░' * (width - filled)
    return f'[{bar}] {pct:5.1f}%'


def _parse_ffmpeg_time(value: str) -> float:
    value = value.strip()
    if not value or value == 'N/A':
        return 0.0
    parts = value.split(':')
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
    except ValueError:
        pass
    return 0.0


def read_ffmpeg_progress(progress_path: str) -> tuple[float, bool]:
    """读取 ffmpeg -progress 文件，返回 (已输出秒数, 是否结束)。"""
    try:
        text = Path(progress_path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return 0.0, False
    out_sec = 0.0
    ended = False
    for line in text.splitlines():
        if line.startswith('out_time_ms='):
            val = line.split('=', 1)[1].strip()
            if val and val != 'N/A':
                try:
                    out_sec = int(val) / 1000.0
                except ValueError:
                    pass
        elif line.startswith('out_time='):
            val = line.split('=', 1)[1].strip()
            out_sec = _parse_ffmpeg_time(val) or out_sec
        elif line.strip() == 'progress=end':
            ended = True
    return out_sec, ended


def normalize_stem(stem: str) -> str:
    """去掉 _C 后缀、前导 1～2 位下载序号（2- / 2标题 等）、常见下载后缀。"""
    stem = stem.strip()
    if stem.endswith(DUB_TAG):
        stem = stem[:-len(DUB_TAG)]
    stem = _NUMERIC_PREFIX_RE.sub('', stem, count=1)
    stem = re.sub(r'\s*-\s*YouTube\s*$', '', stem, flags=re.IGNORECASE)
    return stem.strip()


def comparison_stem(stem: str) -> str:
    """配对比较用：归一化后跳过开头若干字符，再参与 token/前缀比对。"""
    s = normalize_stem(stem)
    if len(s) > COMPARE_HEAD_SKIP:
        return s[COMPARE_HEAD_SKIP:].strip()
    return s


def _compact_from(stem: str, stem_fn: Callable[[str], str]) -> str:
    s = stem_fn(stem)
    s = re.sub(r'[\s\u3000]+', '', s)
    s = re.sub(r'[\u200b-\u200d\ufeff\ue000-\uf8ff]', '', s)
    return s


def compact_stem(stem: str) -> str:
    """比较用：去序号/后缀后，再去掉空白与不可见/私有区字符。"""
    return _compact_from(stem, normalize_stem)


def _core_compact_from(stem: str, stem_fn: Callable[[str], str]) -> str:
    s = _compact_from(stem, stem_fn).casefold()
    s = re.sub(r'^[^\w\d]+', '', s, flags=re.UNICODE)
    return s


def core_compact(stem: str) -> str:
    """比较用：compact 后去前导 emoji/符号，英文转小写。"""
    return _core_compact_from(stem, normalize_stem)


def _tokenize_from(stem: str, stem_fn: Callable[[str], str]) -> list[str]:
    parts = re.split(r'[\s【】！。、!?…,.;:：；\-–—]+', stem_fn(stem))
    return [p for p in parts if p]


def tokenize_stem(stem: str) -> list[str]:
    """按空白与常见标点切分为词/片段（已去序号前缀）。"""
    return _tokenize_from(stem, normalize_stem)


def _shared_match_key_with(
    stem_a: str, stem_b: str, stem_fn: Callable[[str], str],
) -> str | None:
    na, nb = stem_fn(stem_a), stem_fn(stem_b)
    ca, cb = _compact_from(stem_a, stem_fn), _compact_from(stem_b, stem_fn)
    if not ca or not cb:
        return None
    if na == nb or ca == cb:
        return (ca or na)[:48]

    wa, wb = _tokenize_from(stem_a, stem_fn), _tokenize_from(stem_b, stem_fn)
    n = MATCH_TOKEN_COUNT
    if len(wa) >= n and len(wb) >= n and wa[:n] == wb[:n]:
        return ' '.join(wa[:n])[:48]

    ka, kb = _core_compact_from(stem_a, stem_fn), _core_compact_from(stem_b, stem_fn)
    if ka and kb:
        if ka == kb:
            return ka[:48]
        shorter, longer = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
        if len(shorter) >= MIN_PREFIX_LEN and longer.startswith(shorter):
            return shorter[:48]

    shorter, longer = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    if len(shorter) >= MIN_CJK_SUBSTR and longer.startswith(shorter):
        return shorter[:48]
    return None


def shared_match_key(stem_a: str, stem_b: str) -> str | None:
    """
    文件名配对（与时长/体积无关）：
    - 先去下载序号，再忽略开头 5 字后比较；若不匹配则回退为不跳字比较
    - 整段相同、前 4 token 相同、或较短名为较长名前缀均可配对
    """
    key = _shared_match_key_with(stem_a, stem_b, comparison_stem)
    if key:
        return key
    return _shared_match_key_with(stem_a, stem_b, normalize_stem)


def match_label(stem_a: str, stem_b: str) -> str:
    key = shared_match_key(stem_a, stem_b)
    if key:
        return key[:40] + ('…' if len(key) > 40 else '')
    return '?'


def is_output_file(path: Path) -> bool:
    return path.stem.endswith(DUB_TAG)


def classify_file(path: Path) -> str | None:
    """
    判定媒体类型：video / audio / skip。
    .mp4 / .webm 等容器按 ffprobe 流信息判断（录制配音常为无画面 mp4/webm）。
    """
    if not safe_is_file(path):
        return None
    ext = path.suffix.lower()
    if ext not in MEDIA_EXTS:
        return None
    if is_output_file(path):
        return None

    meta = probe_media(path)
    if ext in AUDIO_EXTS:
        return 'audio' if meta['has_audio'] or meta['size'] > 0 else None

    if meta['has_video']:
        return 'video'
    if meta['has_audio'] and not meta['has_video']:
        return 'audio'

    # 无流信息时按扩展名粗判
    if ext in VIDEO_EXTS:
        return 'video'
    return None


def has_numeric_prefix(stem: str) -> bool:
    return bool(_NUMERIC_PREFIX_RE.match(stem.strip()))


def pair_role(a: Path, b: Path) -> tuple[Path, Path] | None:
    """
    判定 (视频, 配音)。不使用文件体积或时长。
    录制配音常为 mp4 且带画面流；文件名更短或带 1～2 位序号前缀的多为原视频。
    """
    ma, mb = probe_media(a), probe_media(b)

    if not ma['has_audio'] and not mb['has_audio']:
        return None
    if not ma['has_video'] and not mb['has_video']:
        return None

    if ma['has_video'] and not mb['has_video'] and mb['has_audio']:
        return a, b
    if mb['has_video'] and not ma['has_video'] and ma['has_audio']:
        return b, a

    if ma['has_video'] and mb['has_video']:
        if has_numeric_prefix(a.stem) and not has_numeric_prefix(b.stem):
            return a, b
        if has_numeric_prefix(b.stem) and not has_numeric_prefix(a.stem):
            return b, a
        la, lb = len(compact_stem(a.stem)), len(compact_stem(b.stem))
        if la != lb:
            return (b, a) if la < lb else (a, b)
        return a, b

    if ma['has_video'] and mb['has_audio']:
        return a, b
    if mb['has_video'] and ma['has_audio']:
        return b, a
    return None


def refine_video_audio_pairs(
    videos: list[Path], audios: list[Path],
) -> list[tuple[Path, Path, str]]:
    """按 shared_match_key 配对（兼容旧调用）。"""
    candidates = list(dict.fromkeys(videos + audios))
    return find_pairs_from_candidates(candidates)


def find_pairs_from_candidates(
    candidates: list[Path],
) -> list[tuple[Path, Path, str]]:
    possible: list[tuple[Path, Path, str, int]] = []
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            key = shared_match_key(a.stem, b.stem)
            if not key:
                continue
            roles = pair_role(a, b)
            if not roles:
                continue
            video, audio = roles
            possible.append((video, audio, key, len(key)))

    possible.sort(key=lambda x: x[3], reverse=True)
    used: set[Path] = set()
    pairs: list[tuple[Path, Path, str]] = []
    for video, audio, key, _ in possible:
        if video in used or audio in used:
            continue
        used.add(video)
        used.add(audio)
        pairs.append((video, audio, key))
    return pairs


def find_pairs_in_dir(
    directory: Path,
    on_media: Callable[[], None] | None = None,
) -> list[tuple[Path, Path, str]]:
    if is_recycle_or_system_path(directory):
        return []
    candidates: list[Path] = []
    for p in safe_iterdir(directory):
        if not safe_is_file(p) or is_output_file(p):
            continue
        if p.suffix.lower() not in MEDIA_EXTS:
            continue
        if on_media is not None:
            on_media()
        meta = probe_media(p)
        if not meta['has_video'] and not meta['has_audio']:
            continue
        candidates.append(p)
    return find_pairs_from_candidates(candidates)


def list_mp4_videos_in_dir(directory: Path) -> list[Path]:
    """目录内带视频流的 .mp4（不含已生成的 *_C.mp4）。"""
    if is_recycle_or_system_path(directory):
        return []
    videos: list[Path] = []
    for p in safe_iterdir(directory):
        if not safe_is_file(p) or is_output_file(p):
            continue
        if p.suffix.lower() != '.mp4':
            continue
        if probe_media(p)['has_video']:
            videos.append(p)
    return videos


def collect_unpaired_from_scan(scan: WorkspaceScan) -> list[tuple[Path, Path]]:
    """基于已完成的扫描结果统计未配对视频（避免再次遍历磁盘）。"""
    paired_videos = {job.video for job in scan.jobs}
    unpaired: list[tuple[Path, Path]] = []
    for directory in scan.directories:
        for video in list_mp4_videos_in_dir(directory):
            if video in paired_videos:
                continue
            if is_valid_output(output_path_for(video)):
                continue
            unpaired.append((directory, video))
    return unpaired


def print_subfolder_plan(scan: WorkspaceScan) -> None:
    dirs_with_pairs = sorted(
        {job.directory for job in scan.jobs},
        key=lambda p: str(p).lower(),
    )
    if not dirs_with_pairs:
        return
    print(f'\n将处理 {len(dirs_with_pairs)} 个子文件夹：')
    log_report(f'【子文件夹计划】共 {len(dirs_with_pairs)} 个')
    for directory in dirs_with_pairs:
        count = len(scan.pairs_by_dir.get(directory) or [])
        print(f'  • {directory}（{count} 对）')
        log_report(f'  • {directory}（{count} 对）')


def print_path_grouped_items(
    title: str,
    items: list[tuple[Path, ...]],
    *,
    empty_hint: str,
    line_formatter: Callable[[tuple[Path, ...]], str],
) -> None:
    print(f'\n{title}共 {len(items)} 个')
    log_report(f'\n{title}共 {len(items)} 个')
    if not items:
        print(f'  {empty_hint}')
        log_report(f'  {empty_hint}')
        return
    current_dir: Path | None = None
    for row in items:
        directory = row[0]
        if directory != current_dir:
            current_dir = directory
            print(f'  📁 {directory}')
            log_report(f'  📁 {directory}')
        line = line_formatter(row)
        print(f'     {line}')
        log_report(f'     {line}')


def is_valid_output(path: Path) -> bool:
    st = safe_stat(path)
    return st is not None and st.st_size > 1000


def directory_writable(directory: Path) -> bool:
    """检测目录是否可写（ffmpeg 需要在此创建输出文件）。"""
    probe = directory / '.merge_dub_write_probe'
    try:
        with open(win_long_path(probe), 'w', encoding='utf-8') as f:
            f.write('ok')
        os.unlink(win_long_path(probe))
        return True
    except OSError:
        return False


def explain_ffmpeg_error(err: str) -> str:
    text = (err or '').strip()
    if text.startswith('源文件不完整') or text.startswith('无法写入输出目录'):
        return text[:800]
    lower = text.lower()
    if 'permission denied' in lower:
        return (
            '无法写入输出目录（Permission denied）。'
            '该文件夹可能为只读、无写入权限，或被安全软件拦截。'
            '请右键文件夹→属性→取消「只读」，或换到可写目录（如 C:\\video\\）。'
            f' 详情：{text[:350]}'
        )
    if 'partial file' in lower:
        return (
            '源文件不完整或已损坏（partial file），常见于下载中断。'
            '请重新下载对应的视频/配音后再合并。'
            f' 详情：{text[:350]}'
        )
    return text[:800]


def output_path_for(video: Path) -> Path:
    return video.parent / f'{video.stem}{DUB_TAG}.mp4'


def should_skip(video: Path, audio: Path, output: Path) -> bool:
    if not is_valid_output(output):
        return False
    out_st = safe_stat(output)
    v_st = safe_stat(video)
    a_st = safe_stat(audio)
    if not out_st or not v_st or not a_st:
        return False
    return out_st.st_mtime >= v_st.st_mtime and out_st.st_mtime >= a_st.st_mtime


def remove_invalid_output(output: Path) -> str | None:
    """删除无效的旧输出（如 0 字节失败残留）。无法删除则返回错误说明。"""
    if not safe_is_file(output) or is_valid_output(output):
        return None
    try:
        os.unlink(win_long_path(output))
        return None
    except OSError as exc:
        return f'无法删除无效输出文件 {output.name}：{exc}'


class InitProgress:
    """扫描/初始化阶段的控制台进度提示。"""

    _SPIN = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

    def __init__(self, phase: str = '正在初始化') -> None:
        self.phase = phase
        self.start = 0.0
        self.dir_count = 0
        self.media_count = 0
        self._spin_i = 0
        self._last_draw = 0.0
        self._active = False

    def start_phase(self, extra: str = '') -> None:
        self._active = True
        self.start = time.perf_counter()
        self.dir_count = 0
        self.media_count = 0
        print(f'\n{self.phase}{extra}')
        print('  正在扫描文件夹并分析媒体（ffprobe），请稍候…')
        print('  大目录/整盘扫描可能需要 1～5 分钟，属正常现象。')
        self._draw()

    def on_directory(self, directory: Path) -> None:
        self.dir_count += 1
        self._maybe_draw(str(directory))

    def on_media_file(self) -> None:
        self.media_count += 1
        self._maybe_draw()

    def _maybe_draw(self, current: str = '') -> None:
        if not self._active:
            return
        now = time.perf_counter()
        if now - self._last_draw < 0.15:
            return
        self._last_draw = now
        self._draw(current)

    def _draw(self, current: str = '') -> None:
        elapsed = int(time.perf_counter() - self.start)
        spin = self._SPIN[self._spin_i % len(self._SPIN)]
        self._spin_i += 1
        pseudo_pct = min(95.0, 8.0 + self.dir_count * 0.4 + self.media_count * 0.25)
        bar = render_progress_bar(pseudo_pct)
        tail = f'  当前 …{current[-32:]}' if current else ''
        line = (
            f'\r  {spin} {bar}  '
            f'文件夹 {self.dir_count}  媒体 {self.media_count}  '
            f'{elapsed}s{tail}   '
        )
        print(line, end='', flush=True)

    def finish(self, summary: str) -> None:
        self._active = False
        elapsed = int(time.perf_counter() - self.start)
        print(
            f'\r  ✓ {summary}（{self.dir_count} 文件夹，'
            f'{self.media_count} 媒体，{elapsed} 秒）'
            + ' ' * 20,
        )


def root_selection_hint(root: Path) -> str:
    text = str(root)
    if re.match(r'^[A-Za-z]:\\?$', text) or text in ('\\', '/'):
        return (
            'ℹ️  您选择了磁盘根目录（如 D:\\ 或 E:\\）。'
            '其下所有课程子文件夹会一次性扫描（已自动跳过回收站 $RECYCLE.BIN）。'
            '扫描可能较慢，请耐心等待进度条。'
        )
    return ''


@dataclass
class MergeJob:
    directory: Path
    video: Path
    audio: Path
    match_key: str
    output: Path

    @property
    def pending(self) -> bool:
        return not should_skip(self.video, self.audio, self.output)


@dataclass
class WorkspaceScan:
    directories: list[Path]
    pairs_by_dir: dict[Path, list[tuple[Path, Path, str]]]
    jobs: list[MergeJob]


def scan_workspace(root: Path, progress: InitProgress) -> WorkspaceScan:
    """单次扫描：列出目录、配对、统计待合并任务。"""
    directories: list[Path] = []
    pairs_by_dir: dict[Path, list[tuple[Path, Path, str]]] = {}
    jobs: list[MergeJob] = []

    for directory in iter_walk_tree(root, on_dir=progress.on_directory):
        directories.append(directory)
        pairs = find_pairs_in_dir(directory, on_media=progress.on_media_file)
        pairs_by_dir[directory] = pairs
        for video, audio, key in pairs:
            jobs.append(MergeJob(
                directory=directory,
                video=video,
                audio=audio,
                match_key=key,
                output=output_path_for(video),
            ))
    return WorkspaceScan(directories, pairs_by_dir, jobs)


def _start_stderr_drain(proc: subprocess.Popen) -> tuple[threading.Thread, list[str]]:
    """后台读尽 stderr，避免 ffmpeg 写满管道后卡死（Windows 常见）。"""
    chunks: list[str] = []

    def _read() -> None:
        if not proc.stderr:
            return
        try:
            chunks.append(proc.stderr.read())
        except Exception:
            pass

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    return thread, chunks


def merge_video_audio(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    dub_vol: float = 2.0,
    tail_pad: float = AUDIO_TAIL_PAD_SEC,
    on_progress: Callable[[float, float], None] | None = None,
) -> tuple[bool, float, float, str]:
    """
    合并视频与配音。
    返回 (是否成功, 视频时长秒, 合并耗时秒, 失败原因)。
    """
    video_dur = get_video_duration(video_path)
    use_timed_merge = video_dur > 0
    target_dur = video_dur + tail_pad if use_timed_merge else 0.0
    target_s = f'{target_dur:.3f}'

    if use_timed_merge:
        audio_filter = (
            f'[1:a]aformat=sample_rates=48000:channel_layouts=stereo,'
            f'atrim=0:{target_s},asetpts=PTS-STARTPTS,'
            f'apad=whole_dur={target_s},'
            f'volume={dub_vol:.2f},alimiter=limit=0.95[aout]'
        )
    else:
        audio_filter = (
            f'[1:a]aformat=sample_rates=48000:channel_layouts=stereo,'
            f'volume={dub_vol:.2f},alimiter=limit=0.95[aout]'
        )

    progress_file = tempfile.NamedTemporaryFile(
        mode='w', suffix='.ffprogress', delete=False, encoding='utf-8',
    )
    progress_path = progress_file.name
    progress_file.close()

    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-i', win_long_path(video_path),
        '-i', win_long_path(audio_path),
        '-filter_complex', audio_filter,
        '-map', '0:v',
        '-map', '[aout]',
        '-c:v', 'copy',
        '-c:a', 'aac',
        '-b:a', '192k',
        '-progress', progress_path,
        '-nostats',
    ]
    if use_timed_merge:
        cmd.extend(['-t', target_s])
    else:
        cmd.append('-shortest')
    cmd.append(win_long_path(output_path))

    start = time.perf_counter()
    proc: subprocess.Popen | None = None
    stderr_thread: threading.Thread | None = None
    stderr_chunks: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        stderr_thread, stderr_chunks = _start_stderr_drain(proc)
        last_pct = -1.0
        last_pct_at = start
        stall_warned = False
        while proc.poll() is None:
            out_sec, ended = read_ffmpeg_progress(progress_path)
            elapsed = time.perf_counter() - start
            if use_timed_merge and target_dur > 0:
                pct = min(100.0, out_sec / target_dur * 100.0)
            elif ended:
                pct = 100.0
            else:
                pct = min(95.0, elapsed * 8.0) if video_dur <= 0 else min(
                    99.0, out_sec / max(video_dur, 1.0) * 100.0,
                )
            if on_progress:
                if abs(pct - last_pct) >= 0.3 or elapsed - last_pct_at >= 2.0:
                    on_progress(pct, elapsed)
                    if abs(pct - last_pct) >= 0.3:
                        last_pct = pct
                        last_pct_at = elapsed
                elif (
                    not stall_warned
                    and elapsed > 45
                    and elapsed - last_pct_at > 40
                ):
                    stall_warned = True
                    on_progress(pct, elapsed)
            time.sleep(0.2)

        if stderr_thread is not None:
            stderr_thread.join(timeout=30)
        stderr = ''.join(stderr_chunks).strip()
        elapsed = time.perf_counter() - start
        if on_progress:
            on_progress(100.0, elapsed)

        if proc.returncode != 0:
            err = (stderr or 'ffmpeg 未知错误').strip()[-800:]
            return False, video_dur, elapsed, explain_ffmpeg_error(err)

        ok = is_valid_output(output_path)
        if not ok:
            return False, video_dur, elapsed, '输出文件无效或体积过小'
        return True, video_dur, elapsed, ''
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        try:
            os.unlink(progress_path)
        except OSError:
            pass


def process_directory(
    directory: Path,
    *,
    pairs: list[tuple[Path, Path, str]] | None = None,
    dry_run: bool = False,
    dub_vol: float = 2.0,
    merge_stats: dict | None = None,
    job_counter: list[int] | None = None,
    job_total: int = 0,
    failures: list[tuple[Path, Path, Path, str]] | None = None,
) -> tuple[int, int, int]:
    if pairs is None:
        pairs = find_pairs_in_dir(directory)
    if not pairs:
        return 0, 0, 0

    ok = skip = fail = 0
    print(f'\n📁 {directory}')
    log_report(f'\n📁 {directory}')
    for video, audio, match_key in pairs:
        out = output_path_for(video)
        tag = match_key[:40] + ('…' if len(match_key) > 40 else '')
        print(f'  匹配 [{tag}]')
        print(f'    视频：{video.name}')
        print(f'    配音：{audio.name}')
        print(f'    输出：{out.name}')
        log_report(f'  匹配 [{tag}]')
        log_report(f'    视频：{video.name}')
        log_report(f'    配音：{audio.name}')
        log_report(f'    输出：{out.name}')

        if should_skip(video, audio, out):
            print('    [跳过] 输出已存在且较新')
            log_report('    [跳过] 输出已存在且较新')
            skip += 1
            continue

        if dry_run:
            v_dur = get_video_duration(video)
            a_dur = probe_media(audio)['duration']
            out_dur = v_dur + AUDIO_TAIL_PAD_SEC if v_dur > 0 else 0
            if v_dur > 0 and a_dur > 0:
                note = (
                    f'视频 {format_duration(v_dur)}  配音 {format_duration(a_dur)}'
                    f' → 输出约 {format_duration(out_dur)}'
                )
                if a_dur > out_dur:
                    note += '（裁切多余配音）'
                elif a_dur < v_dur:
                    note += '（配音较短，末尾补静音）'
                print(f'    {note}')
                log_report(f'    {note}')
            print('    [预览] 将合并')
            log_report('    [预览] 将合并')
            ok += 1
            continue

        if job_counter is not None:
            job_counter[0] += 1
            job_idx = job_counter[0]
        else:
            job_idx = 0

        cleanup_err = remove_invalid_output(out)
        if cleanup_err:
            print(f'    [失败] {cleanup_err}')
            log_report(f'    [失败] {cleanup_err}')
            fail += 1
            if failures is not None:
                failures.append((directory, video, audio, cleanup_err))
            continue

        if not directory_writable(out.parent):
            err_msg = explain_ffmpeg_error('Permission denied')
            print(f'    [失败] {err_msg}')
            log_report(f'    [失败] {err_msg}')
            fail += 1
            if failures is not None:
                failures.append((directory, video, audio, err_msg))
            continue

        print('    合并中（仅配音轨，不含原声）')
        a_meta = probe_media(audio)
        if a_meta.get('has_video') and audio.suffix.lower() in VIDEO_EXTS:
            print('    [提示] 配音为带画面的 .mp4，合并会比纯 .mp3 慢；'
                  '若有 _C.mp3 建议优先用音频文件')

        def on_progress(
            pct: float, elapsed: float, *, _idx: int = job_idx,
        ) -> None:
            prefix = f'    [{_idx}/{job_total}] ' if job_total > 0 else '    '
            line = (
                prefix + render_progress_bar(pct)
                + f'  已用 {format_elapsed_hm(elapsed)}'
            )
            print('\r' + line + ' ' * 4, end='', flush=True)

        success, v_dur, merge_sec, err_msg = merge_video_audio(
            video, audio, out, dub_vol=dub_vol, on_progress=on_progress,
        )
        print()
        if success:
            mb = out.stat().st_size / 1024 / 1024
            print(f'    [OK] {out.name}（{mb:.1f} MB）')
            print(f'    视频时长：{format_video_length(v_dur)}')
            print(f'    合并时长：{format_elapsed_hm(merge_sec)}')
            log_report(f'    [OK] {out.name}（{mb:.1f} MB）')
            log_report(f'    视频时长：{format_video_length(v_dur)}')
            log_report(f'    合并时长：{format_elapsed_hm(merge_sec)}')
            ok += 1
            if merge_stats is not None:
                merge_stats['video_sec'] += v_dur
                merge_stats['merge_sec'] += merge_sec
        else:
            reason = explain_ffmpeg_error(err_msg)
            print(f'    [失败] {reason}')
            log_report(f'    [失败] {reason}')
            fail += 1
            if failures is not None:
                failures.append((directory, video, audio, reason))

    return ok, skip, fail


def pick_folder_gui(
    title: str = '选择父文件夹（如 E:\\，会处理其下所有子文件夹）',
) -> Path | None:
    """Windows/macOS 图形选文件夹（tkinter，Python 自带）。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        print('❌ 无法打开文件夹选择窗口（缺少 tkinter）')
        print('   请改用命令行：merge_dub_videos.py "你的目录路径"')
        return None
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes('-topmost', True)
    except Exception:
        pass
    root.update()
    chosen = filedialog.askdirectory(title=title, mustexist=True)
    root.destroy()
    if not chosen:
        return None
    return Path(chosen).resolve()


def resolve_root(args: argparse.Namespace) -> Path | None:
    if args.root and args.root != '.':
        return Path(args.root).resolve()
    if args.pick_folder:
        return pick_folder_gui()
    return None


def find_non_writable_merge_dirs(root: Path) -> list[Path]:
    """待合并任务所在、但当前不可写的目录。"""
    need_write: set[Path] = set()
    for directory in walk_tree(root):
        for video, audio, _ in find_pairs_in_dir(directory):
            out = output_path_for(video)
            if should_skip(video, audio, out):
                continue
            need_write.add(out.parent)
    return sorted(d for d in need_write if not directory_writable(d))


def print_writable_dir_help(bad_dirs: list[Path]) -> None:
    print('\n' + '!' * 50)
    print('⚠️  以下目录无法写入，合并会失败（Permission denied）：')
    for d in bad_dirs:
        print(f'  • {d}')
    print('\n常见原因：')
    print('  • 文件夹或磁盘被设为「只读」')
    print('  • 从只读介质/网络盘打开（部分云盘同步文件夹也会拦截）')
    print('  • 安全软件阻止 ffmpeg 写入')
    print('\n建议操作：')
    print('  1. 右键该文件夹 → 属性 → 取消「只读」→ 应用到子文件夹')
    print('  2. 把整个课程文件夹复制到本地可写位置，例如 C:\\video\\')
    print('  3. 关闭正在播放/占用这些 mp4 的播放器')
    print('!' * 50)


def confirm_continue_after_preflight() -> bool:
    try:
        ans = input('\n仍要继续尝试合并吗？(y/N)：').strip().lower()
    except EOFError:
        return False
    return ans in ('y', 'yes')


def count_pending_merges(root: Path) -> int:
    total = 0
    for directory in walk_tree(root):
        for video, audio, _ in find_pairs_in_dir(directory):
            out = output_path_for(video)
            if not should_skip(video, audio, out):
                total += 1
    return total


def print_final_report(
    root: Path,
    *,
    dry_run: bool,
    merge_stats: dict,
    total_ok: int,
    failures: list[tuple[Path, Path, Path, str]],
    scan: WorkspaceScan | None = None,
) -> None:
    if not dry_run and total_ok > 0:
        vline = f'合计视频时长：{format_video_length(merge_stats["video_sec"])}'
        mline = f'合计合并时长：{format_elapsed_hm(merge_stats["merge_sec"])}'
        print(vline)
        print(mline)
        log_report(vline)
        log_report(mline)

    unpaired = collect_unpaired_from_scan(scan) if scan is not None else []
    unpaired_rows = [(directory, video) for directory, video in unpaired]

    print_path_grouped_items(
        '【有视频、未找到配对配音】',
        unpaired_rows,
        empty_hint='（无）',
        line_formatter=lambda row: f'• {row[1].name}',
    )

    print(f'\n【合并失败】共 {len(failures)} 个')
    log_report(f'\n【合并失败】共 {len(failures)} 个')
    if not failures:
        print('  （无）')
        log_report('  （无）')
    else:
        current_dir: Path | None = None
        for directory, video, audio, reason in failures:
            if directory != current_dir:
                current_dir = directory
                print(f'  📁 {directory}')
                log_report(f'  📁 {directory}')
            print(f'     • 视频：{video.name}')
            print(f'       配音：{audio.name}')
            print(f'       原因：{reason}')
            log_report(f'     • 视频：{video.name}')
            log_report(f'       配音：{audio.name}')
            log_report(f'       原因：{reason}')

    print_scan_warnings()


def walk_and_merge(
    root: Path,
    *,
    dry_run: bool = False,
    dub_vol: float = 2.0,
) -> None:
    reset_scan_warnings()
    formal_merge_ran = False
    completed_normally = False
    report_path: Path | None = None
    if not safe_is_dir(root):
        print(f'❌ 根目录不可访问：{root}')
        print(f'   {explain_access_error(OSError("device unavailable"))}')
        return

    report_path = begin_session_report(root, dry_run=dry_run)
    print(f'📄 本次报告实时写入：{report_path}')
    log_root = merge_state_dir()
    if log_root.parent.name == 'logs':
        print(f'   项目日志目录：{log_root.parent}  （见 logs/README.md）')

    try:
        hint = root_selection_hint(root)
        if hint:
            print(f'\n{hint}')
            log_report(hint)

        progress = InitProgress('正在初始化')
        progress.start_phase(' — 扫描与配对')
        scan = scan_workspace(root, progress)

        pending_jobs = [j for j in scan.jobs if j.pending]
        skip_jobs = len(scan.jobs) - len(pending_jobs)
        mode_note = '预览' if dry_run else '待合并'
        scan_summary = (
            f'扫描完成：共 {len(scan.jobs)} 对匹配，'
            f'{mode_note} {len(pending_jobs)} 个，跳过 {skip_jobs} 个'
        )
        progress.finish(scan_summary)
        log_report(f'\n【扫描】{scan_summary}')

        if not scan.jobs:
            msg = '未找到可配对的视频+配音，请检查文件名是否一致。'
            print(f'\n{msg}')
            log_report(f'\n{msg}')
            print_final_report(
                root, dry_run=dry_run, merge_stats={'video_sec': 0.0, 'merge_sec': 0.0},
                total_ok=0, failures=[], scan=scan,
            )
            completed_normally = True
            return

        print_subfolder_plan(scan)

        total_ok = total_skip = total_fail = total_dirs = 0
        merge_stats = {'video_sec': 0.0, 'merge_sec': 0.0}
        failures: list[tuple[Path, Path, Path, str]] = []
        job_total = len(pending_jobs)
        job_counter = [0]

        if not dry_run and pending_jobs:
            bad_dirs = sorted({
                j.output.parent for j in pending_jobs
                if not directory_writable(j.output.parent)
            })
            if bad_dirs:
                print_writable_dir_help(bad_dirs)
                if not confirm_continue_after_preflight():
                    msg = '已取消合并。请先修复目录写入权限后重试。'
                    print(msg)
                    log_report(msg)
                    return

        if not dry_run:
            set_running_lock(root)
            formal_merge_ran = True
            shutdown_hint = (
                '提示：合并过程中可另开窗口双击「合并完成后自动关机.bat」，'
                f'全部完成后约 {SHUTDOWN_DELAY_SEC} 秒自动关机（不打搅本窗口）。'
            )
            print(f'\n{shutdown_hint}')
            log_report(shutdown_hint)

        log_report('\n【处理明细】')
        print('\n开始处理…')
        for directory in scan.directories:
            pairs = scan.pairs_by_dir.get(directory) or []
            if not pairs:
                continue
            ok, skip, fail = process_directory(
                directory,
                pairs=pairs,
                dry_run=dry_run,
                dub_vol=dub_vol,
                merge_stats=merge_stats if not dry_run else None,
                job_counter=job_counter if not dry_run else None,
                job_total=job_total,
                failures=failures if not dry_run else None,
            )
            if ok or skip or fail:
                total_dirs += 1
                total_ok += ok
                total_skip += skip
                total_fail += fail

        summary = (
            f'处理文件夹：{total_dirs}  成功：{total_ok}  跳过：{total_skip}  失败：{total_fail}'
        )
        print('\n' + '=' * 50)
        print(summary)
        log_report('\n【汇总】')
        log_report(summary)
        if dry_run:
            note = '（预览模式，未实际写入）'
            print(note)
            log_report(note)
        print_final_report(
            root,
            dry_run=dry_run,
            merge_stats=merge_stats,
            total_ok=total_ok,
            failures=failures,
            scan=scan,
        )
        completed_normally = True
    finally:
        clear_running_lock()
        finish_session_report(completed_normally=completed_normally)
        maybe_shutdown_after_merge(
            formal_merge_ran=formal_merge_ran,
            dry_run=dry_run,
            completed_normally=completed_normally,
        )


def interactive_menu() -> None:
    setup_console_encoding()
    if not check_ffmpeg():
        print('❌ 未找到 ffmpeg，请先安装并加入 PATH')
        print('   https://ffmpeg.org/download.html')
        sys.exit(1)

    print('=' * 50)
    print('  合并视频 + 配音 → 原名_C.mp4（仅配音轨）')
    print('  配对：去下载序号后忽略开头 5 字，再比较文件名')
    print('=' * 50)
    print('[1] 浏览选文件夹 + 预览（不写文件）')
    print('[2] 浏览选文件夹 + 正式合并')
    print('[3] 手动输入路径 + 正式合并')
    print('[4] 手动输入路径 + 预览')
    print('     提示：若有 0607A/B/C 多个子文件夹，直接选父目录 E:\\ 即可一次全部处理')
    print()
    choice = input('请选择 1-4（直接回车=2）：').strip() or '2'

    dry_run = choice in ('1', '4')
    root: Path | None = None

    if choice in ('1', '2'):
        print('\n请在弹窗中选择课程根目录…')
        root = pick_folder_gui()
    elif choice in ('3', '4'):
        raw = input('请输入课程根目录（可拖文件夹到此）：').strip().strip('"')
        root = Path(raw).resolve() if raw else None
    else:
        print('❌ 无效选项')
        sys.exit(1)

    if root is None or not safe_is_dir(root):
        print('❌ 未选择或目录不存在/不可访问')
        if root is not None:
            print(f'   {explain_access_error(OSError("device unavailable"))}')
        sys.exit(1)

    print(f'\n根目录：{root}')
    if dry_run:
        print('模式：预览（--dry-run）')
    walk_and_merge(root, dry_run=dry_run)


def main() -> None:
    setup_console_encoding()
    parser = argparse.ArgumentParser(
        description='递归合并视频与配音 → 原名_C.mp4（仅配音轨）')
    parser.add_argument(
        'root',
        nargs='?',
        default='',
        help='课程根目录（留空则配合 --pick-folder）',
    )
    parser.add_argument(
        '--pick-folder',
        action='store_true',
        help='弹出文件夹选择窗口',
    )
    parser.add_argument(
        '--menu',
        action='store_true',
        help='交互菜单（推荐 Windows 双击 bat 时使用）',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='只显示匹配结果，不实际合并',
    )
    parser.add_argument(
        '--dub-vol',
        type=float,
        default=2.0,
        help='配音音量倍数（默认 2.0）',
    )
    parser.add_argument(
        '--request-shutdown',
        action='store_true',
        help='登记：正式合并全部完成后自动关机（供单独 bat 调用）',
    )
    parser.add_argument(
        '--cancel-shutdown',
        action='store_true',
        help='取消合并完成后关机（供单独 bat 调用）',
    )
    args = parser.parse_args()

    if args.request_shutdown:
        setup_console_encoding()
        sys.exit(cmd_request_shutdown())
    if args.cancel_shutdown:
        setup_console_encoding()
        sys.exit(cmd_cancel_shutdown())

    if args.menu:
        interactive_menu()
        return

    root = resolve_root(args)
    if root is None:
        print('❌ 未选择或未指定目录')
        sys.exit(1)
    if not safe_is_dir(root):
        print(f'❌ 目录不存在或不可访问：{root}')
        print(f'   {explain_access_error(OSError("device unavailable"))}')
        sys.exit(1)

    if not check_ffmpeg():
        print('❌ 未找到 ffmpeg，请先安装并加入 PATH')
        print('   https://ffmpeg.org/download.html')
        sys.exit(1)

    print(f'根目录：{root}')
    print('规则：去下载序号后忽略开头 5 字再比较 → 合并为 *_C.mp4（仅配音轨）')
    walk_and_merge(root, dry_run=args.dry_run, dub_vol=args.dub_vol)


if __name__ == '__main__':
    main()
