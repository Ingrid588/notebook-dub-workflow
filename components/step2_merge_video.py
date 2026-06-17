#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
步骤二：视频 + 配音 → 合并新视频
==================================
把原视频和步骤一生成的配音 MP3 合并，
原声音量降低（保留环境音），中文配音替代主声道。

依赖：
    ffmpeg（需要安装并加入 PATH）
    下载：https://ffmpeg.org/download.html
    Windows 用户：下载后把 ffmpeg.exe 所在目录加入系统环境变量

用法：
    python step2_merge_video.py 视频.mp4 配音.mp3
    python step2_merge_video.py 视频.mp4 配音.mp3 --original-vol 0.1
    python step2_merge_video.py 视频.mp4 配音.mp3 --no-original

    # 批量处理整个目录
    python step2_merge_video.py --batch 视频目录/

参数：
    --original-vol  原声音量（0~1，默认 0.1，即10%环境音）
    --no-original   完全去掉原声
    --output        指定输出文件路径
    --batch         批量处理目录（自动匹配同名配音文件）
    --suffix        批量模式下配音文件的后缀（默认 _配音_晓晓）
"""

import argparse
import re
import os
import subprocess
import sys
from pathlib import Path

from dub_naming import DUB_TAG, dub_mp3_path, dub_video_path, is_dub_output

def setup_console_encoding():
    """在 Windows 控制台下尽量避免 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

def check_ffmpeg():
    """检查 ffmpeg 是否安装"""
    try:
        r = subprocess.run(['ffmpeg', '-version'], capture_output=True)
        return r.returncode == 0
    except FileNotFoundError:
        return False

def get_duration(path: str) -> float:
    """获取媒体文件时长（秒）"""
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', path],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0

def _atempo_chain(tempo: float) -> str:
    """ffmpeg atempo 链（0.25~4.0）。"""
    from step1_subtitle_to_audio import _build_atempo_filter_chain
    s = _build_atempo_filter_chain(tempo)
    return s or 'anull'


def merge(video_path: str, audio_path: str, output_path: str,
          original_vol: float = 0.1, no_original: bool = False,
          dub_vol: float = 2.0, speed_factor: float = 1.0):
    """
    合并视频和配音
    原声 × original_vol + 配音 = 最终音轨
    """
    if not os.path.exists(video_path):
        print(f'❌ 视频文件不存在：{video_path}')
        return False
    if not os.path.exists(audio_path):
        print(f'❌ 配音文件不存在：{audio_path}')
        return False

    print(f'\n📹 视频：{os.path.basename(video_path)}')
    print(f'🎙️  配音：{os.path.basename(audio_path)}')
    print(f'🔊 原声音量：{"静音" if no_original else f"{int(original_vol*100)}%"}')
    print(f'💾 输出：{output_path}')

    video_dur = get_duration(video_path)
    audio_dur = get_duration(audio_path)
    print(f'   视频时长：{video_dur:.1f}s  配音时长：{audio_dur:.1f}s')
    if video_dur <= 0:
        print('❌ 无法读取视频时长')
        return False

    sf = float(speed_factor)
    use_speed = abs(sf - 1.0) >= 0.008
    if use_speed:
        out_dur = video_dur / sf
        print(f'   配速整片微调：{sf:.3f}×（输出约 {out_dur:.1f}s，画面+配音同步）')
    else:
        out_dur = video_dur

    od = f'{out_dur:.3f}'
    vd = f'{video_dur:.3f}'
    dub_fit = (
        f'aformat=sample_rates=48000:channel_layouts=stereo,'
        f'atrim=0:{vd},apad=whole_dur={vd}'
    )
    if use_speed:
        tempo_f = _atempo_chain(sf)
        dub_fit = f'{dub_fit},{tempo_f},apad=whole_dur={od}'

    if no_original:
        if use_speed:
            audio_filter = (
                f'[0:v]setpts=PTS/{sf:.6f}[vout];'
                f'[1:a]{dub_fit},'
                f'volume={dub_vol:.2f},alimiter=limit=0.95[aout]'
            )
            cmd = [
                'ffmpeg', '-y',
                '-i', video_path,
                '-i', audio_path,
                '-filter_complex', audio_filter,
                '-map', '[vout]',
                '-map', '[aout]',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '20',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-t', od,
                output_path,
            ]
        else:
            audio_filter = (
                f'[1:a]{dub_fit},'
                f'volume={dub_vol:.2f},alimiter=limit=0.95[aout]'
            )
            cmd = [
                'ffmpeg', '-y',
                '-i', video_path,
                '-i', audio_path,
                '-filter_complex', audio_filter,
                '-map', '0:v',
                '-map', '[aout]',
                '-c:v', 'copy',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-t', vd,
                output_path,
            ]
    else:
        orig_chain = f'[0:a]atrim=0:{vd},apad=whole_dur={vd},volume={original_vol:.2f}'
        if use_speed:
            orig_chain += f',{_atempo_chain(sf)},apad=whole_dur={od}'
        orig_chain += '[orig]'
        audio_filter = (
            f'{orig_chain};'
            f'[1:a]{dub_fit},volume={dub_vol:.2f}[dub];'
            f'[orig][dub]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,'
            f'atrim=0:{od},alimiter=limit=0.95[aout]'
        )
        vmap = '[vout]' if use_speed else '0:v'
        if use_speed:
            audio_filter = (
                f'[0:v]setpts=PTS/{sf:.6f}[vout];'
                + audio_filter
            )
        cmd = [
            'ffmpeg', '-y',
            '-i', video_path,
            '-i', audio_path,
            '-filter_complex', audio_filter,
            '-map', vmap,
            '-map', '[aout]',
            '-c:v', 'libx264' if use_speed else 'copy',
            *(['-preset', 'fast', '-crf', '20'] if use_speed else []),
            '-c:a', 'aac',
            '-b:a', '192k',
            '-t', od if use_speed else vd,
            output_path,
        ]

    print('\n⏳ 合并中...')
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding='utf-8', errors='replace')

    if result.returncode != 0:
        print(f'❌ ffmpeg 失败：')
        print(result.stderr[-500:])  # 只显示最后500字符
        return False

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f'✅ 完成！输出文件：{output_path}（{size_mb:.1f} MB）')
    return True

