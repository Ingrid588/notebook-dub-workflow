#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量处理主控脚本（支持 tubeget.config.json）
把视频、字幕、脚本放同一文件夹后运行此脚本
"""
import os, re, sys, json, subprocess, time
from pathlib import Path

def setup_console_encoding():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

# 必须在任何 print 之前调用
setup_console_encoding()

SCRIPT_DIR = Path(__file__).parent.resolve()
HERE = Path.cwd().resolve()


def resolve_python_cmd() -> list[str]:
    """批量配音须 Python 3.12（Kokoro / run_batch_course 兼容）。"""
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

PYTHON_CMD = resolve_python_cmd()

# ── 读取配置文件 ──────────────────────────────────────────────
def load_config() -> dict:
    defaults = {
        'voice': '晓晓',
        'engine': 'edge',
        'rate': '+0%',
        'original_vol': 0.15,
        'dub_vol': 3.0,
        'require_complete_dub': True,
        'max_ignored_missing_ratio': 0.001,
        'allow_partial_dub': True,
        'azure': None,
    }
    # 先读 GUI 全局配置（~/.tubeget/config.json）
    home_cfg = Path.home() / '.tubeget' / 'config.json'
    if home_cfg.exists():
        try:
            with open(home_cfg, encoding='utf-8') as f:
                hdata = json.load(f)
            if hdata.get('azure_key'):
                defaults['azure'] = {
                    'key': hdata['azure_key'],
                    'region': hdata.get('azure_region', 'eastasia'),
                }
            if hdata.get('engine'):
                defaults['engine'] = hdata['engine']
        except Exception:
            pass

    # 再读视频文件夹或脚本目录的配置（优先级更高）
    cfg_path = HERE / 'tubeget.config.json'
    if not cfg_path.exists():
        cfg_path = SCRIPT_DIR / 'tubeget.config.json'
    if cfg_path.exists():
        try:
            with open(cfg_path, encoding='utf-8') as f:
                data = json.load(f)
            defaults.update({k: v for k, v in data.items() if not k.startswith('_')})
            print(f'  [OK] 已读取配置文件：{cfg_path.name}')
            print(f'     配音员={defaults["voice"]}  引擎={defaults["engine"]}  语速={defaults["rate"]}')
        except Exception as e:
            print(f'  [!]  配置文件读取失败（{e}），使用默认值')
    else:
        print('  [i]  未找到 tubeget.config.json，使用默认配置')
    return defaults

CFG = load_config()

VOICE        = CFG['voice']
ENGINE       = CFG['engine']
RATE         = CFG['rate']
ORIGINAL_VOL = str(CFG['original_vol'])
DUB_VOL      = str(CFG['dub_vol'])
REQUIRE_COMPLETE_DUB = CFG['require_complete_dub']
AZURE_CFG    = CFG.get('azure') or {}

# ── 写入 Azure 环境变量（供 step1 读取）──────────────────────
if ENGINE == 'azure' and AZURE_CFG.get('key'):
    os.environ['AZURE_SPEECH_KEY']    = AZURE_CFG['key']
    os.environ['AZURE_SPEECH_REGION'] = AZURE_CFG.get('region', 'eastasia')
    print(f'   Azure TTS 已配置（region={AZURE_CFG.get("region","eastasia")}）')

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from step1_subtitle_to_audio import (
    dub_pipeline_fingerprint,
    video_base_from_zh_subtitle,
    is_dub_complete,
    load_dub_status,
    dub_output_mp3_path,
)
from dub_naming import DUB_TAG, dub_video_path, is_dub_output
from batch_report import (
    BatchReport,
    apply_pipeline_perf_from_status,
    probe_media_duration,
    print_batch_report,
)


# ── 文件名清理（去掉表情符号）────────────────────────────────
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U0001f926-\U0001f937"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF"
    "\u200d"
    "\ufe0f"
    "\u2b50"
    "\u2b55"
    "\u3030"
    "]+",
    flags=re.UNICODE
)

def clean_filename(name: str) -> str:
    cleaned = EMOJI_PATTERN.sub('', name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = cleaned.strip('_- ')
    return cleaned if cleaned else name

def sanitize_filenames(folder: Path):
    renamed = 0
    for f in folder.iterdir():
        if not f.is_file():
            continue
        clean = clean_filename(f.stem) + f.suffix
        if clean != f.name:
            new_path = f.parent / clean
            if not new_path.exists():
                f.rename(new_path)
                print(f'    重命名：{f.name} -> {clean}')
                renamed += 1
    if renamed:
        print(f'    共重命名 {renamed} 个文件')

# 跳过：英文、脚本生成的双语轨、配音成片字幕
SKIP_NAME_PARTS = (
    'sync-bilingual', 'bilingual', '.english', '.en_us.', '.en-us.',
)
# 识别：与 step1 find_zh_subtitle 对齐的常见中文命名
ZH_NAME_PARTS = (
    'zh-hans', 'zh-cn', 'zh_en', 'zh-en-merge', 'en-zh-merge',
    'chs', 'chinese', '简体', '中文',
)


def is_zh_subtitle(path: Path) -> bool:
    name = path.name.lower()
    stem = path.stem.lower()
    if stem.endswith('_c') or '.bilingual_c' in name:
        return False
    if any(m in name for m in SKIP_NAME_PARTS):
        return False
    # 纯英文字幕（.en.srt / xxx.en_US.srt）
    if re.search(r'(?:^|\.)(?:en(?:_us|-us)?)(?:\.|$)', stem):
        return False
    if any(m in name for m in ZH_NAME_PARTS):
        return True
    if re.search(r'\.zh(?:-hans|-cn)?$', stem):
        return True
    if name.endswith('.sync-zh.srt') or name.endswith('.sync-zh.vtt'):
        return True
    return False


def is_dub_source_subtitle(path: Path) -> bool:
    """仅中文字幕（zh-Hans / zh-CN / sync-zh 等），不用 bilingual。"""
    return is_zh_subtitle(path)


def is_dub_subtitle(path: Path) -> bool:
    return is_dub_source_subtitle(path)


def _dub_sub_priority(path: Path) -> int:
    n = path.name.lower()
    if 'zh-hans' in n or 'zh-cn' in n:
        return 0
    if 'sync-zh' in n:
        return 1
    if 'zh-en-merge' in n or 'en-zh-merge' in n:
        return 2
    return 5


def _zh_sub_priority(path: Path) -> int:
    return _dub_sub_priority(path)


def subtitle_video_base(path: Path) -> str:
    """归一化字幕对应的原视频主名。"""
    return video_base_from_zh_subtitle(str(path))


def pick_zh_subtitles(files: list[Path]) -> list[Path]:
    """同一视频只保留一条最优配音字幕，避免重复配音。"""
    candidates = [f for f in files if is_dub_subtitle(f)]
    by_base: dict[str, tuple[int, Path]] = {}
    for f in candidates:
        base = subtitle_video_base(f)
        pr = _dub_sub_priority(f)
        old = by_base.get(base)
        if old is None or pr < old[0]:
            by_base[base] = (pr, f)
    return [t[1] for t in sorted(by_base.values(), key=lambda x: x[1].name)]


def explain_subtitle_scan(files: list[Path]) -> None:
    if not files:
        print('  目录内无任何 .srt / .vtt 文件')
        return
    print('  目录字幕扫描：')
    for f in sorted(files):
        tag = '可用' if is_dub_source_subtitle(f) else '跳过'
        print(f'    [{tag}] {f.name}')

def find_zh_subtitle_for_video(video: Path, zh_subs: list) -> Path | None:
    vbase_clean = video.stem
    for sub in zh_subs:
        if subtitle_video_base(sub) == vbase_clean:
            return sub
    for sub in zh_subs:
        stem_base = subtitle_video_base(sub)
        if stem_base in vbase_clean or vbase_clean in sub.stem:
            return sub
    return None


def find_dub_subtitle_for_video(video: Path) -> Path | None:
    """在同目录为视频查找配音字幕（优先 zh-Hans，其次 *bilingual*.srt）。"""
    folder = video.parent
    files = list(folder.glob('*.srt')) + list(folder.glob('*.vtt'))
    subs = pick_zh_subtitles(files)
    hit = find_zh_subtitle_for_video(video, subs)
    if hit:
        return hit
    base = video.stem
    for pat in (
        f'{base}.zh-Hans.srt',
        f'{base}.zh-CN.srt',
        f'{base}.zh-hans.srt',
        f'{base}.zh-cn.srt',
        f'{base}.sync-zh.srt',
        f'{base}.en-zh-merge.srt',
    ):
        p = folder / pat
        if p.exists() and is_dub_source_subtitle(p):
            return p
    for f in files:
        if not is_dub_source_subtitle(f):
            continue
        if base.lower()[:20] in f.name.lower():
            return f
    from dub_naming import find_bilingual_sub_for_video
    bil = find_bilingual_sub_for_video(video, folder)
    if bil:
        return bil
    return None


def find_mp3_for_video(video: Path, sub: Path | None = None) -> Path | None:
    if sub:
        mp3 = dub_output_mp3_path(str(sub), VOICE)
        if mp3.exists() and mp3.stat().st_size > 1000:
            return mp3
    vbase = re.sub(
        r'\.(en|zh|zh-hans|zh-cn)$', '', video.stem, flags=re.IGNORECASE)
    for mp3 in video.parent.glob('*.mp3'):
        if mp3.stem.endswith('_C') and (
                mp3.stem.startswith(vbase) or vbase in mp3.stem):
            if mp3.stat().st_size > 1000:
                return mp3
    return None


def run_step1_subtitle(sub: Path, report: BatchReport | None = None) -> int:
    audio_out = dub_output_mp3_path(str(sub), VOICE)
    st = load_dub_status(str(sub))
    cmd = PYTHON_CMD + ['-u', str(SCRIPT_DIR / 'step1_subtitle_to_audio.py'),
           str(sub), '--voice', VOICE, f'--rate={RATE}', '--no-local-fallback']
    if REQUIRE_COMPLETE_DUB:
        cmd.append('--require-complete')
        if st and not st.get('complete'):
            cmd.append('--retry-failed')
    base = subtitle_video_base(sub)
    job = report.job(base, base) if report else None
    t0 = time.perf_counter()
    code = run(cmd)
    if job is not None:
        job.dub_wall_sec += time.perf_counter() - t0
        apply_pipeline_perf_from_status(job, load_dub_status(str(sub)))
    return code


def ensure_dub_mp3(
    video: Path, sub: Path | None = None, report: BatchReport | None = None,
) -> Path | None:
    """无 MP3 时从字幕合成；返回可用配音路径。"""
    sub = sub or find_dub_subtitle_for_video(video)
    if not sub:
        return None
    audio = find_mp3_for_video(video, sub)
    if audio:
        if not REQUIRE_COMPLETE_DUB or is_dub_complete(str(sub), VOICE):
            return audio
        st = load_dub_status(str(sub))
        if st and st.get('partial_accept') and audio.stat().st_size > 1000:
            return audio
    print(f'  无配音 MP3，先从字幕合成：{video.name}')
    print(f'           字幕：{sub.name}')
    code = run_step1_subtitle(sub, report)
    audio = find_mp3_for_video(video, sub)
    if audio and audio.stat().st_size > 1000:
        if code == 0:
            return audio
        if code == 2:
            st = load_dub_status(str(sub))
            if st and st.get('partial_accept'):
                print('  [提示] 配音略有缺句（已记入日志），仍继续合并')
                return audio
    return None

def run(cmd: list) -> int:
    print('  运行:', ' '.join(str(c) for c in cmd[:6]), '...')
    r = subprocess.run(cmd, env=os.environ.copy())
    return r.returncode


def _sync_dub_stats(report: BatchReport, video_stem: str, sub: Path | None) -> None:
    """把步骤一（按字幕主名）记录的配音耗时归并到对应视频条目。"""
    if not sub:
        return
    sub_base = subtitle_video_base(sub)
    if sub_base == video_stem or sub_base not in report.jobs:
        return
    src = report.jobs[sub_base]
    dst = report.job(video_stem)
    dst.dub_wall_sec += src.dub_wall_sec
    dst.subtitle_prepare_sec += src.subtitle_prepare_sec
    dst.tts_sec += src.tts_sec
    dst.audio_merge_sec += src.audio_merge_sec
    dst.dub_skipped = dst.dub_skipped or src.dub_skipped
    if src.status not in ('—', 'ok', '跳过'):
        dst.status = src.status
    elif dst.status == '—' and src.status != '—':
        dst.status = src.status
    if not dst.dub_mp3_sec and src.dub_mp3_sec:
        dst.dub_mp3_sec = src.dub_mp3_sec
    del report.jobs[sub_base]

def step1_needs_run(sub: Path, audio_out: Path) -> bool:
    if not audio_out.exists():
        return True
    if sub.stat().st_mtime > audio_out.stat().st_mtime:
        return True
    st = load_dub_status(str(sub))
    if st and st.get('pipeline_fingerprint') != dub_pipeline_fingerprint():
        return True
    if REQUIRE_COMPLETE_DUB and not is_dub_complete(str(sub), VOICE):
        if st and not st.get('complete'):
            return True
        if st and st.get('partial_accept'):
            return False
        return True
    return False

def main():
    setup_console_encoding()
    failed_steps = []
    pending_dub  = set()
    report = BatchReport()
    batch_t0 = time.perf_counter()
    prep_t0 = time.perf_counter()
    print('=' * 50)
    print('批量处理开始')

    # 清理文件名中的表情符号
    sanitize_filenames(HERE)
    print(f'  配音员={VOICE}  引擎={ENGINE}  语速={RATE}')
    print(f'  Python：{" ".join(PYTHON_CMD)}')
    print(f'  原声={ORIGINAL_VOL}  配音={DUB_VOL}  须完整={REQUIRE_COMPLETE_DUB}')
    print('=' * 50)
    report.prep_wall_sec = time.perf_counter() - prep_t0

    srt_files = list(HERE.glob('*.srt')) + list(HERE.glob('*.vtt'))
    zh_subs = pick_zh_subtitles(srt_files)

    # 步骤一
    print('\n[步骤一] 字幕 → 配音')
    if not zh_subs:
        print('  当前目录未找到配音字幕，步骤一跳过（步骤二会为每个视频单独找字幕）')
        print('  支持：*.zh-Hans.srt  *.zh-CN.srt  （仅用中文字幕，不处理 .en / bilingual）')
        explain_subtitle_scan(srt_files)
    else:
        print(f'  找到 {len(zh_subs)} 条配音字幕')
        for sub in zh_subs:
            audio_out = dub_output_mp3_path(str(sub), VOICE)
            base = subtitle_video_base(sub)
            job = report.job(base, base)
            if not step1_needs_run(sub, audio_out):
                print(f'  跳过（配音已齐）：{audio_out.name}')
                job.dub_skipped = True
                job.dub_mp3_sec = probe_media_duration(str(audio_out))
                apply_pipeline_perf_from_status(job, load_dub_status(str(sub)))
                continue
            code = run_step1_subtitle(sub, report)
            job.dub_mp3_sec = probe_media_duration(str(audio_out))
            if code == 2:
                st = load_dub_status(str(sub))
                if st and st.get('partial_accept') and audio_out.exists():
                    print(f'  [可忽略缺句] 已输出：{audio_out.name}')
                else:
                    title = video_base_from_zh_subtitle(str(sub))
                    pending_dub.add(title)
                    job.status = '挂起'
                    print(f'  [暂挂]  暂挂起（配音未齐）：{title}')
            elif code != 0:
                job.status = '失败'
                failed_steps.append(f'步骤一失败：{sub.name}')
                st = load_dub_status(str(sub))
                reason = (st or {}).get('exit_reason') or f'退出码 {code}'
                print(f'  [失败] 步骤一：{sub.name}（{reason}）')
                print(f'         提示：请向上滚动查看该字幕的详细报错；'
                      f'或查看同目录 {subtitle_video_base(sub)}.dub-status.json')

    # 步骤二
    print('\n[步骤二] 视频 + 配音 → 合并')
    video_exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm'}
    videos = [f for f in HERE.iterdir()
              if f.suffix.lower() in video_exts
              and not is_dub_output(f)
              and '_字幕' not in f.stem
              and '_预览' not in f.stem]

    if not videos:
        print('  未找到视频文件')
    else:
        for video in sorted(videos):
            output   = dub_video_path(video)
            zh_sub   = find_zh_subtitle_for_video(video, zh_subs)
            if not zh_sub:
                zh_sub = find_dub_subtitle_for_video(video)

            job = report.job(video.stem, video.name)
            job.video_sec = probe_media_duration(str(video))
            _sync_dub_stats(report, video.stem, zh_sub)
            if zh_sub:
                apply_pipeline_perf_from_status(
                    job, load_dub_status(str(zh_sub)))

            audio = find_mp3_for_video(video, zh_sub)
            if not audio:
                audio = ensure_dub_mp3(video, zh_sub, report)
            if audio:
                job.dub_mp3_sec = probe_media_duration(str(audio))

            if REQUIRE_COMPLETE_DUB and zh_sub and not audio:
                st = load_dub_status(str(zh_sub))
                if not (st and st.get('partial_accept')):
                    title = video_base_from_zh_subtitle(str(zh_sub))
                    pending_dub.add(title)
                    job.status = '挂起'
                    print(f'  跳过（配音未齐）：{video.name}')
                    continue

            if output.exists():
                if audio and audio.stat().st_mtime > output.stat().st_mtime:
                    print(f'  配音已更新，重新合并：{output.name}')
                else:
                    print(f'  跳过（已存在）：{output.name}')
                    job.merge_skipped = True
                    if job.status == '—':
                        job.status = 'ok'
                    continue

            if not audio:
                print(f'  [失败] 无配音且无字幕：{video.name}')
                print(f'         请放入同名 .zh-Hans.srt 或 .zh-CN.srt')
                job.status = '失败'
                failed_steps.append(f'无字幕/配音：{video.name}')
                continue

            merge_t0 = time.perf_counter()
            code = run([sys.executable, str(SCRIPT_DIR / 'step2_merge_video.py'),
                        str(video), str(audio),
                        '--original-vol', ORIGINAL_VOL,
                        '--dub-vol', DUB_VOL,
                        '--output', str(output)])
            job.merge_wall_sec += time.perf_counter() - merge_t0
            if code == 0:
                print(f'  [OK] 输出：{output.name}')
                job.status = 'ok'
            else:
                job.status = '失败'
                failed_steps.append(f'步骤二失败：{video.name}')

    report.batch_wall_sec = time.perf_counter() - batch_t0
    print_batch_report(report)

    print('\n' + '=' * 50)
    if pending_dub:
        print('以下视频配音未齐（可稍后重跑补全）：')
        for t in sorted(pending_dub): print(f'  - {t}')
    if failed_steps:
        print('处理完成（含失败项）：')
        for item in failed_steps: print(f'  - {item}')
    elif not pending_dub and not failed_steps:
        print('全部完成！')
    else:
        print('其它视频已处理；请稍后重跑以补全挂起项')
    print(f'  *{DUB_TAG}.mp4  = 最终配音视频')
    print('=' * 50)
    if pending_dub: sys.exit(2)

if __name__ == '__main__':
    main()