def find_audio_for_video(video: Path, suffix: str = '') -> str | None:
    """匹配视频对应的配音 MP3（优先 原名_C.mp3）。"""
    expected = dub_mp3_path(video)
    if expected.is_file():
        return str(expected)

    stem = video.stem
    all_mp3 = list(video.parent.glob('*.mp3'))

    def score(mp3: Path) -> int:
        mp3_title = mp3.stem
        if mp3_title.endswith(DUB_TAG):
            mp3_title = mp3_title[:-len(DUB_TAG)]
        if '_配音' in mp3_title:
            mp3_title = mp3_title[:mp3_title.index('_配音')]
        mp3_title = re.sub(
            r'\.(en|zh|zh-hans|zh-cn|chs|en-zh-merge)$',
            '', mp3_title, flags=re.IGNORECASE)
        common = 0
        for a, b in zip(stem.lower(), mp3_title.lower()):
            if a == b:
                common += 1
            else:
                break
        return common

    candidates = [(mp3, score(mp3)) for mp3 in all_mp3 if score(mp3) >= 8]
    candidates.sort(key=lambda x: -x[1])
    if candidates:
        return str(candidates[0][0])
    return None


def find_subtitle_for_video(video: Path) -> str | None:
    """智能匹配视频对应的中文字幕（优先 zh，其次其他）"""
    base = video.stem.rsplit('.', 1)[0] if '.' in video.stem else video.stem
    stem = video.stem

    # 优先找中文字幕
    zh_priority = ['zh', 'zh-CN', 'zh-Hans', 'chs', 'chi', 'chinese']
    all_subs = list(video.parent.glob('*.srt')) + list(video.parent.glob('*.vtt'))

    # 按优先级找
    for zh in zh_priority:
        for sub in all_subs:
            if zh.lower() in sub.stem.lower() and (
                sub.stem.startswith(base) or sub.stem.startswith(stem)
            ):
                return str(sub)

    # 找任意同名字幕
    for sub in all_subs:
        sub_base = sub.stem.split('.')[0]
        if sub_base == base or sub_base == stem:
            return str(sub)

    return None


def batch_process(directory: str, suffix: str, original_vol: float,
                  no_original: bool):
    """批量处理目录下的视频文件"""
    video_exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm'}
    # 排除已经处理过的输出文件
    videos = [f for f in Path(directory).iterdir()
              if f.suffix.lower() in video_exts
              and not is_dub_output(f)
              and '_字幕' not in f.stem
              and '_预览' not in f.stem]

    if not videos:
        print(f'❌ 目录里没有找到视频文件：{directory}')
        return

    print(f'找到 {len(videos)} 个视频文件')
    ok, fail = 0, 0

    for video in sorted(videos):
        audio = find_audio_for_video(video, suffix)
        if not audio:
            print(f'\n⚠️  跳过（找不到配音 MP3）：{video.name}')
            fail += 1
            continue

        output = str(dub_video_path(video))
        print(f'\n匹配：{video.name} + {Path(audio).name}')
        if merge(str(video), audio, output, original_vol, no_original):
            ok += 1
        else:
            fail += 1

    print(f'\n批量完成：成功 {ok} 个，失败/跳过 {fail} 个')

def main():
    setup_console_encoding()
    parser = argparse.ArgumentParser(description='视频 + 配音 → 合并新视频')
    parser.add_argument('video',   nargs='?', help='视频文件路径')
    parser.add_argument('audio',   nargs='?', help='配音 MP3 路径')
    parser.add_argument('--original-vol', type=float, default=0.1,
                        help='原声音量 0~1（默认 0.1）')
    parser.add_argument('--dub-vol',      type=float, default=2.0,
                        help='配音音量倍数（默认 2.0，即放大一倍）')
    parser.add_argument('--no-original', action='store_true',
                        help='完全去掉原声')
    parser.add_argument('--output', default='',
                        help='输出文件路径')
    parser.add_argument('--batch',  default='',
                        help='批量模式：指定视频目录')
    parser.add_argument('--speed-factor', type=float, default=1.0,
                        help='整片同步变速（画面+配音，默认 1.0；配速流程自动传入）')
    args = parser.parse_args()

    # 检查 ffmpeg
    if not check_ffmpeg():
        print('❌ 未找到 ffmpeg！')
        print('   请下载安装：https://ffmpeg.org/download.html')
        print('   Windows：下载后把 bin 目录加入系统 PATH 环境变量')
        sys.exit(1)

    # 批量模式
    if args.batch:
        batch_process(args.batch, args.suffix, args.original_vol, args.no_original)
        return

    # 单文件模式
    if not args.video or not args.audio:
        parser.print_help()
        sys.exit(1)

    video = args.video
    audio = args.audio

    if args.output:
        output = args.output
    else:
        p = Path(video)
        output = str(dub_video_path(p))

    ok = merge(video, audio, output, args.original_vol, args.no_original,
               args.dub_vol, speed_factor=args.speed_factor)
    if not ok:
        sys.exit(1)

if __name__ == '__main__':
    main()
