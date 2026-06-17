#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量处理课程视频 - 直接使用 bilingual.srt 配音
==============================================
- 以中文为准：合并英文字幕拆开的相邻句，整句配音
- 英文字幕时间轴跟随合并后的中文
- 可选 AI 优化字幕（更顺滑）

运行环境：必须用 Python 3.12（py -3.12），不要用默认 python 3.13（Kokoro 不支持）。
"""

import os
import re
import sys
import json
import asyncio
import shutil
import argparse
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# 编码修复
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = Path(__file__).parent.resolve()
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from step1_subtitle_to_audio import (
    synth_one_local,
    synth_one,
    adjust_speed,
    merge_audio as merge_cues_audio,
    dub_work_dir,
    expand_bilingual_cues_to_speech_units,
    _load_local_voice_prefs,
    resolve_local_voice_id,
    last_chance_local_fill,
    can_ignore_missing_dub,
    log_ignored_missing_dub,
    _load_dub_policy_config,
    filter_cues_contextless_music,
)
from edge_tts_scheduler import (
    edge_config_for_small_batch,
    load_edge_scheduler_config,
    print_edge_scheduler_config,
    run_edge_scheduler,
    should_skip_edge_for_remainder,
)
from step2_merge_video import merge as merge_video_audio
from dub_naming import (
    DUB_TAG,
    active_dub_tag,
    dub_stem,
    dub_mp3_path,
    dub_video_path,
    ensure_dub_tag,
    dub_tag_is_explicit,
    is_dub_output,
    kokoro_samples_dir_from_work,
    save_bilingual_sub_for_dub,
    save_kokoro_sample,
    sync_cues_to_dub_playback,
)
from cue_synth_eligibility import (
    extract_chinese,
    tts_text_from_cue,
    scan_cues_for_synth,
    print_unsynth_report,
    apply_unsynth_cues,
    is_synth_eligible,
    filter_synth_eligible_indices,
)
from ai_shorten import (
    load_secrets,
    save_secrets,
    max_zh_chars_for_window,
    cue_speech_params,
    split_bilingual_text,
    rewrite_cues_with_gemini,
    rewrite_cues_speech_aligned,
    shorten_zh_gemini,
    ensure_gemini_api,
    ask_api_keys_dialog,
    dub_prep_config,
    prepare_cues_for_dub,
    print_cue_analysis,
    assess_gemini_translate_strategy,
    resolve_gemini_model,
    normalize_gemini_model,
    GEMINI_MODEL_CHOICES,
    GEMINI_SUBTITLE_MODEL_IDS,
    DEFAULT_GEMINI_MODEL,
    gemini_model_combobox_values,
    gemini_model_label,
    gemini_combobox_index_for_id,
    resolve_gemini_model_from_ui,
    SECRETS_PATH,
)
from speech_activity import (
    speech_activity_config,
    analyze_video_speech,
    merge_cues_respecting_pauses,
    apply_speech_windows_to_cues,
    build_speech_activity_report,
    save_speech_activity_report,
    post_tts_speech_adjust_batch,
    split_cues_for_speech_window,
)
from speech_grouping import (
    natural_speech_grouping,
    build_speech_grouping_report,
    save_speech_grouping_report,
)
from pipeline_metrics import (
    PipelineTimer,
    build_pipeline_metrics,
    save_pipeline_metrics,
)
from progress_log import pipeline_step, stage
from terminology import get_glossary
from gemini_log import (
    GeminiLogSession,
    cues_data_from_log_json,
    cues_data_from_stats,
    gemini_compare_txt_path,
    gemini_log_enabled,
    save_gemini_rewrite_texts,
    save_gemini_skipped_report,
    save_dub_run_summary,
)
from dub_qc_subtitles import build_dub_qc_payload, write_dub_qc_files
from gemini_workflow import (
    build_force_source_plan,
    build_gemini_send_plan,
    print_gemini_workflow_plan,
    save_gemini_run_report,
    save_batch_run_index,
    gemini_run_txt_path,
    GEMINI_ALL_OK_MSG,
)
from dub_cost_policy import (
    dub_translate_once_policy,
    gemini_allowed_in_dub_pipeline,
    gemini_allowed_post_tts,
    print_translate_once_banner,
    subtitle_is_fit_dub,
)
from gemini_chunk_translate import (
    build_semantic_chunks,
    chunks_to_plan,
    should_use_chunk_translate,
    translate_cues_via_semantic_chunks,
    write_translated_zh_srt,
)

# ── 配置（本目录 tubeget.config.json 优先，不被 ~/.tubeget 的 engine 覆盖）──
def load_config() -> dict:
    defaults = {
        'voice': '晓晓',
        'engine': 'edge+kokoro',
        'kokoro_voice': 'zf_xiaoxiao',
        'rate': '+0%',
        'max_rate_pct': 15,
        'baseline_playback_speed': 1.25,
        'original_vol': 0.15,
        'dub_vol': 2.0,
        'no_original': True,
        'require_complete_dub': True,
        'min_dub_success_rate': 0.98,
        'max_ignored_missing_ratio': 0.001,
        'allow_partial_dub': True,
        'final_retry_rounds': 2,
        'gemini_min_overflow_ratio': 1.12,
        'gemini_skip_merge': True,
        'gemini_skip_suspicious': True,
        'use_zh_compress_strategy': True,
        'build_strategy_samples': 25,
        'tts_concurrency': 1,
        'cue_workers': 0,
        'video_workers': 0,
        'edge_timeout_sec': 12,
        'edge_rounds': 4,
        'zh_chars_per_sec': 4.0,
        'max_playback_speed': 1.5,
        'gemini_rewrite': True,
        'zh_en_char_ratio': 1.0,
        'gemini_rewrite_batch': 20,
        'dub_one_tts_per_cue': True,
        'dub_translate_once': True,
        'gemini_dub_rewrite': False,
        'gemini_post_tts_rewrite': False,
        'gemini_chunk_translate': False,
        'chunk_gap_sec': 2.0,
        'chunk_chars_per_sec': 4.5,
        'gemini_only_problem_cues': True,
        'gemini_preflight': True,
        'gemini_preflight_block': True,
        'gemini_min_source_ratio': 0.5,
        'min_cue_sec': 1.0,
        'short_zh_merge_max_chars': 8,
    }
    cfg_path = HERE / 'tubeget.config.json'
    if cfg_path.exists():
        try:
            with open(cfg_path, encoding='utf-8') as f:
                data = json.load(f)
            defaults.update({k: v for k, v in data.items() if not k.startswith('_')})
        except Exception:
            pass
    return defaults

CFG = load_config()
os.environ['TUBEGET_ENGINE'] = str(CFG.get('engine', 'kokoro'))

# Kokoro 中文音色（zm=男 zf=女）
# Kokoro 中文音色（名称接近 Edge，但非同一引擎；晓晓用 zf_xiaoxiao，勿用 zf_xiaobei 东北腔）
KOKORO_VOICE_MAP = {
    '云希': 'zm_yunxi',
    '云扬': 'zm_yunyang',
    '云健': 'zm_yunjian',
    '云枫': 'zm_yunxi',
    '云皓': 'zm_yunxi',
    '晓晓': 'zf_xiaoxiao',
    '晓伊': 'zf_xiaoni',
    '晓萱': 'zf_xiaoxiao',
    '晓梦': 'zf_xiaoxiao',
}


def get_kokoro_voice_id() -> str:
    """配置 kokoro_voice 优先，否则按 voice 名称映射。"""
    if CFG.get('kokoro_voice'):
        return str(CFG['kokoro_voice'])
    return KOKORO_VOICE_MAP.get(CFG.get('voice', ''), 'zm_yunxi')


def check_kokoro_ready() -> bool:
    if sys.version_info >= (3, 13):
        print('  [错误] 当前是 Python 3.13+，Kokoro 无法运行')
        print('         本机请固定使用 Python 3.12：')
        print('         py -3.12 -m pip install -r requirements.txt')
        print('         py -3.12 run_batch_course.py')
        print('         或双击 批量处理课程.bat')
        return False
    if sys.version_info < (3, 10):
        print('  [错误] Kokoro 需要 Python 3.10–3.12')
        return False
    try:
        import kokoro  # noqa: F401
        import soundfile  # noqa: F401
    except ImportError:
        print('  [错误] 未安装 Kokoro，请执行：')
        print('         py -3.12 -m pip install -r requirements.txt')
        return False
    return True


def _cpu_count() -> int:
    return multiprocessing.cpu_count() or 4


def edge_timeout_sec() -> float:
    """Edge 单条请求超时（秒），超时即判失败，不在同条反复重试。"""
    try:
        return max(1.0, float(CFG.get('edge_timeout_sec', 4)))
    except (TypeError, ValueError):
        return 4.0


def edge_rounds_count() -> int:
    try:
        return max(1, min(6, int(CFG.get('edge_rounds', 4))))
    except (TypeError, ValueError):
        return 4


def ai_settings() -> dict:
    """
    合并 tubeget.config.json 与 tubeget.secrets.json 的 AI 配置。

    通道（用户在界面勾选「优化字幕」后选择）：
      - optimize_subtitle=False：完全免费通路，不调用任何 AI。
      - ai_provider='self'    ：自填 API（用用户自己的 Gemini Key）。
      - ai_provider='platform'：使用软件平台（商用授权，Key 由厂商内置）。
    """
    sec = load_secrets()

    # 是否优化字幕：用户选择（secrets）优先，其次配置文件
    optimize = sec.get('optimize_subtitle')
    if optimize is None:
        optimize = CFG.get('optimize_subtitle', CFG.get('gemini_rewrite', False))
    optimize = bool(optimize)

    provider = str(sec.get('ai_provider') or CFG.get('ai_provider', 'self')).lower()
    if provider not in ('self', 'platform'):
        provider = 'self'

    self_key = sec.get('gemini_api_key', '')
    platform_token = sec.get('platform_token', '')
    # 商用打包时由厂商内置；开源/个人版为空 = 平台通道未开通
    platform_key = CFG.get('platform_gemini_key', '')

    if not optimize:
        eff_key = ''
    elif provider == 'platform':
        eff_key = platform_key
    else:
        eff_key = self_key

    return {
        'optimize_subtitle': optimize,
        'ai_provider': provider,
        'gemini_key': eff_key,
        'self_key': self_key,
        'platform_token': platform_token,
        'platform_active': bool(platform_key),
        'gemini_model': resolve_gemini_model(sec, CFG),
        'gemini_rewrite': optimize,  # 兼容旧字段：优化开关即改写开关
        'zh_en_ratio': float(CFG.get('zh_en_char_ratio', 1.0)),
        'gemini_batch': int(CFG.get('gemini_rewrite_batch', 20)),
        'gemini_only_problem_cues': _effective_gemini_only_problem_cues(sec),
        'speech_activity_align': _effective_speech_activity_align(sec),
        'chars_per_sec': float(CFG.get('zh_chars_per_sec', 4.0)),
        'max_speed': float(CFG.get('max_playback_speed', 1.5)),
        'min_cue_sec': float(CFG.get('min_cue_sec', 1.0)),
        'short_zh_merge_max_chars': int(CFG.get('short_zh_merge_max_chars', 8)),
    }


def _effective_gemini_only_problem_cues(secrets: dict | None = None) -> bool:
    """secrets 优先：True=仅问题句送 Gemini，False=全部中文组。"""
    sec = secrets if secrets is not None else load_secrets()
    if 'gemini_only_problem_cues' in sec:
        return bool(sec['gemini_only_problem_cues'])
    return bool(CFG.get('gemini_only_problem_cues', True))


def _effective_speech_activity_align(secrets: dict | None = None) -> bool:
    sec = secrets if secrets is not None else load_secrets()
    if 'speech_activity_align' in sec:
        return bool(sec['speech_activity_align'])
    return bool(CFG.get('speech_activity_align', True))


def ensure_ai_keys(interactive: bool = True) -> bool:
    """混合模式：确保已配置 Gemini API Key。"""
    s = ai_settings()
    if s['gemini_key']:
        return True
    if not interactive:
        print('  [i] 未配置 Gemini Key，跳过字幕改写（直接用原中文）')
        return False
    print('\n  需要 Gemini API Key（配音前改写过长中文字幕）')
    data = ask_api_keys_dialog(load_secrets())
    if data is None:
        return False
    if data:
        save_secrets(data)
    return bool(data.get('gemini_api_key'))


def ask_dub_options(existing: dict | None = None) -> dict | None:
    """
    配音与 Gemini 设置弹窗（保存到 tubeget.secrets.json）。

    - 优化字幕 / Speech Activity 开关
    - Gemini 送优范围：全部中文组 vs 仅问题句
    - API Key、模型、AI 通道
    """
    existing = existing or load_secrets()
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except ImportError:
        return None

    cur_opt = existing.get('optimize_subtitle')
    if cur_opt is None:
        cur_opt = bool(CFG.get('optimize_subtitle', CFG.get('gemini_rewrite', False)))
    cur_sa = existing.get('speech_activity_align')
    if cur_sa is None:
        cur_sa = bool(CFG.get('speech_activity_align', True))
    cur_partial = existing.get('gemini_only_problem_cues')
    if cur_partial is None:
        cur_partial = bool(CFG.get('gemini_only_problem_cues', True))
    cur_provider = str(existing.get('ai_provider') or CFG.get('ai_provider', 'self')).lower()
    if cur_provider not in ('self', 'platform'):
        cur_provider = 'self'

    result = {'value': None}
    root = tk.Tk()
    root.title('配音与 Gemini 设置')
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = 620, 560
    root.geometry(f'{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}')
    root.lift()
    root.attributes('-topmost', True)
    root.after(300, lambda: root.attributes('-topmost', False))

    frm = ttk.Frame(root, padding=14)
    frm.pack(fill='both', expand=True)

    opt_var = tk.BooleanVar(value=bool(cur_opt))
    sa_var = tk.BooleanVar(value=bool(cur_sa))
    gemini_scope_var = tk.StringVar(
        value='problem_only' if cur_partial else 'all')
    prov_var = tk.StringVar(value=cur_provider)
    token_var = tk.StringVar(value=existing.get('platform_token', ''))
    from gemini_secrets import gemini_api_key_from_env, gemini_api_key_status
    model_var = tk.StringVar(
        value=gemini_model_label(
            existing.get('gemini_model')
            or CFG.get('ai_shorten_model_gemini', DEFAULT_GEMINI_MODEL)))

    ttk.Label(
        frm, text='配音前 Gemini 与节奏选项（保存后请运行「批量处理课程」）',
        font=('', 10, 'bold'),
    ).pack(anchor='w', pady=(0, 8))

    ttk.Checkbutton(
        frm, text='优化字幕（AI 改写译文，更顺滑；需 Gemini API）',
        variable=opt_var,
    ).pack(anchor='w')
    ttk.Checkbutton(
        frm, text='Speech Activity 口播节奏（VAD 自然分组 + 口播窗口；建议开启）',
        variable=sa_var,
    ).pack(anchor='w', pady=(0, 6))

    scope_box = ttk.LabelFrame(frm, text='Gemini 送优范围（勾选优化或 Speech Activity 后生效）', padding=10)
    scope_box.pack(fill='x', pady=(0, 8))
    ttk.Radiobutton(
        scope_box,
        text='全部中文组 — 每组都送 Gemini 口播优化（自然度优先，token 较多）',
        variable=gemini_scope_var, value='all',
    ).pack(anchor='w')
    ttk.Radiobutton(
        scope_box,
        text='仅问题句 — 只送超长 / 误译嫌疑 / 合并句（省 token，可对比测试）',
        variable=gemini_scope_var, value='problem_only',
    ).pack(anchor='w', pady=(4, 0))
    ttk.Label(
        scope_box,
        text='说明：Speech Activity 开启时同样受此选项约束；正常句将跳过 Gemini。',
        foreground='gray', wraplength=560, justify='left',
    ).pack(anchor='w', pady=(6, 0))

    box = ttk.LabelFrame(frm, text='AI 通道与 API', padding=10)
    box.pack(fill='x')

    ttk.Radiobutton(
        box, text='自填 API Key（从环境变量 GEMINI_API_KEY 读取）',
        variable=prov_var, value='self').pack(anchor='w')
    ttk.Label(
        box, text=gemini_api_key_status(),
        foreground='green' if gemini_api_key_from_env() else 'red',
        wraplength=520,
    ).pack(anchor='w', padx=(22, 0), pady=(0, 4))
    ttk.Label(
        box, text='Key 不写入项目文件；见 GEMINI_API_KEY_SETUP.txt',
        foreground='gray').pack(anchor='w', padx=(22, 0))

    ttk.Label(box, text='Gemini 模型（与 AI Studio 列表一致）').pack(
        anchor='w', padx=(22, 0), pady=(6, 0))
    combo_values = gemini_model_combobox_values()
    model_box = ttk.Combobox(
        box, textvariable=model_var, width=44,
        values=combo_values, state='readonly')
    model_box.pack(anchor='w', padx=(22, 0), pady=(0, 4))
    cur_id = normalize_gemini_model(
        existing.get('gemini_model')
        or CFG.get('ai_shorten_model_gemini', DEFAULT_GEMINI_MODEL))
    model_box.current(gemini_combobox_index_for_id(cur_id))
    model_var.set(gemini_model_label(cur_id))

    ttk.Radiobutton(
        box, text='使用软件平台（商用授权，免自己申请 Key）',
        variable=prov_var, value='platform').pack(anchor='w', pady=(8, 0))
    token_entry = ttk.Entry(box, textvariable=token_var, width=56)
    token_entry.pack(fill='x', padx=(22, 0), pady=(0, 2))
    plat_note = ttk.Label(box, text='', foreground='gray', wraplength=500)
    plat_note.pack(anchor='w', padx=(22, 0))

    def refresh(*_):
        on = opt_var.get() or sa_var.get()
        scope_children = scope_box.winfo_children()
        for w in scope_children:
            try:
                w.configure(state='normal' if on else 'disabled')
            except tk.TclError:
                pass
        if not on:
            token_entry.configure(state='disabled')
            model_box.configure(state='disabled')
        elif prov_var.get() == 'platform':
            token_entry.configure(state='normal')
            model_box.configure(state='readonly')
        else:
            token_entry.configure(state='disabled')
            model_box.configure(state='readonly')
        platform_active = bool(CFG.get('platform_gemini_key', ''))
        plat_note.configure(
            text='激活码 / 账号'
            if platform_active
            else '平台通道即将开放，当前请用「自填 API」')

    opt_var.trace_add('write', refresh)
    sa_var.trace_add('write', refresh)
    prov_var.trace_add('write', refresh)
    refresh()

    def on_save():
        data = dict(existing)
        data['optimize_subtitle'] = bool(opt_var.get())
        data['speech_activity_align'] = bool(sa_var.get())
        data['gemini_only_problem_cues'] = (
            gemini_scope_var.get() == 'problem_only')
        data['ai_provider'] = prov_var.get()
        if model_var.get().strip():
            m = resolve_gemini_model_from_ui(model_var.get().strip())
            if m not in GEMINI_MODEL_CHOICES:
                messagebox.showwarning('提示', '请从下拉列表选择 Gemini 模型')
                return
            if m not in GEMINI_SUBTITLE_MODEL_IDS:
                if not messagebox.askyesno(
                    '提示',
                    f'「{model_var.get()}」为 TTS/音频模型，不适合字幕改写。\n仍要保存吗？',
                ):
                    return
            data['gemini_model'] = m
        if token_var.get().strip():
            data['platform_token'] = token_var.get().strip()
        needs_key = (
            (data['optimize_subtitle'] or data['speech_activity_align'])
            and data['ai_provider'] == 'self'
            and not gemini_api_key_from_env())
        if needs_key:
            messagebox.showwarning(
                '提示',
                '已开启 Gemini 且选「自填 API」，但未检测到 GEMINI_API_KEY。\n'
                '请配置 Windows 用户环境变量（见 GEMINI_API_KEY_SETUP.txt）。',
            )
            return
        save_secrets(data)
        merged = load_secrets()
        data['gemini_api_key'] = merged.get('gemini_api_key', '')
        result['value'] = data
        root.destroy()

    def on_cancel():
        result['value'] = None
        root.destroy()

    bf = ttk.Frame(frm)
    bf.pack(fill='x', pady=(14, 0))
    ttk.Button(bf, text='保存', command=on_save).pack(side='left', padx=4)
    ttk.Button(bf, text='取消', command=on_cancel).pack(side='left', padx=4)
    ttk.Label(
        bf, text='→ tubeget.secrets.json',
        foreground='gray').pack(side='left', padx=8)

    root.protocol('WM_DELETE_WINDOW', on_cancel)
    root.mainloop()
    return result['value']


async def _prepare_zh_for_kokoro(cue: dict, zh: str) -> tuple[str, str]:
    """Kokoro 前处理中文：超字数则本地压缩（translate_once 下禁止 AI）。"""
    window = cue['end'] - cue['start']
    cfg = ai_settings()
    max_c = max_zh_chars_for_window(
        window,
        chars_per_sec=cfg['chars_per_sec'],
        max_speed=cfg['max_speed'],
    )
    if len(zh) <= max_c:
        return zh, f'原{len(zh)}字≤上限{max_c}'
    en, _ = split_bilingual_text(cue.get('text', ''))
    if dub_translate_once_policy(CFG)['translate_once'] or not cfg.get('gemini_key'):
        from zh_compress import compress_zh_for_dub, load_strategy
        short, how = compress_zh_for_dub(zh, max_c, load_strategy())
        return short, f'{len(zh)}→{len(short)}字(本地/{how})，上限{max_c}'
    short, src = await shorten_zh_gemini(
        zh, max_c,
        gemini_key=cfg['gemini_key'],
        gemini_model=cfg['gemini_model'],
        en_ref=en,
    )
    return short, f'{len(zh)}→{len(short)}字({src})，上限{max_c}'


def engine_mode() -> str:
    """规范化 engine 配置（支持 edge+kokoro / edge_kokoro / hybrid）。"""
    e = str(CFG.get('engine', 'kokoro')).lower().replace('_', '+')
    if e in ('hybrid', 'edge+kokoro'):
        return 'edge+kokoro'
    return e


def effective_tts_concurrency() -> int:
    """Edge / 混合模式下的 asyncio 并发数。"""
    n = int(CFG.get('tts_concurrency', 0) or 0)
    if n > 0:
        return max(1, min(n, 8))
    if engine_mode() == 'edge+kokoro':
        return 4
    if engine_mode() == 'kokoro':
        return 1
    return 3


def effective_cue_workers() -> int:
    """单视频内字幕分段多进程数；0=自动。"""
    engine = engine_mode()
    if engine == 'edge+kokoro':
        return 1
    n = int(CFG.get('cue_workers', 0) or 0)
    if n > 0:
        # Kokoro 每进程约 2~3GB 模型；16GB 内存建议 1，最多 2
        cap = 2 if engine == 'kokoro' else 16
        return max(1, min(n, cap))
    if engine == 'kokoro':
        return 1
    if engine == 'local':
        return max(2, min(6, _cpu_count() - 1))
    return max(2, min(8, _cpu_count()))


def effective_video_workers() -> int:
    """多视频并行进程数；0=自动。本地 SAPI 默认 1 路视频以免抢语音。"""
    n = int(CFG.get('video_workers', 0) or 0)
    if n > 0:
        return max(1, min(n, 8))
    engine = engine_mode()
    if engine in ('kokoro', 'local', 'edge+kokoro'):
        return 1
    return max(1, min(4, _cpu_count() // 2))


def split_into_chunks(items: list, n_workers: int) -> list[list]:
    n = max(1, min(n_workers, len(items)))
    buckets = [[] for _ in range(n)]
    for idx, item in enumerate(items):
        buckets[idx % n].append(item)
    return [b for b in buckets if b]

VOICE_MAP = {
    '晓晓': 'zh-CN-XiaoxiaoNeural',
    '晓伊': 'zh-CN-XiaoyiNeural',
    '云希': 'zh-CN-YunxiNeural',
    '云扬': 'zh-CN-YunyangNeural',
    '晓萱': 'zh-CN-XiaoxuanNeural',
    '云健': 'zh-CN-YunjianNeural',
    '晓梦': 'zh-CN-XiaomengNeural',
    '云枫': 'zh-CN-YunfengNeural',
    '晓辰': 'zh-CN-XiaochenNeural',
}


# ── 文件名清理（去掉表情符号）────────────────────────────────
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
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
    """去掉文件名中的表情符号"""
    cleaned = EMOJI_PATTERN.sub('', name)
    # 去掉多余空格
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # 去掉开头结尾的特殊符号
    cleaned = cleaned.strip('_- ')
    return cleaned if cleaned else name


def sanitize_filenames(folder: Path):
    """扫描文件夹，重命名含表情符号的文件"""
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


VIDEO_EXT = {'.mp4', '.mkv', '.avi', '.mov', '.webm'}


# ── 解析 SRT ──────────────────────────────────────────────────
def parse_srt(path: str) -> list:
    """解析 SRT 文件，返回 [{index, start, end, text}, ...]"""
    with open(path, encoding='utf-8-sig') as f:
        content = f.read()

    cues = []
    blocks = re.split(r'\n\s*\n', content.strip())
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        # 时间行
        time_match = re.search(
            r'(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})',
            lines[1]
        )
        if not time_match:
            continue
        start = srt_time_to_sec(time_match.group(1))
        end = srt_time_to_sec(time_match.group(2))
        text = '\n'.join(lines[2:]).strip()
        if text:
            cues.append({'start': start, 'end': end, 'text': text})
    return cues


def srt_time_to_sec(t: str) -> float:
    t = t.replace(',', '.')
    parts = t.split(':')
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


# ── TTS 合成 ──────────────────────────────────────────────────
def _load_azure_config() -> tuple:
    cfg_path = HERE / 'tubeget.config.json'
    if cfg_path.exists():
        try:
            with open(cfg_path, encoding='utf-8') as f:
                data = json.load(f)
            az = data.get('azure') or {}
            key = az.get('key', '')
            region = az.get('region', 'eastasia')
            if key:
                return key, region
        except Exception:
            pass
    return os.environ.get('AZURE_SPEECH_KEY', ''), os.environ.get('AZURE_SPEECH_REGION', 'eastasia')




# ── Kokoro TTS（本地离线）──────────────────────────────────────
_kokoro_pipeline = None

def get_kokoro_pipeline(*, quiet: bool = False):
    global _kokoro_pipeline
    if _kokoro_pipeline is None:
        from kokoro import KPipeline
        if not quiet:
            print('  Kokoro TTS 已加载（本地离线）', flush=True)
        _kokoro_pipeline = KPipeline(lang_code='z')
    return _kokoro_pipeline


async def _synth_kokoro(text, voice, speed, out_path, cue_index) -> bool:
    return _synth_kokoro_sync(text, voice, speed, out_path, cue_index)


def _synth_kokoro_sync(text, voice, speed, out_path, cue_index) -> bool:
    """使用 Kokoro 本地合成（同步，供多进程 worker 调用）。"""
    import numpy as np
    try:
        pipeline = get_kokoro_pipeline(quiet=True)
        audio_parts = []
        for _, _, audio in pipeline(text, voice=voice, speed=speed):
            audio_parts.append(audio)
        if not audio_parts:
            return False
        combined = np.concatenate(audio_parts)
        if len(combined) < 100:
            return False
        if len(combined) / 24000 < 0.8:
            label = f'第 {cue_index + 1} 条' if cue_index >= 0 else ''
            print(f'\n    [失败] {label} Kokoro 输出过短({len(combined)/24000:.2f}s)，'
                  f'请 py -3.12 -m pip install "kokoro>=0.9.2" "misaki[zh]>=0.9.2"')
            return False
        from pydub import AudioSegment
        pcm = (np.clip(combined, -1.0, 1.0) * 32767).astype(np.int16)
        seg = AudioSegment(
            pcm.tobytes(), frame_rate=24000, sample_width=2, channels=1)
        seg.export(out_path, format='mp3', bitrate='128k')
        return os.path.exists(out_path) and os.path.getsize(out_path) > 500
    except Exception as e:
        label = f'第 {cue_index + 1} 条' if cue_index >= 0 else ''
        print(f'\n    [失败] {label} Kokoro 合成失败: {e}')
        return False


async def synth_one(text: str, voice_id: str, rate: str,
                    out_path: str, retries: int = 3, cue_index: int = -1) -> bool:
    """合成单条音频（local / Kokoro / Azure / Edge）"""
    if not text.strip():
        return False

    engine = CFG.get('engine', 'local')

    if engine == 'local':
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, synth_one_local, text, out_path, rate)

    # Kokoro 本地神经语音（需 Python 3.10–3.12 + pip install kokoro soundfile）
    if engine == 'kokoro':
        # rate: "+0%" -> speed: 1.0, "+20%" -> 1.2, "-10%" -> 0.9
        try:
            pct = int(rate.replace('%', '').replace('+', ''))
            speed = 1.0 + pct / 100.0
        except:
            speed = 1.0
        # voice_id 映射到 Kokoro 音色
        return await _synth_kokoro(
            text, get_kokoro_voice_id(), speed, out_path, cue_index)

    azure_key, azure_region = _load_azure_config()

    if engine == 'azure' and azure_key:
        return await _synth_azure(text, voice_id, rate, out_path,
                                   azure_key, azure_region, retries, cue_index)
    else:
        return await _synth_edge(text, voice_id, rate, out_path, cue_index)


async def _synth_azure(text, voice_id, rate, out_path,
                        key, region, retries, cue_index) -> bool:
    import urllib.request
    import urllib.error
    import xml.sax.saxutils

    safe_text = xml.sax.saxutils.escape(text.strip())
    if not safe_text:
        return False

    ssml = (
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='zh-CN'>"
        f"<voice name='{voice_id}'>"
        f"<prosody rate='{rate}'>{safe_text}</prosody>"
        "</voice></speak>"
    )
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-16khz-128kbitrate-mono-mp3",
    }

    def _do_request():
        req = urllib.request.Request(url, data=ssml.encode('utf-8'), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read(), None
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')[:200]
            return None, f'HTTP {e.code}: {body}'
        except Exception as e:
            return None, f'{type(e).__name__}: {e}'

    loop = asyncio.get_event_loop()
    for attempt in range(1, retries + 1):
        data, err = await loop.run_in_executor(None, _do_request)
        if err:
            if attempt < retries:
                await asyncio.sleep(attempt * 2)
            continue
        if data and len(data) > 500:
            with open(out_path, 'wb') as f:
                f.write(data)
            return True
        if attempt < retries:
            await asyncio.sleep(attempt * 2)

    label = f'第 {cue_index + 1} 条' if cue_index >= 0 else ''
    print(f'\n    [失败] {label} Azure 合成失败')
    return False


async def _synth_edge(text, voice_id, rate, out_path,
                      cue_index: int = -1) -> bool:
    """Edge 单条合成：仅尝试 1 次，超过 edge_timeout_sec 即失败（避免反复请求被限流）。"""
    import edge_tts

    if not text.strip():
        return False

    timeout = edge_timeout_sec()
    try:
        from edge_tts_scheduler import edge_communicate
        comm = edge_communicate(text.strip(), voice_id, rate=rate)
        await asyncio.wait_for(comm.save(out_path), timeout=timeout)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 500:
            return True
    except asyncio.TimeoutError:
        pass
    except Exception:
        pass

    if os.path.exists(out_path):
        try:
            if os.path.getsize(out_path) <= 500:
                os.remove(out_path)
        except OSError:
            pass
    return False


async def synth_all(cues: list, voice_id: str, rate: str,
                    work_dir: str, concurrency: int = 2,
                    subtitle_path: str | None = None,
                    status_meta: dict | None = None) -> list:
    """合成所有字幕条目，返回 [(index, start, end, mp3_path), ...]"""
    if engine_mode() == 'edge':
        print_edge_scheduler_config(load_edge_scheduler_config())

        def _on_ok(_idx: int, mp3_path: str, cue: dict) -> None:
            tune_cue_playback(
                mp3_path, cue['end'] - cue['start'], cue.get('text', ''))

        results, _failed, _ = await run_edge_scheduler(
            cues,
            voice_id,
            rate,
            work_dir,
            subtitle_path=subtitle_path,
            status_meta=status_meta,
            text_extractor=lambda c: tts_text_from_cue(c),
            on_success=_on_ok,
        )
        ok_map = {i: (i, s, e, p) for i, s, e, p in results}
        out = []
        for i in range(len(cues)):
            if i in ok_map:
                out.append(ok_map[i])
            else:
                c = cues[i]
                out.append((i, c['start'], c['end'], None))
        return out

    sem = asyncio.Semaphore(concurrency)
    results = []
    total = len(cues)
    done_count = 0

    async def worker(i, cue):
        nonlocal done_count
        zh_text = extract_chinese(cue['text'])
        mp3_path = os.path.join(work_dir, f'cue_{i:04d}.mp3')

        async with sem:
            ok = False
            if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 500:
                ok = True
            elif zh_text:
                ok = await synth_one(zh_text, voice_id, rate, mp3_path, cue_index=i)

            if ok and os.path.exists(mp3_path):
                target_dur = cue['end'] - cue['start']
                tune_cue_playback(mp3_path, target_dur, cue.get('text', ''))

            done_count += 1
            pct = done_count * 100 // total
            bar = '#' * (pct // 5)
            print(f'\r    [{bar:<20}] {pct}%  {done_count}/{total}', end='', flush=True)

            return (i, cue['start'], cue['end'], mp3_path if ok else None)

    tasks = [worker(i, cue) for i, cue in enumerate(cues)]
    for coro in asyncio.as_completed(tasks):
        result = await coro
        results.append(result)

    print()
    results.sort(key=lambda x: x[0])
    return results


async def synth_all_hybrid(
    cues: list, voice_id: str, rate: str, work_dir: str,
    concurrency: int = 4,
    subtitle_path: str | None = None,
    status_meta: dict | None = None,
) -> list:
    """
    Edge 多轮 → Kokoro 补全 → 末轮重试（Edge/Kokoro/本地）→ 仍缺则留空位合并。
    支持 dub-work 缓存续跑（已成功 mp3 不重复合成）。
    """
    kokoro_voice = get_kokoro_voice_id()
    final_retries = int(CFG.get('final_retry_rounds', 2))

    total = len(cues)
    timeout = edge_timeout_sec()
    rounds = edge_rounds_count()
    by_i: dict[int, tuple] = {}
    cue_engines: dict[int, str] = {}
    kokoro_units: list[dict] = []

    def _mp3_path(i: int) -> str:
        return os.path.join(work_dir, f'cue_{i:04d}.mp3')

    def _is_ok(i: int) -> bool:
        r = by_i.get(i)
        return bool(r and r[3] and os.path.exists(r[3]))

    def _seed_cache() -> int:
        n = 0
        for i in range(total):
            mp3 = _mp3_path(i)
            if os.path.exists(mp3) and os.path.getsize(mp3) > 500:
                c = cues[i]
                by_i[i] = (i, c['start'], c['end'], mp3)
                if i not in cue_engines:
                    cue_engines[i] = 'cached'
                n += 1
        return n

    def _clear_failed_mp3(indices: list[int]) -> None:
        for i in indices:
            mp3 = _mp3_path(i)
            if os.path.exists(mp3):
                try:
                    os.remove(mp3)
                except OSError:
                    pass

    edge_probe_failed = False

    async def run_edge_pass(label: str, indices: list[int]) -> int:
        nonlocal edge_probe_failed
        if not indices:
            return 0
        pending = [i for i in indices if not _is_ok(i)]
        if not pending:
            print(f'  {label}：{len(indices)} 条均已缓存')
            return len(indices)
        print(f'  {label}（{len(pending)} 条待合成）')
        edge_cfg = edge_config_for_small_batch(
            len(pending), total, load_edge_scheduler_config())
        print_edge_scheduler_config(edge_cfg)

        def _on_ok(idx: int, mp3_path: str, cue: dict) -> None:
            by_i[idx] = (idx, cue['start'], cue['end'], mp3_path)
            cue_engines[idx] = 'edge'
            tune_cue_playback(
                mp3_path, cue['end'] - cue['start'], cue.get('text', ''))

        results, failed, edge_stats = await run_edge_scheduler(
            cues,
            voice_id,
            rate,
            work_dir,
            only_indices=set(pending),
            subtitle_path=subtitle_path,
            status_meta=status_meta,
            text_extractor=lambda c: tts_text_from_cue(c),
            on_success=_on_ok,
            skip_indices=unsynth_skip,
        )
        for i, s, e, p in results:
            if p:
                by_i[i] = (i, s, e, p)
        for i in failed:
            by_i[i] = (i, cues[i]['start'], cues[i]['end'], None)
        ok_count = sum(1 for i in indices if _is_ok(i))
        if edge_stats.get('probe_failed'):
            edge_probe_failed = True
        return ok_count

    async def run_kokoro_pass(indices: list[int], *, label: str = 'Kokoro') -> int:
        if not indices:
            return 0
        indices, skipped_unsynth = filter_synth_eligible_indices(
            indices, cues, text_extractor=lambda c: c['text'])
        if skipped_unsynth and label == 'Kokoro':
            print(f'  [Kokoro] 跳过 {len(skipped_unsynth)} 条不可合成句')
        if not indices:
            return 0
        if label == 'Kokoro':
            print(
                f'  [Kokoro] 开始本地配音 {len(indices)} 条'
                f'（首次加载模型约 30～90 秒，请稍候）…',
                flush=True)
        get_kokoro_pipeline(quiet=(label != 'Kokoro'))
        ok_n = 0
        shortened_n = 0
        try:
            base_pct = int(str(rate).replace('%', '').replace('+', ''))
        except ValueError:
            base_pct = 0
        kokoro_speed = 1.0 + base_pct / 100.0
        for n, i in enumerate(indices, 1):
            if _is_ok(i):
                ok_n += 1
                continue
            cue = cues[i]
            zh = extract_chinese(cue['text'])
            mp3_path = _mp3_path(i)
            ok = False
            if zh and is_synth_eligible(cue['text']):
                zh_use = zh
                if label == 'Kokoro':
                    zh_use, note = await _prepare_zh_for_kokoro(cue, zh)
                    if len(zh_use) < len(zh):
                        shortened_n += 1
                ok = _synth_kokoro_sync(
                    zh_use, kokoro_voice, kokoro_speed, mp3_path, i)
            if ok:
                by_i[i] = (i, cue['start'], cue['end'], mp3_path)
                cue_engines[i] = 'kokoro'
                sample_mp3 = save_kokoro_sample(work_dir, i, mp3_path, zh_use)
                kokoro_units.append({
                    'index': i,
                    'cue_no': i + 1,
                    'text': zh_use[:200],
                    'mp3': sample_mp3.name,
                    'label': label,
                })
                ok_n += 1
            else:
                by_i[i] = (i, cue['start'], cue['end'], None)
            if n % 10 == 0 or n == len(indices):
                print(f'\r    {label} {n}/{len(indices)}', end='', flush=True)
        print()
        if label == 'Kokoro':
            print(f'  Kokoro 补全：{ok_n}/{len(indices)}（其中 {shortened_n} 条经 AI/规则压缩）')
        elif ok_n:
            print(f'  {label}：{ok_n}/{len(indices)} 成功')
        return ok_n

    async def run_local_pass(indices: list[int]) -> int:
        if not indices:
            return 0
        indices, skipped_unsynth = filter_synth_eligible_indices(
            indices, cues, text_extractor=lambda c: c['text'])
        if skipped_unsynth:
            print(f'  本地语音：跳过 {len(skipped_unsynth)} 条不可合成句')
        if not indices:
            return 0
        loop = asyncio.get_event_loop()
        ok_n = 0
        for i in indices:
            if _is_ok(i):
                ok_n += 1
                continue
            cue = cues[i]
            zh = extract_chinese(cue['text'])
            mp3_path = _mp3_path(i)
            if not zh or not is_synth_eligible(cue['text']):
                continue
            ok = await loop.run_in_executor(
                None, synth_one_local, zh, mp3_path, rate)
            if ok:
                by_i[i] = (i, cue['start'], cue['end'], mp3_path)
                cue_engines[i] = 'local'
                ok_n += 1
                print(f'    第 {i + 1} 条 本地语音补全 OK')
        return ok_n

    def _write_kokoro_manifest() -> None:
        if not kokoro_units:
            return
        import json
        samples_dir = kokoro_samples_dir_from_work(work_dir)
        samples_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            'kokoro_voice': kokoro_voice,
            'total_kokoro': len(kokoro_units),
            'samples_dir': str(samples_dir),
            'units': kokoro_units,
        }
        manifest = samples_dir / 'manifest.json'
        with open(manifest, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _update_dub_status_engines() -> None:
        if not subtitle_path:
            return
        from step1_subtitle_to_audio import load_dub_status, write_dub_status
        st = load_dub_status(str(subtitle_path)) or {}
        cue_list = list(st.get('cues') or [])
        if len(cue_list) < total:
            cue_list.extend({} for _ in range(total - len(cue_list)))
        for i, eng in cue_engines.items():
            if i >= len(cue_list):
                continue
            entry = dict(cue_list[i] or {})
            entry['index'] = i
            entry['engine'] = eng
            if eng == 'unsynth':
                entry['status'] = 'unsynth'
                reason = unsynth_scan.unsynth.get(i)
                if reason:
                    entry['unsynth_reason'] = reason
            if eng == 'kokoro':
                entry['kokoro_sample'] = str(
                    kokoro_samples_dir_from_work(work_dir)
                    / f'cue_{i:04d}.mp3')
            cue_list[i] = entry
        counts: dict[str, int] = {}
        for eng in cue_engines.values():
            counts[eng] = counts.get(eng, 0) + 1
        st['cues'] = cue_list[:total]
        st['engine_counts'] = counts
        st['kokoro_samples_dir'] = str(kokoro_samples_dir_from_work(work_dir))
        if unsynth_scan.unsynth:
            st['unsynth_indices'] = sorted(unsynth_scan.unsynth.keys())
            st['unsynth_stats'] = unsynth_scan.count_by_reason()
        write_dub_status(str(subtitle_path), st)

    all_idx = list(range(total))
    cached = _seed_cache()
    if cached:
        print(f'  续跑：复用缓存 {cached}/{total} 条（仅补失败句）')

    unsynth_scan = scan_cues_for_synth(
        cues, text_extractor=lambda c: c['text'])
    print_unsynth_report(unsynth_scan)

    def _on_unsynth(i: int, mp3: str, reason: str) -> None:
        c = cues[i]
        by_i[i] = (i, c['start'], c['end'], mp3)
        cue_engines[i] = 'unsynth'

    n_unsynth = apply_unsynth_cues(
        cues,
        unsynth_scan,
        work_dir,
        mp3_path_fn=_mp3_path,
        on_applied=_on_unsynth,
    )
    for i in unsynth_scan.unsynth:
        if i not in cue_engines and _is_ok(i):
            cue_engines[i] = 'unsynth'
    if n_unsynth:
        print(f'  已为 {n_unsynth} 条不可合成句写入静音占位')

    unsynth_skip = frozenset(unsynth_scan.unsynth.keys())

    remainder = [i for i in all_idx if not _is_ok(i)]
    skip_edge = should_skip_edge_for_remainder(len(remainder), total, cached)
    if skip_edge and remainder:
        nums = ', '.join(str(i + 1) for i in remainder[:8])
        if len(remainder) > 8:
            nums += ' …'
        print(
            f'  Edge：仅剩 {len(remainder)} 条缺句（第 {nums} 句），'
            f'跳过网络轮次 → Kokoro/本地')
    elif not remainder:
        print('  Edge：缓存已齐，跳过网络合成')
    else:
        for round_no in range(1, rounds + 1):
            indices = [i for i in all_idx if not _is_ok(i)]
            if not indices:
                break
            if round_no > 1:
                _clear_failed_mp3(indices)
            n_ok = await run_edge_pass(
                f'[Edge {round_no}/{rounds}] 网络合成', indices)
            rate_pct = n_ok * 100.0 / max(len(indices), 1)
            print(f'  Edge 第{round_no}轮：{n_ok}/{len(indices)} 成功（{rate_pct:.1f}%）')
            if edge_probe_failed:
                print('  Edge 网络不可用，不再重试后续 Edge 轮次', flush=True)
                break

    edge_ok = sum(1 for i in all_idx if _is_ok(i))
    edge_rate = edge_ok * 100.0 / max(total, 1)
    print(f'  Edge 合计：{edge_ok}/{total}（{edge_rate:.1f}%）', flush=True)

    failed = [i for i in all_idx if not _is_ok(i)]
    if failed:
        print(f'  [Kokoro] 本地补全 {len(failed)} 条（先压缩超长中文）...')
        await run_kokoro_pass(failed, label='Kokoro')
    elif edge_ok:
        print(
            '  [准备] Edge 已全部成功，即将按字幕时间窗做加速/对齐…',
            flush=True,
        )

    for retry_no in range(1, final_retries + 1):
        failed = [i for i in all_idx if not _is_ok(i)]
        if not failed:
            break
        print(f'  [末轮补重试 {retry_no}/{final_retries}] 剩余 {len(failed)} 条 → Edge → Kokoro → 本地语音')
        await asyncio.sleep(2)
        if should_skip_edge_for_remainder(len(failed), total, cached):
            print(f'  补重试：缺句 {len(failed)} 条，跳过 Edge → Kokoro')
        else:
            _clear_failed_mp3(failed)
            await run_edge_pass(f'[补重试 Edge {retry_no}]', failed)
        failed = [i for i in all_idx if not _is_ok(i)]
        if failed:
            await run_kokoro_pass(failed, label=f'补重试 Kokoro {retry_no}')
        failed = [i for i in all_idx if not _is_ok(i)]
        if failed:
            loc = await run_local_pass(failed)
            if loc:
                print(f'  本地语音补全：{loc} 条')

    to_tune = [
        i for i in all_idx
        if (r := by_i.get(i)) and r[3] and os.path.exists(r[3])
    ]
    skip_tune = bool(CFG.get('speech_activity_align')) and bool(
        CFG.get('speech_window_soft', True))
    if to_tune and not skip_tune:
        print(
            f'  [准备] 正在按字幕时间窗加速/对齐 {len(to_tune)} 条配音'
            f'（ffmpeg 逐条处理，约需 1～5 分钟）…',
            flush=True,
        )
    for n, i in enumerate(to_tune, 1):
        r = by_i[i]
        target = r[2] - r[1]
        tune_cue_playback(r[3], target, cues[i].get('text', ''))
        if n % 15 == 0 or n == len(to_tune):
            print(f'\r    时间窗处理 {n}/{len(to_tune)}', end='', flush=True)
    if to_tune:
        print(flush=True)

    final_ok = sum(1 for i in all_idx if _is_ok(i))
    final_rate = final_ok * 100.0 / max(total, 1)
    print(f'  最终成功率：{final_ok}/{total}（{final_rate:.1f}%）')
    still = [i for i in all_idx if not _is_ok(i)]
    if still:
        preview = ', '.join(str(i + 1) for i in still[:12])
        if len(still) > 12:
            preview += ' …'
        print(f'  仍失败 {len(still)} 条（序号）：{preview}')
    if edge_rate < 70 and final_ok < total:
        print(f'  [提示] Edge 偏低，可调 edge_timeout_sec（当前 {timeout:g}s）'
              f' 或 edge_rounds（当前 {rounds}）')

    _write_kokoro_manifest()
    _update_dub_status_engines()

    eng_edge = sum(1 for e in cue_engines.values() if e == 'edge')
    eng_kokoro = sum(1 for e in cue_engines.values() if e == 'kokoro')
    eng_local = sum(1 for e in cue_engines.values() if e == 'local')
    eng_cached = sum(1 for e in cue_engines.values() if e == 'cached')
    eng_unsynth = sum(1 for e in cue_engines.values() if e == 'unsynth')
    print(f'  引擎统计：Edge {eng_edge}  Kokoro {eng_kokoro}  '
          f'本地 {eng_local}  缓存 {eng_cached}  不可合成(静音) {eng_unsynth}')
    if kokoro_units:
        kdir = kokoro_samples_dir_from_work(work_dir)
        print(f'  Kokoro 试听目录：{kdir}')
        print('  （每句：cue_XXXX.mp3 + cue_XXXX.txt，清单 manifest.json）')
        preview = kokoro_units[:8]
        for u in preview:
            print(f'    第 {u["cue_no"]} 句 → {u["mp3"]}')
        if len(kokoro_units) > 8:
            print(f'    … 共 {len(kokoro_units)} 句，见 manifest.json')

    return [
        by_i.get(i, (i, cues[i]['start'], cues[i]['end'], None))
        for i in all_idx
    ]


def _mp_worker_init():
    """子进程内懒加载 Kokoro（不在启动时 4 路同时拉模型）。"""
    pass


def _mp_synth_chunk(task: tuple) -> list:
    """
    子进程：合成一段字幕（同进程内顺序执行，避免 pyttsx3 多线程问题）。
    task: (work_dir, rate, voice_id, engine, kokoro_voice, items)
    items: [(i, start, end, zh_text), ...]
    """
    work_dir, rate, voice_id, engine, kokoro_voice, items = task
    chunk_results = []

    for i, start, end, zh_text in items:
        mp3_path = os.path.join(work_dir, f'cue_{i:04d}.mp3')
        ok = False
        if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 500:
            ok = True
        elif zh_text:
            if engine == 'local':
                ok = synth_one_local(zh_text, mp3_path, rate)
            else:
                try:
                    pct = int(rate.replace('%', '').replace('+', ''))
                    speed = 1.0 + pct / 100.0
                except Exception:
                    speed = 1.0
                if engine == 'kokoro':
                    ok = _synth_kokoro_sync(
                        zh_text, kokoro_voice, speed, mp3_path, i)
                else:
                    ok = asyncio.run(
                        synth_one(zh_text, voice_id, rate, mp3_path, cue_index=i))

        # 调速在 merge_cues_audio 合并时统一处理，此处不再逐条 ffmpeg

        chunk_results.append(
            (i, start, end, mp3_path if ok else None))

    return chunk_results


def synth_all_multiprocess(
    cues: list, voice_id: str, rate: str, work_dir: str,
    workers: int | None = None,
) -> list:
    """按段多进程合成字幕（每进程处理一段 cue）。"""
    workers = workers or effective_cue_workers()
    items = []
    for i, cue in enumerate(cues):
        zh = extract_chinese(cue['text'])
        items.append((i, cue['start'], cue['end'], zh))

    chunks = split_into_chunks(items, workers)
    engine = CFG.get('engine', 'local')
    kokoro_voice = get_kokoro_voice_id()
    tasks = [
        (work_dir, rate, voice_id, engine, kokoro_voice, chunk)
        for chunk in chunks
    ]

    print(f'  多进程合成：{workers} 进程 × {len(chunks)} 段，共 {len(cues)} 条')
    if engine == 'kokoro':
        print('  （纯 Kokoro 离线，不用 Edge；各进程首次合成时会加载模型，请稍候）')
    results = []
    done = 0
    total = len(cues)

    with ProcessPoolExecutor(
            max_workers=workers, initializer=_mp_worker_init) as pool:
        for partial in pool.map(_mp_synth_chunk, tasks):
            results.extend(partial)
            done += len(partial)
            pct = done * 100 // max(total, 1)
            bar = '#' * (pct // 5)
            print(f'\r    [{bar:<20}] {pct}%  {done}/{total}', end='', flush=True)

    print()
    results.sort(key=lambda x: x[0])
    return results


async def _retry_failed_cues(
    cues: list, results: list, voice_id: str, rate: str, work_dir: str,
) -> list:
    """多进程后串行补试失败句。"""
    by_i = {r[0]: list(r) for r in results}
    for round_no in range(2):
        failed_i = [
            i for i, r in by_i.items()
            if r[3] is None or not os.path.exists(r[3] or '')
        ]
        if not failed_i:
            break
        print(f'  自动重试第 {round_no + 1} 轮（{len(failed_i)} 条）...')
        await asyncio.sleep(2)
        for i in failed_i:
            cue = cues[i]
            zh = extract_chinese(cue['text'])
            mp3 = os.path.join(work_dir, f'cue_{i:04d}.mp3')
            if not zh:
                continue
            ok = await synth_one(zh, voice_id, rate, mp3, cue_index=i)
            if ok:
                by_i[i] = [i, cue['start'], cue['end'], mp3]
        await asyncio.sleep(1)
    out = [tuple(by_i[i]) for i in sorted(by_i)]
    return out


async def run_synth_all(
    cues: list, voice_id: str, rate: str, work_dir: str,
    subtitle_path: str | None = None,
    status_meta: dict | None = None,
) -> list:
    """按配置选择混合 / asyncio 并发或多进程分段合成。"""
    if engine_mode() == 'edge+kokoro':
        conc = effective_tts_concurrency()
        return await synth_all_hybrid(
            cues, voice_id, rate, work_dir, concurrency=conc,
            subtitle_path=subtitle_path, status_meta=status_meta)

    cw = effective_cue_workers()
    if cw > 1 and len(cues) >= cw and engine_mode() != 'edge':
        results = synth_all_multiprocess(cues, voice_id, rate, work_dir, cw)
        return await _retry_failed_cues(cues, results, voice_id, rate, work_dir)
    conc = effective_tts_concurrency()
    return await synth_all(
        cues, voice_id, rate, work_dir, concurrency=conc,
        subtitle_path=subtitle_path, status_meta=status_meta)


# ── 音频合并 ──────────────────────────────────────────────────
def get_mp3_duration(path: str) -> float:
    """粗略估算 MP3 时长"""
    try:
        size = os.path.getsize(path)
        return size / (128000 / 8)  # 128kbps
    except Exception:
        return 0



def get_audio_duration(path: str) -> float:
    """用 ffprobe 获取音频精确时长"""
    import subprocess
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, errors='replace')
        return float(r.stdout.strip())
    except Exception:
        return 0


def playback_speed_limits() -> tuple[float, float]:
    """返回 (baseline 整体提速, max_playback_speed 总倍率上限)。"""
    max_total = max(1.0, float(CFG.get('max_playback_speed', 1.5)))
    baseline = float(CFG.get('baseline_playback_speed', 1.0))
    baseline = max(1.0, min(baseline, max_total))
    return baseline, max_total


def apply_speed_factor(mp3_path: str, factor: float) -> bool:
    """固定倍率加速（ffmpeg atempo，用于整体 baseline 提速）。"""
    if factor <= 1.02:
        return False
    import subprocess
    tempo = round(min(factor, 2.0), 3)
    tmp = mp3_path + '.spd.mp3'
    r = subprocess.run(
        ['ffmpeg', '-y', '-i', mp3_path,
         '-filter:a', f'atempo={tempo}',
         '-b:a', '128k', tmp],
        capture_output=True, text=True, errors='replace')
    if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 100:
        os.replace(tmp, mp3_path)
        return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def tune_cue_playback(
    mp3_path: str,
    target_duration: float,
    cue_text: str = '',
) -> None:
    """TTS 后卡时间窗：Speech Activity 下加速≤1.3x，不顺延、不铺满。"""
    from step1_subtitle_to_audio import (
        _load_dub_pipeline_config,
        fit_audio_to_window,
        fit_audio_strict_window,
        adjust_speed,
        zh_has_continuation_mark,
    )
    from ai_shorten import split_bilingual_text

    if bool(CFG.get('speech_activity_align', False)):
        if bool(CFG.get('speech_window_soft', True)):
            return
        max_t = float(CFG.get('speech_activity_max_tempo', 1.5))
        hard_t = float(CFG.get('speech_activity_hard_max_tempo', 2.0))
        end_buf = float(CFG.get('speech_activity_end_buffer_sec', 0.0))
        slack_cfg = float(CFG.get('speech_activity_continuation_slack_sec', 1.5))
        _, zh = split_bilingual_text(cue_text)
        cont = zh_has_continuation_mark(zh)
        if cont:
            speak = target_duration
            slack = slack_cfg
        else:
            speak = max(0.25, target_duration - end_buf)
            slack = 0.0
        fit_audio_strict_window(
            mp3_path, target_duration,
            speak_duration=speak,
            max_tempo=max_t,
            hard_max_tempo=hard_t,
            allow_slack_sec=slack,
        )
        return

    baseline, max_total = playback_speed_limits()
    if baseline > 1.02:
        apply_speed_factor(mp3_path, baseline)
    fit_headroom = max(1.02, max_total / baseline) if baseline > 1.0 else max_total
    if target_duration <= 0.3:
        return
    pipe = _load_dub_pipeline_config()
    fill = bool(pipe.get('dub_fill_window', True))
    if int(pipe.get('dub_batch_merge_size', 0) or 0) >= 2:
        fit_audio_to_window(
            mp3_path, target_duration,
            max_tempo=fit_headroom,
            fill_window=fill,
        )
    else:
        adjust_speed(mp3_path, target_duration, max_tempo=fit_headroom)


def adjust_speed(mp3_path: str, target_duration: float, max_tempo: float = 1.5) -> bool:
    """如果配音超过目标时长，自动加速（最多 max_tempo 倍）"""
    import subprocess
    actual = get_audio_duration(mp3_path)
    if actual <= 0 or actual <= target_duration:
        return False  # 不需要调整

    ratio = actual / target_duration
    if ratio > max_tempo:
        # 超过上限，仍然加速到上限（未来可用 AI 简化）
        tempo = max_tempo
    else:
        tempo = round(ratio, 3)

    if tempo <= 1.02:
        return False  # 差距太小，不调

    tmp = mp3_path + '.adj.mp3'
    r = subprocess.run(
        ['ffmpeg', '-y', '-i', mp3_path,
         '-filter:a', f'atempo={tempo}',
         '-b:a', '128k', tmp],
        capture_output=True, text=True, errors='replace')

    if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 100:
        os.replace(tmp, mp3_path)
        return True
    else:
        if os.path.exists(tmp):
            os.remove(tmp)
        return False


def merge_audio(results: list, total_dur: float, out_path: str) -> bool:
    """把分段 MP3 按时间轴合并为一个完整配音文件（支持大量分段）"""
    import subprocess
    import tempfile

    entries = []
    for idx, start, end, mp3_path in results:
        if mp3_path and os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 500:
            entries.append((start, end, mp3_path))

    if not entries:
        print('  [失败] 无有效音频可合并')
        return False

    entries.sort(key=lambda x: x[0])
    success = len(entries)
    failed = len(results) - success
    print(f'  合并音频（{success} 条成功，{failed} 条静音）...', flush=True)

    parent = os.path.dirname(out_path)

    # 方案：生成静音底板 + 逐段叠加
    # 1. 生成总时长的静音音频
    print(f'  [进行中] 生成 {total_dur:.0f}s 静音底板…', flush=True)
    silence_path = os.path.join(parent, '_silence_base.mp3')
    r = subprocess.run(
        ['ffmpeg', '-y', '-f', 'lavfi',
         '-i', f'anullsrc=r=16000:cl=mono',
         '-t', str(total_dur + 1),
         '-b:a', '128k', silence_path],
        capture_output=True, text=True, errors='replace')
    if r.returncode != 0:
        print(f'  [失败] 生成静音底板失败')
        return False

    # 2. 分批叠加音频（每批最多 30 个）
    chunk_size = 30
    current = silence_path

    for ci in range(0, len(entries), chunk_size):
        chunk = entries[ci:ci + chunk_size]
        chunk_out = os.path.join(parent, f'_merge_chunk_{ci}.mp3')

        # 构建 ffmpeg 命令：当前底板 + 本批音频
        inputs = ['-i', current]
        filter_parts = []
        for i, (start, end, mp3_path) in enumerate(chunk):
            inputs.extend(['-i', mp3_path])
            delay_ms = int(start * 1000)
            filter_parts.append(f'[{i + 1}]adelay={delay_ms}|{delay_ms}[a{i}]')

        # 混合：底板 [0] + 各段延迟后的音频
        mix_inputs = '[0]' + ''.join(f'[a{i}]' for i in range(len(chunk)))
        n_inputs = len(chunk) + 1
        filter_parts.append(
            f'{mix_inputs}amix=inputs={n_inputs}:duration=first:dropout_transition=0[out]')

        cmd = ['ffmpeg', '-y'] + inputs + [
            '-filter_complex', ';'.join(filter_parts),
            '-map', '[out]',
            '-ac', '1', '-ar', '16000', '-b:a', '128k',
            chunk_out,
        ]

        r = subprocess.run(cmd, capture_output=True, text=True, errors='replace')
        if r.returncode != 0:
            print(f'  [失败] 第 {ci} 批合并失败')
            # 清理临时文件
            for f in Path(parent).glob('_merge_chunk_*'):
                f.unlink(missing_ok=True)
            if os.path.exists(silence_path):
                os.remove(silence_path)
            return False

        # 删除上一轮的临时文件
        if current != silence_path and os.path.exists(current):
            os.remove(current)
        current = chunk_out

        batch_num = ci // chunk_size + 1
        total_batches = (len(entries) + chunk_size - 1) // chunk_size
        print(f'    批次 {batch_num}/{total_batches} 完成')

    # 3. 最终文件
    if os.path.exists(current):
        shutil.move(current, out_path)

    # 清理
    if os.path.exists(silence_path):
        os.remove(silence_path)
    for f in Path(parent).glob('_merge_chunk_*'):
        f.unlink(missing_ok=True)

    print(f'  [OK] 音频合并完成：{os.path.basename(out_path)}')
    return True


def save_subtitles_for_dubbed(
    video: Path,
    subtitle: Path,
    cues: list | None = None,
    *,
    gemini: bool = False,
    quiet: bool = False,
):
    """为 _C 成片保存双语外挂字幕（一句一行显示）。"""
    return save_bilingual_sub_for_dub(
        video, subtitle, cues, gemini=gemini, quiet=quiet,
        display_max_zh=CFG.get('subtitle_display_max_zh'),
        display_max_en=CFG.get('subtitle_display_max_en'),
    )


# ── 主流程 ────────────────────────────────────────────────────
async def process_one(video: Path, subtitle: Path, voice: str, rate: str,
                      orig_vol: str, dub_vol: str, *, quiet: bool = False):
    """处理单个视频"""
    voice_id = VOICE_MAP.get(voice, voice)
    pipe_timer = PipelineTimer()
    pipe_timer.start_total()
    grouping_stats: dict = {}

    ensure_dub_tag(video.parent, video, quiet=quiet)
    out_mp3 = dub_mp3_path(video)
    out_video = dub_video_path(video)

    if out_video.exists() and dub_tag_is_explicit():
        if not quiet:
            print(f'  跳过（已存在）：{out_video.name}')
        save_subtitles_for_dubbed(video, subtitle, quiet=quiet)
        return True

    if not quiet:
        print(f'\n  视频：{video.name}')
        print(f'  字幕：{subtitle.name}')
        if subtitle_is_fit_dub(subtitle):
            print('  [字幕] 进框翻译 fit-dub（翻译层已处理）')
        cost_pol = dub_translate_once_policy(CFG)
        if cost_pol['translate_once']:
            print_translate_once_banner(quiet=False)
            if not subtitle_is_fit_dub(subtitle):
                print('  [提示] 建议先 fit_translate.bat；本流程配音阶段不调 Gemini')
        print(f'  配音员：{voice}（{voice_id}）  语速：{rate}')

    engine_name = engine_mode()
    if not quiet:
        if engine_name == 'local':
            print('  引擎：Windows 本地语音（pyttsx3，离线）')
        elif engine_name == 'kokoro':
            print(f'  引擎：Kokoro 神经语音（离线）音色：{get_kokoro_voice_id()}')
        elif engine_name == 'edge+kokoro':
            if dub_translate_once_policy(CFG)['translate_once']:
                print(
                    f'  引擎：Edge×{edge_rounds_count()}轮 → Kokoro（{get_kokoro_voice_id()}）'
                    f'，无配音阶段 Gemini')
            else:
                print(f'  引擎：Gemini改写字幕 → Edge×{edge_rounds_count()}轮 → Kokoro（{get_kokoro_voice_id()}）')
        elif engine_name == 'azure':
            az_key, az_region = _load_azure_config()
            print(f'  引擎：Azure TTS（{az_region}）')
        else:
            print(f'  引擎：Edge TTS')

    # 1. 解析字幕
    pipe_timer.begin('subtitle_prepare_sec')
    cues = parse_srt(str(subtitle))
    raw_cue_count = len(cues)
    cues, music_skipped = filter_cues_contextless_music(cues)
    original_cue_count = raw_cue_count
    if not quiet:
        print(f'  字幕条目：{len(cues)} 条')
        if music_skipped:
            print(f'  已删除无上下文「音乐」：跳过 {music_skipped} 条')

    if not cues:
        if not quiet:
            print('  [失败] 字幕为空')
        return False

    stage(
        '  流程：① 字幕准备/VAD → ② Gemini 优化（配音前） → ③ TTS → ④ 合并成片')

    sa_cfg = speech_activity_config(CFG)
    aic_pref = ai_settings()
    speech_align = bool(aic_pref.get('speech_activity_align', True))
    chunk_translate = (
        should_use_chunk_translate(CFG, subtitle_path=subtitle, cues=cues)
        and bool(aic_pref.get('optimize_subtitle'))
        and gemini_allowed_in_dub_pipeline(CFG)
    )
    vad_data: dict = {}
    sa_cue_stats: dict = {}
    glossary_text = ''

    # 1a. VAD + Natural Speech Grouping（Chunk 翻译模式保留原始行粒度，不在此合并）
    if speech_align:
        pipeline_step(1, 4, '字幕准备：VAD 说话节奏 + 自然语义分组…', quiet=quiet)
        if not quiet:
            print('  [Speech Activity] 分析原视频说话节奏（VAD）…')
        vad_data = analyze_video_speech(video, CFG)
        if vad_data.get('error'):
            if not quiet:
                print(f'  [警告] VAD 失败（{vad_data["error"]}），回退字幕时间轴')
            speech_align = False
        else:
            st = vad_data.get('stats') or {}
            if not quiet:
                print(
                    f'  说话 {st.get("total_speech_sec", 0):.1f}s / '
                    f'静音 {st.get("total_silence_sec", 0):.1f}s，'
                    f'长停顿 {st.get("long_pause_count", 0)} 处')
            if CFG.get('speech_natural_grouping', True) and not chunk_translate:
                if subtitle_is_fit_dub(subtitle):
                    if not quiet:
                        print(
                            '  fit-dub：跳过自然语义分组'
                            f'（{len(cues)} 条已进框，逐条 Edge 配音）')
                else:
                    cues, grouping_stats = natural_speech_grouping(
                        cues, vad_data, CFG)
                    if not quiet:
                        gs = grouping_stats
                        print(
                            f'  自然语义分组：{gs.get("before", 0)} → {gs.get("after", 0)} 组'
                            f'（合并 {gs.get("merged_pairs", 0)} 处，'
                            f'gap≤{CFG.get("speech_merge_gap_sec", 2.0)}s 优先合并）')
            else:
                cues, pause_merge = merge_cues_respecting_pauses(
                    cues, vad_data, CFG)
                if not quiet and pause_merge.get('merged'):
                    print(
                        f'  VAD 合并：{pause_merge["before"]} → {pause_merge["after"]} 条'
                        f'（跳过 {pause_merge["merged"]} 处长停顿）')

    # 1b. 短句合并 + 本地压缩（Chunk 模式延至 Gemini 之后，避免提前合并破坏时间轴）
    prep_cfg = dub_prep_config(CFG)
    if chunk_translate:
        prep_cfg = {
            **prep_cfg,
            'merge_zh_sentences': False,
            'only_problem_cues': False,
        }
        if not quiet:
            pipeline_step(
                1, 4,
                f'字幕准备：保留 {len(cues)} 行原时间轴（Chunk 翻译，'
                f'间隙<{CFG.get("chunk_gap_sec", 2.0)}s 合并送 Gemini）',
                quiet=quiet,
            )
        analysis = {
            'total_zh': len(cues),
            'prep': {'before': len(cues), 'after': len(cues)},
        }
    elif speech_align:
        prep_cfg = {**prep_cfg, 'merge_zh_sentences': False}
        cues, analysis = prepare_cues_for_dub(
            cues, float(CFG.get('zh_en_char_ratio', 1.0)), prep_cfg)
    else:
        if not quiet:
            pipeline_step(1, 4, '字幕准备：合并短句与本地压缩…', quiet=quiet)
        cues, analysis = prepare_cues_for_dub(
            cues, float(CFG.get('zh_en_char_ratio', 1.0)), prep_cfg)
    pipe_timer.end('subtitle_prepare_sec')

    if speech_align and vad_data and not vad_data.get('error') and not chunk_translate:
        cues, sa_cue_stats = apply_speech_windows_to_cues(cues, vad_data, CFG)
        if not quiet:
            if sa_cue_stats.get('soft_window'):
                print(
                    f'  口播窗口（软约束）：{len(cues)} 组，'
                    f'时间窗供 Gemini 参考，不要求说满')
            else:
                print(
                    f'  口播窗口：{sa_cue_stats.get("adjusted", 0)} 条已对齐说话区，'
                    f'{sa_cue_stats.get("clipped_at_pause", 0)} 条截断于长停顿')
        gloss = get_glossary()
        if gloss.size:
            glossary_text = gloss.collect_for_cues(cues)
            if not quiet:
                print(f'  术语表：已加载 {gloss.size} 条（{len(gloss.loaded_files)} 个 CSV）')

    aic = ai_settings()
    cost_pol = dub_translate_once_policy(CFG)
    use_gemini = (
        gemini_allowed_in_dub_pipeline(CFG)
        and bool(aic['gemini_key'])
        and (aic['optimize_subtitle'] or speech_align)
    )
    gemini_skip_reason = ''
    if cost_pol['translate_once']:
        use_gemini = False
        gemini_skip_reason = (
            '策略 dub_translate_once：翻译层 fit_translate 一次 Gemini，'
            '配音阶段不送 AI')
    gem_log_paths: tuple = (None, None)
    gem_plan: dict | None = None
    gem_stats: dict = {}

    if use_gemini:
        if chunk_translate:
            chunks = build_semantic_chunks(
                cues,
                gap_sec=float(CFG.get('chunk_gap_sec', 2.0)),
                chars_per_sec=float(CFG.get('chunk_chars_per_sec', 4.5)),
            )
            gem_plan = chunks_to_plan(chunks, CFG)
            gem_plan['total_zh'] = len(cues)
            gem_plan['to_send_count'] = gem_plan.get('total_lines', 0)
            gem_plan['translate_mode'] = 'chunk_translate'
            print_gemini_workflow_plan(
                gem_plan,
                model=aic['gemini_model'],
                quiet=quiet,
            )
        else:
            translate_strategy = assess_gemini_translate_strategy(
                cues, aic['zh_en_ratio'],
                only_problem_cues=aic['gemini_only_problem_cues'],
                cfg=prep_cfg,
            )
            if translate_strategy['force_source_all'] and not speech_align:
                gem_plan = build_force_source_plan(
                    cues, translate_strategy,
                    speech_align=speech_align,
                    only_problem_cues=aic['gemini_only_problem_cues'],
                    model=aic['gemini_model'],
                )
                print_gemini_workflow_plan(
                    gem_plan,
                    model=aic['gemini_model'],
                    quiet=quiet,
                )
            else:
                gem_plan = build_gemini_send_plan(
                    cues,
                    speech_align=speech_align,
                    only_problem_cues=aic['gemini_only_problem_cues'],
                    zh_en_ratio=aic['zh_en_ratio'],
                    prep_cfg=prep_cfg,
                )
                print_gemini_workflow_plan(
                    gem_plan,
                    model=aic['gemini_model'],
                    quiet=quiet,
                )
                if not quiet:
                    print_cue_analysis(analysis, analysis.get('prep', {}))
    else:
        if not gemini_skip_reason:
            if not aic['gemini_key']:
                gemini_skip_reason = '未配置 Gemini API Key'
                pipeline_step(
                    2, 4,
                    'Gemini 已跳过：未配置 API Key'
                    '（请先运行 配置.bat）',
                    quiet=quiet,
                )
            elif not aic['optimize_subtitle'] and not speech_align:
                gemini_skip_reason = '未开启字幕优化且未开 Speech Activity'
                pipeline_step(2, 4, f'Gemini 已跳过：{gemini_skip_reason}', quiet=quiet)
        elif cost_pol['translate_once'] and not quiet:
            pipeline_step(2, 4, gemini_skip_reason, quiet=quiet)

    if not quiet and use_gemini and (
            analysis.get('prep', {}).get('zh_sentence_merged')
            or analysis.get('prep', {}).get('merged_pairs')
            or engine_mode() == 'edge+kokoro'):
        pass  # 已在上方 print_cue_analysis
    elif not quiet and not use_gemini and (
            analysis.get('prep', {}).get('zh_sentence_merged')
            or analysis.get('prep', {}).get('merged_pairs')
            or engine_mode() == 'edge+kokoro'):
        print_cue_analysis(analysis, analysis.get('prep', {}))

    if use_gemini:
        pipe_timer.begin('gemini_sec')
        verified = await ensure_gemini_api(
            aic['gemini_key'], aic['gemini_model'],
            interactive=not quiet,
            prompt_if_missing_key=False,
        )
        if verified:
            gkey, gmodel = verified
            pipeline_step(
                2, 4,
                f'Gemini API 已连接（{gmodel}），'
                f'{"Chunk 语义块翻译" if chunk_translate else "一次翻译" if gem_plan and gem_plan.get("force_source_all") else "正在优化"} '
                f'字幕…',
                quiet=quiet,
            )

            async def _retry_gemini_key(err: str):
                return await ensure_gemini_api(
                    gkey, gmodel, interactive=False, error_hint=err,
                    prompt_if_missing_key=False,
                )

            gem_log = None
            if gemini_log_enabled(CFG):
                gem_log = GeminiLogSession(
                    video=video,
                    subtitle=subtitle,
                    model=gmodel,
                    mode=(
                        'speech_align' if speech_align
                        else 'chunk_translate' if chunk_translate
                        else 'source_translate' if gem_plan.get('force_source_all')
                        else 'rewrite'),
                )

            if speech_align and not chunk_translate:
                cues, gem_stats = await rewrite_cues_speech_aligned(
                    cues,
                    api_key=gkey,
                    model=gmodel,
                    batch_size=aic['gemini_batch'],
                    on_batch_error=_retry_gemini_key,
                    glossary_text=glossary_text,
                    log_session=gem_log,
                    only_problem_cues=aic['gemini_only_problem_cues'],
                    prep_cfg=prep_cfg,
                    zh_en_ratio=aic['zh_en_ratio'],
                )
            elif chunk_translate:
                cues, gem_stats = await translate_cues_via_semantic_chunks(
                    cues,
                    api_key=gkey,
                    model=gmodel,
                    cfg=CFG,
                    on_chunk_error=_retry_gemini_key,
                    log_session=gem_log,
                    subtitle_path=str(subtitle),
                )
                zh_srt_out = subtitle.parent / (
                    f'{dub_stem(video.stem)}.chunk-zh.srt')
                write_translated_zh_srt(cues, str(zh_srt_out))
                if not quiet:
                    print(f'  Chunk 中文字幕：{zh_srt_out.name}')
                raw_path = subtitle.parent / (
                    f'{dub_stem(video.stem)}.gemini-chunk-raw.srt')
                raw_parts = gem_stats.get('raw_responses') or []
                if raw_parts:
                    raw_path.write_text(
                        '\n\n'.join(raw_parts).strip() + '\n',
                        encoding='utf-8',
                    )
                    if not quiet:
                        print(f'  Gemini 原始回复：{raw_path.name}')
                cues, analysis = prepare_cues_for_dub(
                    cues, float(CFG.get('zh_en_char_ratio', 1.0)), prep_cfg)
                if speech_align and vad_data and not vad_data.get('error'):
                    cues, sa_cue_stats = apply_speech_windows_to_cues(
                        cues, vad_data, CFG)
                    if not quiet:
                        if sa_cue_stats.get('soft_window'):
                            print(
                                f'  口播窗口（软约束）：{len(cues)} 组，'
                                f'Gemini Chunk 后对齐 VAD')
                        else:
                            print(
                                f'  口播窗口：{sa_cue_stats.get("adjusted", 0)} 条已对齐说话区，'
                                f'{sa_cue_stats.get("clipped_at_pause", 0)} 条截断于长停顿')
            else:
                cues, gem_stats = await rewrite_cues_with_gemini(
                    cues,
                    api_key=gkey,
                    model=gmodel,
                    zh_en_ratio=aic['zh_en_ratio'],
                    batch_size=aic['gemini_batch'],
                    on_batch_error=_retry_gemini_key,
                    prep_cfg=prep_cfg,
                    only_problem_cues=aic['gemini_only_problem_cues'],
                    log_session=gem_log,
                    subtitle_path=str(subtitle),
                )

            changed = gem_stats.get('changed', 0)
            sent = gem_stats.get('sent', gem_stats.get('gemini_targets', 0))
            if sent:
                if gem_stats.get('mode') == 'chunk_translate':
                    pipeline_step(
                        2, 4,
                        f'Gemini 完成：{gem_stats.get("chunks", 0)} 块 / '
                        f'{sent} 行，改写 {changed} 行',
                        quiet=quiet,
                    )
                else:
                    pipeline_step(
                        2, 4,
                        f'Gemini 完成：API 送优 {sent} 组，改写 {changed} 组',
                        quiet=quiet,
                    )
            else:
                pipeline_step(
                    2, 4,
                    GEMINI_ALL_OK_MSG,
                    quiet=quiet,
                )

            gem_log_paths: tuple = (None, None, None, None)
            compare_txt: Path | None = None
            zh_txt: Path | None = None
            if gem_log is not None:
                gem_log_paths = gem_log.save(
                    video,
                    extra_summary={
                        'total_cues': len(cues),
                        'plan_send': gem_plan.get('to_send_count') if gem_plan else sent,
                    },
                    mirror_to_project_logs=bool(CFG.get('gemini_log_mirror', True)),
                )
                compare_txt = gem_log_paths[2]
                zh_txt = gem_log_paths[3]
                if gem_log.batches:
                    usage = record_video_gemini_usage(
                        gem_log, video=video, model=gmodel)
                    if usage.get('recorded') and not quiet:
                        print(
                            f'  Gemini 用量估算：+{usage["delta_chars"]:,} 字符'
                            f'（本月累计 {usage["month_total"]:,}）')
            elif gem_stats.get('compare_rows'):
                compare_txt, zh_txt = save_gemini_rewrite_texts(
                    video, gem_stats['compare_rows'],
                    mirror=bool(CFG.get('gemini_log_mirror', True)),
                )
            save_gemini_run_report(
                video, subtitle,
                plan=gem_plan,
                gem_stats=gem_stats,
                executed=True,
                model=gmodel,
                log_json=gem_log_paths[0],
                log_md=gem_log_paths[1],
                compare_txt=compare_txt,
                zh_txt=zh_txt,
                mirror=bool(CFG.get('gemini_log_mirror', True)),
                quiet=quiet,
            )
            if not quiet and compare_txt and compare_txt.exists():
                print(
                    f'  Gemini 对照：{compare_txt.name}'
                    f'（改前/改后，记事本打开）')
            if not quiet and zh_txt and zh_txt.exists():
                print(f'  Gemini 新字幕：{zh_txt.name}')
            elif not quiet and gem_log_paths[1] and os.path.exists(gem_log_paths[1]):
                sm = (gem_log.build_summary({'total_cues': len(cues)})
                      if gem_log is not None else {})
                print(
                    f'  Gemini 详表：{Path(gem_log_paths[1]).name}'
                    f'（送 {sm.get("sent_to_gemini", sent)} 改写 {sm.get("changed_count", changed)}）')
        else:
            gemini_skip_reason = gemini_skip_reason or 'Gemini API 验证失败'
            pipeline_step(
                2, 4,
                f'Gemini 已跳过：API 验证失败（{gemini_skip_reason}）',
                quiet=quiet,
            )
            save_gemini_run_report(
                video, subtitle,
                plan=gem_plan,
                skip_reason=gemini_skip_reason,
                executed=False,
                model=aic['gemini_model'],
                mirror=bool(CFG.get('gemini_log_mirror', True)),
                quiet=quiet,
            )
        pipe_timer.end('gemini_sec')

    if gemini_skip_reason and not use_gemini:
        save_gemini_run_report(
            video, subtitle,
            plan=gem_plan,
            skip_reason=gemini_skip_reason,
            executed=False,
            model=aic.get('gemini_model', ''),
            mirror=bool(CFG.get('gemini_log_mirror', True)),
            quiet=quiet,
        )
    if gemini_skip_reason and gemini_log_enabled(CFG):
        skip_md = save_gemini_skipped_report(
            video, subtitle, gemini_skip_reason,
            mirror=bool(CFG.get('gemini_log_mirror', True)))
        if not quiet:
            print(f'  Gemini 说明：{skip_md.name}（未调用 API 的原因）')
    elif gemini_skip_reason:
        pipeline_step(
            2, 4,
            f'Gemini 说明：{gemini_skip_reason}',
            quiet=quiet,
        )

    # 1c. 可选：时间窗超长拆句（V3 默认关闭，优先保持整句完整）
    if speech_align and CFG.get('speech_window_split_enabled', False):
        cues, win_split = split_cues_for_speech_window(cues, CFG)
        if not quiet and win_split.get('split_cues'):
            print(
                f'  时间窗拆句：{win_split["before"]} → {win_split["after"]} 条'
                f'（{win_split["split_cues"]} 处句尾加 —— 表示未完）')
    zh_cap = int(CFG.get('subtitle_display_max_zh') or 22)
    en_cap = int(CFG.get('subtitle_display_max_en') or 44)
    if CFG.get('dub_one_tts_per_cue', True):
        dub_cues = cues
        if not quiet and len(cues) > 0:
            print(
                f'  配音：{len(cues)} 组整句 TTS'
                f'（不拆行，烧录字幕时再拆显示）')
    else:
        dub_cues = expand_bilingual_cues_to_speech_units(
            cues, max_zh=zh_cap, max_en=en_cap,
            mark_continuation=bool(
                speech_align and CFG.get('speech_window_split_enabled', False)))

    # 2. 合成配音
    work_dir = dub_work_dir(str(subtitle))
    work_dir.mkdir(exist_ok=True)

    pipeline_step(
        3, 4,
        f'Edge/Kokoro TTS 合成：{len(dub_cues)} 句…',
        quiet=quiet,
    )
    status_meta = {
        'voice': voice,
        'voice_id': voice_id,
        'rate': rate,
        'total': len(dub_cues),
        'output_mp3': str(dub_mp3_path(video)),
    }
    pipe_timer.begin('edge_tts_sec')
    results = await run_synth_all(
        dub_cues, voice_id, rate, str(work_dir),
        subtitle_path=str(subtitle),
        status_meta=status_meta,
    )
    pipe_timer.end('edge_tts_sec')

    def _failed_indices(res: list) -> list[int]:
        return [
            i for i, _, _, p in res
            if not p or not os.path.exists(p) or os.path.getsize(p) <= 500
        ]

    failed_idx = _failed_indices(results)
    if failed_idx:
        extra, _ = await last_chance_local_fill(
            dub_cues, failed_idx, rate, str(work_dir))
        if extra:
            by_i = {i: (i, s, e, p) for i, s, e, p in results}
            for tup in extra:
                by_i[tup[0]] = tup
            results = [by_i[i] for i in range(len(dub_cues))]
            if not quiet:
                ok_n = sum(1 for _, _, _, p in results if p and os.path.exists(p))
                print(f'  末轮补全后：{ok_n}/{len(dub_cues)} 条成功')
        failed_idx = _failed_indices(results)

    success = sum(1 for _, _, _, p in results if p and os.path.exists(p))
    total_cues = len(dub_cues)

    # 2b. Speech Activity：TTS 后加速对齐（≤1.5×，不截断）；仍超窗送 Gemini 压缩并重合成
    sa_tts_stats: dict = {}
    if speech_align and success > 0:
        if not quiet:
            if bool(CFG.get('speech_window_soft', True)):
                print(
                    '  [Speech Activity] TTS 后校验（软窗口：轻度加速≤1.3×，'
                    '仍超窗送 Gemini 压缩重合成）…')
            else:
                print(
                    '  [Speech Activity] TTS 后校准（加速≤1.5×，不截断；'
                    '仍超窗则 Gemini 压缩并重合成）…')
        results, sa_tts_stats = await post_tts_speech_adjust_batch(
            dub_cues, results,
            gemini_key=aic.get('gemini_key', '') if gemini_allowed_post_tts(CFG) else '',
            gemini_model=aic.get('gemini_model', DEFAULT_GEMINI_MODEL),
            cfg=CFG,
        )
        resynth_idx = [
            i for i, c in enumerate(dub_cues) if c.get('_needs_resynth')]
        if resynth_idx:
            pipeline_step(
                3, 4,
                f'Gemini 压缩后重合成 {len(resynth_idx)} 条…',
                quiet=quiet,
            )
            from cue_synth_eligibility import extract_chinese
            from speech_activity import fit_tts_to_speech_window
            by_i = {i: (i, s, e, p) for i, s, e, p in results}
            for i in resynth_idx:
                cue = dub_cues[i]
                mp3_path = os.path.join(work_dir, f'cue_{i:04d}.mp3')
                zh = extract_chinese(cue.get('text', ''))
                ok = False
                if zh:
                    if engine_mode() == 'edge+kokoro':
                        ok = await _synth_edge(
                            zh, voice_id, rate, mp3_path, cue_index=i)
                    else:
                        ok = await synth_one(
                            zh, voice_id, rate, mp3_path, cue_index=i)
                if ok and os.path.exists(mp3_path):
                    fit_tts_to_speech_window(mp3_path, cue, CFG)
                    by_i[i] = (i, cue['start'], cue['end'], mp3_path)
                cue.pop('_needs_resynth', None)
            results = [by_i.get(i, (i, dub_cues[i]['start'], dub_cues[i]['end'], None))
                       for i in range(len(dub_cues))]
            sa_tts_stats['resynth_done'] = len(resynth_idx)
        if not quiet:
            print(
                f'  窗口处理：加速 {sa_tts_stats.get("speedup", 0)} 条，'
                f'送 Gemini 压缩 {len(sa_tts_stats.get("regenerate") or [])} 条')

    if not quiet:
        print(f'  合成完成：{success}/{total_cues} 条成功')
    if success < total_cues:
        policy = _load_dub_policy_config()
        min_rate = float(CFG.get('min_dub_success_rate', 0.98))
        allow_partial = CFG.get('allow_partial_dub', True)
        success_rate = success / max(total_cues, 1)
        missing = total_cues - success
        missing_ratio = missing / max(total_cues, 1)
        ignore_missing = can_ignore_missing_dub(missing, total_cues, policy)
        can_proceed = allow_partial and (
            success_rate >= min_rate or ignore_missing
        )
        if can_proceed:
            if ignore_missing and not quiet:
                log_path = log_ignored_missing_dub(
                    str(subtitle),
                    total=total_cues,
                    failed=sorted(failed_idx),
                    missing_ratio=missing_ratio,
                    voice=voice,
                    note='after_edge_kokoro_local',
                )
                print(
                    f'  [可忽略缺句] 缺 {missing} 条（{missing_ratio * 100:.4f}% ≤ 千分之一），'
                    f'缺句处静音，继续合并成片')
                print(f'     失败序号：{sorted(i + 1 for i in failed_idx)}')
                print(f'     已记入：{log_path.name}')
            elif not quiet:
                print(
                    f'  [提示] 缺 {missing} 条（{success_rate * 100:.1f}%），'
                    f'仍继续合并成片（缺句处为静音）')
        elif CFG.get('require_complete_dub', True):
            if not quiet:
                print(
                    f'  [失败] 配音未齐（{success}/{total_cues}），'
                    f'保留 {work_dir.name}/ 可续跑（仅补失败句）')
            return False

    # 3. 字幕与配音对齐（每句中文声 start/end）→ 再合并音频
    if not quiet:
        print('  [准备] 计算各句播放时刻并对齐字幕…', flush=True)
    cues_synced, placements = sync_cues_to_dub_playback(dub_cues, results)
    qc_payload = None
    if CFG.get('generate_dub_qc_subtitles', True):
        qc_payload = build_dub_qc_payload(
            dub_cues, results, cues_synced, failed_idx,
            cfg=CFG, sa_tts_stats=sa_tts_stats, video=video,
        )
    total_dur = max((c['end'] for c in cues_synced), default=0.0) + 1.0
    if not quiet:
        n_place = len(placements or [])
        print(
            f'  [准备] 合并 {n_place} 条配音为完整音轨'
            f'（总时长 {total_dur:.0f}s，叠加与导出较慢，请稍候）…',
            flush=True,
        )
    pipe_timer.begin('audio_merge_sec')
    merge_cues_audio(
        results, [], total_dur, str(out_mp3),
        placements=placements, dub_cues=dub_cues)
    pipe_timer.end('audio_merge_sec')
    ok = os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 1000

    # 清理缓存
    if work_dir.exists():
        if not quiet:
            print('  [准备] 清理临时配音缓存…', flush=True)
        shutil.rmtree(work_dir, ignore_errors=True)

    if not ok or not out_mp3.exists():
        if not quiet:
            print(f'  [失败] 音频合并失败')
        return False

    # 4. 合并视频
    no_orig = bool(CFG.get('no_original', True))
    if not quiet:
        if no_orig:
            print('  合并视频（仅中文配音轨，不含原声）...')
        else:
            print(f'  合并视频（原声 {float(orig_vol)*100:.0f}% + 配音）...')
    pipe_timer.begin('video_merge_sec')
    if not merge_video_audio(
            str(video), str(out_mp3), str(out_video),
            original_vol=float(orig_vol), dub_vol=float(dub_vol),
            no_original=no_orig):
        print(f'  [失败] 视频合并失败')
        return False
    pipe_timer.end('video_merge_sec')

    save_subtitles_for_dubbed(
        video, subtitle, cues_synced,
        gemini=bool(aic['optimize_subtitle'] and aic['gemini_key']),
        quiet=quiet)
    if qc_payload is not None:
        write_dub_qc_files(video, qc_payload, quiet=quiet)

    if speech_align and vad_data and not vad_data.get('error'):
        report = build_speech_activity_report(
            vad_data, sa_cue_stats,
            dub_results=results, dub_cues=dub_cues, cfg=CFG)
        report['tts_postprocess'] = sa_tts_stats
        rpath = save_speech_activity_report(video, report)
        if not quiet:
            sm = report.get('summary') or {}
            print(
                f'  节奏报告：{rpath.name} '
                f'（配音覆盖率 {sm.get("dub_coverage_ratio", 0) * 100:.1f}%，'
                f'长停顿保留率 {sm.get("long_pause_preserve_ratio", 0) * 100:.1f}%）')

    if grouping_stats.get('enabled'):
        greport = build_speech_grouping_report(
            original_cue_count, cues, grouping_stats, vad=vad_data)
        gpath = save_speech_grouping_report(video, greport)
        if not quiet:
            sm = greport.get('summary') or {}
            print(
                f'  分组报告：{gpath.name} '
                f'（{sm.get("original_subtitle_count")} 条 → {sm.get("group_count")} 组，'
                f'均 {sm.get("avg_cues_per_group", 0)} 条/组）')

    pipe_timer.end_total()
    tts_ok = sum(
        1 for _, _, _, p in results
        if p and os.path.exists(p) and os.path.getsize(p) > 500)
    metrics = build_pipeline_metrics(
        pipe_timer,
        tts_total=len(dub_cues),
        tts_ok=tts_ok,
        tts_fail=max(0, len(dub_cues) - tts_ok),
        extra={
            'speech_align': speech_align,
            'natural_grouping': bool(grouping_stats.get('enabled')),
            'soft_window': bool(CFG.get('speech_window_soft', True)),
        },
    )
    mpath = save_pipeline_metrics(video, metrics)
    if not quiet:
        st = metrics.get('stages_sec') or {}
        print(
            f'  性能报告：{mpath.name} '
            f'（TTS {metrics["tts"]["success_rate"] * 100:.1f}% 成功，'
            f'总耗时 {st.get("Total", st.get("total_sec", 0)):.1f}s）')

    from datetime import datetime
    from dub_qc_subtitles import dub_qc_zh_path

    grun_path = gemini_run_txt_path(video)
    qc_path = dub_qc_zh_path(video)
    sent_n = gem_stats.get('sent', 0) if use_gemini else 0
    gemini_status = (
        f'已调用 API（送 {sent_n} 组，改 {gem_stats.get("changed", 0)} 组）'
        if use_gemini and not gemini_skip_reason
        else (f'跳过 — {gemini_skip_reason}' if gemini_skip_reason else '未启用')
    )

    run_lines = [
        '=' * 54,
        f'  配音运行摘要  {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        '=' * 54,
        f'视频: {video.name}',
        f'成片: {out_video.name}',
        f'字幕: {subtitle.name}',
        f'Speech Activity: {"开" if speech_align else "关"}',
        f'Gemini: {gemini_status}',
        f'Gemini 模型: {resolve_gemini_model(cfg=CFG)}',
    ]
    if gem_plan:
        run_lines.append(
            f'Gemini 计划: {gem_plan.get("scope")}，'
            f'拟送 {gem_plan.get("to_send_count", 0)}/'
            f'{gem_plan.get("total_zh", 0)} 组')
    run_lines.extend([
        f'TTS: {tts_ok}/{len(dub_cues)} 成功',
        '',
        '【记事本可打开 — 在本视频文件夹】',
        f'  {grun_path.name}     Gemini 送优计划与结果（必看）',
        f'  {dub_stem(video.stem)}.gemini-compare.txt   改前/改后对照',
        f'  {dub_stem(video.stem)}.gemini-zh.txt        仅 Gemini 新字幕',
    ])
    if gem_stats.get('mode') == 'chunk_translate':
        run_lines.extend([
            f'  {dub_stem(video.stem)}.chunk-zh.srt          Chunk 中文字幕 SRT',
            f'  {dub_stem(video.stem)}.gemini-chunk-raw.srt  Gemini 原始 SRT 回复',
        ])
    run_lines.extend([
        f'  {dub_stem(video.stem)}.dub-run.txt   本摘要',
    ])
    if qc_path.exists():
        run_lines.append(f'  {qc_path.name}        质检字幕（查缺句/截断）')
    if gem_log_paths[1] and os.path.exists(gem_log_paths[1]):
        run_lines.append(f'  {Path(gem_log_paths[1]).name}   Gemini 逐句详表')
    run_lines.extend([
        '',
        '【项目副本 logs/gemini/】',
        '  latest_gemini_run.txt  latest_dub_run.txt  batch_latest.txt',
        '=' * 54,
    ])
    dub_run = save_dub_run_summary(video, run_lines, mirror=bool(CFG.get('gemini_log_mirror', True)))
    pipeline_step(
        4, 4,
        f'运行日志：{dub_run.name}、{grun_path.name}（视频同目录，记事本打开）',
        quiet=quiet,
    )

    if not quiet:
        max_db = _measure_max_volume_db(str(out_video))
        if max_db is not None and max_db < -25:
            print(f'  [警告] 成片峰值仅 {max_db:.1f} dB，可能偏小，请检查播放器音量')
        print(f'  [OK] 请播放：{out_video.name}（不是原片 {video.name}）')
    return True


def _measure_max_volume_db(path: str) -> float | None:
    import subprocess
    import re
    try:
        r = subprocess.run(
            ['ffmpeg', '-hide_banner', '-i', path,
             '-af', 'volumedetect', '-f', 'null', '-'],
            capture_output=True, text=True, errors='replace')
        m = re.search(r'max_volume:\s*([-\d.]+)\s*dB', r.stderr)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def _process_video_worker(job: dict) -> dict:
    """子进程：处理单个视频（供视频级多进程池调用）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    video = Path(job['video'])
    subtitle = Path(job['subtitle'])
    try:
        ok = asyncio.run(process_one(
            video, subtitle,
            job['voice'], job['rate'],
            job['orig_vol'], job['dub_vol'],
            quiet=True,
        ))
    except Exception as e:
        return {
            'ok': False,
            'folder': job['folder'],
            'video': video.name,
            'error': str(e),
        }
    return {
        'ok': ok,
        'folder': job['folder'],
        'video': video.name,
        'error': None,
    }


def find_bilingual_sub(video: Path) -> Path | None:
    """查找视频对应的 bilingual 字幕（仅 .srt）"""
    from dub_naming import find_bilingual_sub_for_video
    return find_bilingual_sub_for_video(video)


def natural_sort_key(name: str):
    """1, 2, 10 按数字顺序，而非 1, 10, 2。"""
    parts = re.split(r'(\d+)', name)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def list_chapter_dirs() -> list[str]:
    """列出含视频的子文件夹名（自然排序）。"""
    names = []
    for d in HERE.iterdir():
        if not d.is_dir() or d.name.startswith('.') or d.name.startswith('_'):
            continue
        if d.name == '__pycache__':
            continue
        if any(
            f.suffix.lower() in VIDEO_EXT and not is_dub_output(f)
            for f in d.iterdir() if f.is_file()
        ):
            names.append(d.name)
    return sorted(names, key=natural_sort_key)


def resolve_folder_names(raw: str, available: list[str]) -> list[str] | None:
    """
    解析用户输入的章节名（英文逗号分隔）。
    返回 None 表示处理全部；[] 表示无匹配。
    """
    text = raw.strip()
    if not text:
        return None
    picked = []
    for part in re.split(r'[,，]', text):
        token = part.strip()
        if not token:
            continue
        low = token.lower()
        exact = [a for a in available if a.lower() == low]
        if exact:
            picked.extend(exact)
            continue
        prefix = [a for a in available if a.lower().startswith(low)]
        if len(prefix) == 1:
            picked.append(prefix[0])
        elif len(prefix) > 1:
            print(f'  [警告] 「{token}」匹配多个文件夹，请写更完整：')
            for p in prefix[:5]:
                print(f'    - {p}')
        else:
            print(f'  [警告] 未找到文件夹：{token}')
    out = []
    seen = set()
    for n in picked:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def ask_select_folders_console(available: list[str]) -> str | None:
    """在控制台选择章节（从 .bat 启动时比弹窗更可靠）。"""
    print(f'\n  共 {len(available)} 个章节文件夹（含视频）：')
    for i, name in enumerate(available, 1):
        n_videos = sum(
            1 for f in (HERE / name).iterdir()
            if f.is_file() and f.suffix.lower() in VIDEO_EXT
            and not is_dub_output(f)
        )
        print(f'    {i:2}. {name}  ({n_videos} 个视频)')

    print('\n  直接回车 = 处理全部章节')
    print('  或输入序号/文件夹名，英文逗号分隔（例：1,3  或  1 - Introduction）')
    print('  输入 q = 取消')
    try:
        line = input('  请选择> ').strip()
    except (EOFError, KeyboardInterrupt):
        print('  已取消')
        return None

    if not line:
        return ''
    if line.lower() in ('q', 'quit', 'exit', '取消'):
        return None

    picked: list[str] = []
    for part in re.split(r'[,，]', line):
        token = part.strip()
        if not token:
            continue
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(available):
                picked.append(available[idx])
            else:
                print(f'  [警告] 序号超出范围：{token}')
        else:
            picked.append(token)
    if not picked:
        return ''
    return ', '.join(picked)


def ask_select_folders(available: list[str]) -> str | None:
    """
    弹窗选章节。返回：
      None — 取消；
      '' — 全部文件夹；
      非空 — 英文逗号分隔的文件夹名（写入输入框的内容）。
    """
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except ImportError:
        print('  [i] 无 tkinter，将处理全部章节文件夹')
        return ''

    result = {'value': None}

    print('\n  ==========================================')
    print('  已打开「选择章节」窗口 — 选好后点「确定」')
    print('  若被挡住，请到任务栏点「课程配音 - 选择章节」')
    print('  ==========================================')

    root = tk.Tk()
    root.title('课程配音 - 选择章节文件夹')
    root.geometry('720x480')
    root.minsize(560, 400)
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = 720, 480
    root.geometry(f'{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}')
    root.lift()
    root.attributes('-topmost', True)
    root.after(300, lambda: root.attributes('-topmost', False))
    root.focus_force()

    frm = ttk.Frame(root, padding=10)
    frm.pack(fill='both', expand=True)

    ttk.Label(
        frm,
        text='输入要处理的文件夹名（英文逗号分隔），或双击下方列表追加：',
        wraplength=660,
    ).pack(anchor='w')

    entry_var = tk.StringVar()
    entry = ttk.Entry(frm, textvariable=entry_var, width=80)
    entry.pack(fill='x', pady=(6, 8))
    entry.focus_set()

    list_frm = ttk.Frame(frm)
    list_frm.pack(fill='both', expand=True)

    scroll = ttk.Scrollbar(list_frm)
    scroll.pack(side='right', fill='y')

    lb = tk.Listbox(list_frm, yscrollcommand=scroll.set, height=14)
    lb.pack(side='left', fill='both', expand=True)
    scroll.config(command=lb.yview)

    for name in available:
        n_videos = sum(
            1 for f in (HERE / name).iterdir()
            if f.is_file() and f.suffix.lower() in VIDEO_EXT
            and not is_dub_output(f)
        )
        lb.insert('end', f'{name}  ({n_videos} 个视频)')

    def append_folder(_event=None):
        sel = lb.curselection()
        if not sel:
            return
        line = lb.get(sel[0])
        folder_name = line.rsplit('  (', 1)[0]
        cur = entry_var.get().strip()
        if cur and not cur.endswith(',') and not cur.endswith('，'):
            entry_var.set(cur + ', ' + folder_name)
        else:
            entry_var.set((cur + folder_name) if cur else folder_name)

    lb.bind('<Double-Button-1>', append_folder)

    btn_frm = ttk.Frame(frm)
    btn_frm.pack(fill='x', pady=(10, 0))

    def on_ok():
        result['value'] = entry_var.get().strip()
        root.destroy()

    def on_all():
        result['value'] = ''
        root.destroy()

    def on_cancel():
        result['value'] = None
        root.destroy()

    ttk.Button(btn_frm, text='确定', command=on_ok).pack(side='left', padx=4)
    ttk.Button(btn_frm, text='全部文件夹', command=on_all).pack(side='left', padx=4)
    ttk.Button(btn_frm, text='取消', command=on_cancel).pack(side='left', padx=4)
    ttk.Label(
        btn_frm,
        text='留空点「确定」= 全部',
        foreground='gray',
    ).pack(side='left', padx=12)

    root.protocol('WM_DELETE_WINDOW', on_cancel)
    root.mainloop()
    return result['value']


def collect_all_pairs(folder_filter: list[str] | None) -> list:
    """收集待处理 (文件夹名, 视频, 字幕)。"""
    all_pairs = []
    allowed = set(folder_filter) if folder_filter else None

    here_pairs = scan_folder(HERE)
    if here_pairs and (allowed is None or HERE.name in allowed):
        all_pairs.extend([(HERE.name, v, s) for v, s in here_pairs])

    for subdir_name in list_chapter_dirs():
        if allowed is not None and subdir_name not in allowed:
            continue
        subdir = HERE / subdir_name
        sub_pairs = scan_folder(subdir)
        if sub_pairs:
            all_pairs.extend([(subdir_name, v, s) for v, s in sub_pairs])
    return all_pairs


def collect_work_dir_pairs(work_dir: Path) -> list:
    """产品测试：只扫描指定文件夹（不递归子目录）。"""
    wd = work_dir.resolve()
    return [(wd.name, v, s) for v, s in scan_folder(wd)]


def scan_folder(folder: Path) -> list:
    """扫描文件夹中的视频和字幕配对"""
    pairs = []
    videos = [f for f in folder.iterdir()
              if f.is_file()
              and f.suffix.lower() in VIDEO_EXT
              and not is_dub_output(f)]
    for video in sorted(videos, key=lambda p: natural_sort_key(p.name)):
        sub = find_bilingual_sub(video)
        pairs.append((video, sub))
    return pairs


def parse_args():
    p = argparse.ArgumentParser(
        description='商用配音完整管线（Gemini API + VAD + TTS + QC）。'
                    '代码文件名 run_batch_course 为历史遗留，非「课程实验」专用。',
    )
    p.add_argument(
        '--folders', default='',
        help='多章节批量：components 下章节文件夹名，逗号分隔；空=全部',
    )
    p.add_argument(
        '--work-dir', default='',
        help='商用单文件夹测试：任意路径（mp4 + bilingual.srt）',
    )
    p.add_argument(
        '--no-dialog', action='store_true',
        help='不弹窗；未指定 --folders 时处理全部章节',
    )
    p.add_argument(
        '--cue-workers', type=int, default=0,
        help='单视频字幕多进程数，0=读配置/自动',
    )
    p.add_argument(
        '--video-workers', type=int, default=0,
        help='多视频并行进程数，0=读配置/自动',
    )
    p.add_argument(
        '--gui', action='store_true',
        help='用图形窗口选章节（默认在下方控制台输入）',
    )
    p.add_argument(
        '--api-keys', action='store_true',
        help='打开 Gemini API Key 快速配置（仅 Key+模型）',
    )
    p.add_argument(
        '--config', action='store_true',
        help='打开配音与 Gemini 完整设置（推荐：配置.bat）',
    )
    p.add_argument(
        '--build-zh-strategy', action='store_true',
        help='一次性：从长句样本让 AI 归纳口播压缩策略，保存到 tubeget.zh_strategy.json',
    )
    p.add_argument(
        '--import-zh-strategy', metavar='FILE',
        help='导入 NotebookLM/手工 JSON 策略（见 notebooklm_策略prompt.txt）',
    )
    p.add_argument(
        '--verify-requirements', action='store_true',
        help='对照用户需求 N 项自检后退出（不改代码也会打印 bat 说明）',
    )
    return p.parse_args()


def run_build_zh_strategy(folder_filter: list[str] | None = None) -> bool:
    """扫描字幕长句 → 一次 Gemini 调用 → 保存本地压缩策略。"""
    from step1_subtitle_to_audio import parse_srt
    from zh_compress import (
        build_strategy_with_gemini,
        sample_long_cues_for_strategy,
        save_strategy,
    )

    aic = ai_settings()
    if not aic['gemini_key']:
        print('  [错误] 需要 Gemini API Key：请先运行 配置.bat')
        return False
    verified = asyncio.run(ensure_gemini_api(
        aic['gemini_key'], aic['gemini_model'], interactive=True))
    if not verified:
        return False
    gkey, gmodel = verified

    prep_cfg = dub_prep_config(CFG)
    ratio = float(CFG.get('zh_en_char_ratio', 1.0))
    limit = int(CFG.get('build_strategy_samples', 25))
    all_samples: list[dict] = []

    chapters = list_chapter_dirs()
    if folder_filter:
        chapters = [c for c in chapters if c in folder_filter]

    for ch_name in chapters:
        ch_path = HERE / ch_name
        for video, sub in scan_folder(ch_path):
            if not sub:
                continue
            cues = parse_srt(str(sub))
            if not cues:
                continue
            merged, _ = prepare_cues_for_dub(cues, ratio, prep_cfg)
            samples = sample_long_cues_for_strategy(
                merged, ratio, prep_cfg, limit=limit)
            all_samples.extend(samples)

    if not all_samples:
        print('  未找到超长句样本（可能 1.5× 速预算下已全部合格）')
        return False

    all_samples.sort(key=lambda x: x['overflow'], reverse=True)
    picked = all_samples[:limit]
    print(f'  抽取 {len(picked)} 条典型长句，请求 Gemini 归纳压缩策略…')
    try:
        strategy = build_strategy_with_gemini(picked, gkey, gmodel)
    except Exception as e:
        print(f'  [失败] {e}')
        return False

    out = save_strategy(strategy)
    print(f'  [OK] 策略已保存：{out.name}')
    print(f'       drop_phrases {len(strategy.get("drop_phrases", []))} 条，'
          f'replacements {len(strategy.get("literal_replacements", []))} 条')
    if strategy.get('notes'):
        print(f'       说明：{strategy["notes"][:120]}')
    print('  之后跑配音将自动用内置+该策略本地压缩，仅仍超长句才送 Gemini。')
    return True


def run_import_zh_strategy(strategy_file: str) -> bool:
    """导入 NotebookLM / 手工策略 JSON。"""
    from zh_compress import (
        DEFAULT_STRATEGY_PATH,
        import_strategy_file,
        preview_strategy_on_samples,
        sample_long_cues_for_strategy,
        save_strategy,
    )
    from step1_subtitle_to_audio import parse_srt

    path = Path(strategy_file)
    if not path.is_file():
        print(f'  [错误] 文件不存在：{path}')
        return False
    try:
        merged = import_strategy_file(path)
    except Exception as e:
        print(f'  [失败] {e}')
        return False

    prep_cfg = dub_prep_config(CFG)
    ratio = float(CFG.get('zh_en_char_ratio', 1.0))
    all_samples: list[dict] = []
    for ch_name in list_chapter_dirs():
        for video, sub in scan_folder(HERE / ch_name):
            if not sub:
                continue
            cues = parse_srt(str(sub))
            if cues:
                merged_cues, _ = prepare_cues_for_dub(cues, ratio, prep_cfg)
                all_samples.extend(sample_long_cues_for_strategy(
                    merged_cues, ratio, prep_cfg, limit=50))

    if all_samples:
        prev = preview_strategy_on_samples(merged, all_samples)
        print(
            f'  策略试跑（长句样本 {prev["tried"]} 条）：'
            f'可压到限额内 {prev["fixed"]} 条，仍超长 {prev["still_long"]} 条')

    out = save_strategy(merged)
    print(f'  [OK] 已导入并保存：{out}')
    print(f'       drop_phrases {len(merged.get("drop_phrases", []))} 条，'
          f'replacements {len(merged.get("literal_replacements", []))} 条')
    if merged.get('notes'):
        print(f'       说明：{str(merged["notes"])[:120]}')
    return True


def run_configure_settings() -> None:
    """打开配音与 Gemini 设置弹窗，保存后退出（不进入批量配音）。"""
    print('=' * 50)
    print('  配音与 Gemini 设置')
    print('=' * 50)
    print('  正在打开设置窗口…')
    print('  （若看不到窗口，请检查任务栏）', flush=True)
    data = ask_dub_options(load_secrets())
    if data is None:
        print('  已取消')
        return
    _print_saved_dub_settings(data)


def _print_saved_dub_settings(data: dict) -> None:
    partial = bool(data.get('gemini_only_problem_cues', True))
    scope = '仅问题句' if partial else '全部中文组'
    print(f'  [OK] 已保存到 {SECRETS_PATH.name}')
    print(f'       优化字幕：{"开" if data.get("optimize_subtitle") else "关"}')
    print(f'       Speech Activity：{"开" if data.get("speech_activity_align") else "关"}')
    print(f'       Gemini 范围：{scope}')
    print(f'       模型：{data.get("gemini_model") or resolve_gemini_model(data, CFG)}')
    print(f'       API Key：{"已从环境变量读取" if data.get("gemini_api_key") else "未设置 GEMINI_API_KEY"}')
    print('  下一步：运行「批量处理课程.bat」开始配音')


def run_configure_api_keys() -> None:
    """仅打开 Gemini 配置弹窗，保存后退出（不进入批量配音）。"""
    print('=' * 50)
    print('  Gemini API 配置')
    print('=' * 50)
    print('  正在打开配置窗口…')
    print('  （若看不到窗口，请检查任务栏或被其他窗口挡住）', flush=True)
    existing = load_secrets()
    data = ask_api_keys_dialog(existing)
    if data is None:
        print('  已取消')
        return
    if data:
        save_secrets(data)
        model = data.get('gemini_model') or resolve_gemini_model(data, CFG)
        has_key = bool(data.get('gemini_api_key'))
        print(f'  [OK] 已保存到 {SECRETS_PATH.name}')
        print(f'       模型：{model}')
        print(f'       API Key：{"已填写" if has_key else "未填写"}')
    else:
        print('  已跳过（未保存）')


def main():
    args = parse_args()
    if args.verify_requirements:
        from pipeline_requirements_check import verify_all_requirements
        report = verify_all_requirements(verbose=True)
        raise SystemExit(0 if report.ok else 1)
    if args.import_zh_strategy:
        run_import_zh_strategy(args.import_zh_strategy)
        return
    if args.build_zh_strategy:
        folder_filter = None
        if args.folders.strip():
            chapters = list_chapter_dirs()
            folder_filter = resolve_folder_names(args.folders, chapters)
        run_build_zh_strategy(folder_filter)
        return
    if args.api_keys:
        run_configure_api_keys()
        return
    if args.config:
        run_configure_settings()
        return
    if args.cue_workers > 0:
        CFG['cue_workers'] = args.cue_workers
    if args.video_workers > 0:
        CFG['video_workers'] = args.video_workers

    product_mode = bool(args.work_dir.strip())
    work_dir: Path | None = None
    if product_mode:
        work_dir = Path(args.work_dir.strip()).resolve()
        if not work_dir.is_dir():
            print(f'  [错误] 文件夹不存在：{work_dir}')
            return

    print('=' * 50)
    if product_mode:
        print('  商用单文件夹验证（Gemini API 完整管线）')
        print(f'  工作文件夹：{work_dir}')
        print('  字幕：*bilingual*.srt；若有 *.fit-dub.srt 优先')
        print('  入口：commercial_dub_test.bat')
    else:
        print('  商用多章节批量（Gemini API 完整管线）')
        print(f'  扫描目录：{HERE}')
        print('  入口：commercial_dub_batch.bat')
        print('  学习实验（NotebookLM 直配）请用 quick_bilingual_dub.bat')
    print('=' * 50)

    voice = CFG.get('voice', '晓晓')
    rate = CFG.get('rate', '+0%')
    orig_vol = str(CFG.get('original_vol', 0.15))
    dub_vol = str(CFG.get('dub_vol', 3.0))

    engine = engine_mode()
    print(f'  配音员：{voice}  语速：{rate}')
    print(f'  引擎：{CFG.get("engine", "kokoro")}（以本目录 tubeget.config.json 为准）')
    if engine == 'kokoro':
        if not check_kokoro_ready():
            return
        print(f'  Kokoro 音色：{get_kokoro_voice_id()}（男声，非 Windows 志伟/慧慧）')
    elif engine == 'edge+kokoro':
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            print('  [错误] 混合模式需要 edge-tts：py -3.12 -m pip install edge-tts')
            return
        if not check_kokoro_ready():
            print('  [警告] Kokoro 未就绪，Edge 失败句将无法本地补全')
        tc_pre = effective_tts_concurrency()
        print(f'  Edge：{edge_rounds_count()} 轮 × {edge_timeout_sec():g}s 超时，并发 {tc_pre}')
        print(f'  Kokoro：仅补 Edge {edge_rounds_count()} 轮后仍失败的句子')

    cw = effective_cue_workers()
    vw = effective_video_workers()
    tc = effective_tts_concurrency()
    print(f'  并行：单视频 {cw} 进程分段合成，同时 {vw} 个视频', end='')
    if engine == 'edge+kokoro':
        print(f'，Edge 并发 {tc}')
    else:
        print()
    if engine == 'kokoro' and cw == 1:
        print('  内存模式：单进程 Kokoro（16GB 推荐，慢但稳）')

    # 沿用 tubeget.secrets.json 中的设置（请先运行 配置.bat）
    aic_main = ai_settings()
    sa_on = bool(aic_main.get('speech_activity_align', True))
    gem_scope = '仅问题句' if aic_main['gemini_only_problem_cues'] else '全部中文组'
    if aic_main['optimize_subtitle'] or sa_on:
        if aic_main['ai_provider'] == 'platform' and not aic_main['platform_active']:
            print('  配音前 Gemini：平台通道未开通 → 不会调用 API')
        elif not aic_main['gemini_key']:
            print('  配音前 Gemini：已开启但未提供 API → 不会调用')
            print('           请先运行：配置.bat')
        else:
            chan = '自填 API' if aic_main['ai_provider'] == 'self' else '软件平台'
            print(
                f'  配音前 Gemini：开（{chan}，{aic_main["gemini_model"]}，'
                f'范围={gem_scope}）')
            print(f'       优化字幕={"开" if aic_main["optimize_subtitle"] else "关"}  '
                  f'Speech Activity={"开" if sa_on else "关"}')
    else:
        print('  配音前 Gemini：关（免费通路）')
        print('           要改选项请运行：配置.bat')

    # 清理文件名中的表情符号
    print('\n  检查文件名...')
    if product_mode and work_dir:
        sanitize_filenames(work_dir)
    else:
        sanitize_filenames(HERE)
        subdirs_pre = [d for d in HERE.iterdir() if d.is_dir()
                       and not d.name.startswith('.') and not d.name.startswith('_')
                       and d.name != '__pycache__']
        for sd in subdirs_pre:
            sanitize_filenames(sd)
    print('  文件名检查完成。')

    if product_mode and work_dir:
        all_pairs = collect_work_dir_pairs(work_dir)
    else:
        chapters = list_chapter_dirs()
        folder_filter = None

        if args.folders.strip():
            folder_filter = resolve_folder_names(args.folders, chapters)
            if folder_filter is not None and not folder_filter:
                print('  未匹配到任何章节文件夹，退出')
                return
        elif not args.no_dialog:
            if args.gui:
                choice = ask_select_folders(chapters)
            else:
                choice = ask_select_folders_console(chapters)
            if choice is None:
                print('  已取消')
                return
            if choice == '':
                folder_filter = None
                print('  将处理：全部章节文件夹')
            else:
                folder_filter = resolve_folder_names(choice, chapters)
                if folder_filter is not None and not folder_filter:
                    print('  未匹配到任何章节文件夹，退出')
                    return
                print(f'  将处理 {len(folder_filter)} 个文件夹：')
                for fn in folder_filter:
                    print(f'    - {fn}')
        else:
            print('  将处理：全部章节文件夹（--no-dialog）')

        all_pairs = collect_all_pairs(folder_filter)

    if not all_pairs:
        print('\n  未找到视频文件（当前目录和子文件夹均无）')
        return

    # 按文件夹分组显示
    folders_seen = []
    for folder_name, video, sub in all_pairs:
        if folder_name not in folders_seen:
            folders_seen.append(folder_name)
    folders_seen.sort(key=natural_sort_key)
    print(f'\n  本次处理 {len(all_pairs)} 个视频，{len(folders_seen)} 个文件夹：')
    for fn in folders_seen:
        count = sum(1 for f, _, _ in all_pairs if f == fn)
        print(f'    {fn}/ ({count} 个视频)')

    success_count = 0
    fail_count = 0
    vw = effective_video_workers()
    batch_video_rows: list[dict] = []

    pending = []
    for folder_name, video, subtitle in all_pairs:
        if not subtitle:
            print(f'  [跳过] 未找到 bilingual 字幕：{video.name}')
            fail_count += 1
            continue
        pending.append((folder_name, video, subtitle))

    if vw <= 1 or len(pending) <= 1:
        for folder_name, video, subtitle in pending:
            print(f'\n{"─" * 50}')
            print(f'  文件夹：{folder_name}/')
            ok = asyncio.run(
                process_one(video, subtitle, voice, rate, orig_vol, dub_vol))
            batch_video_rows.append({
                'folder': folder_name,
                'video': video.name,
                'ok': ok,
                'gemini_run': str(gemini_run_txt_path(video)),
                'dub_run': str(video.parent / f'{video.stem}.dub-run.txt'),
            })
            if ok:
                success_count += 1
            else:
                fail_count += 1
    else:
        print(f'\n  视频级多进程：{vw} 路并行，共 {len(pending)} 个视频')
        jobs = [{
            'folder': folder_name,
            'video': str(video),
            'subtitle': str(subtitle),
            'video_name': video.name,
            'voice': voice,
            'rate': rate,
            'orig_vol': orig_vol,
            'dub_vol': dub_vol,
        } for folder_name, video, subtitle in pending]

        with ProcessPoolExecutor(max_workers=vw) as pool:
            futures = [pool.submit(_process_video_worker, j) for j in jobs]
            for fut in as_completed(futures):
                r = fut.result()
                tag = 'OK' if r['ok'] else '失败'
                err = f"  ({r['error']})" if r.get('error') else ''
                print(f'  [{tag}] {r["folder"]}/{r["video"]}{err}')
                vpath = Path(jobs[0]['video']).parent / r['video'] if jobs else Path('.')
                for j in jobs:
                    if j['video_name'] == r['video'] and j['folder'] == r['folder']:
                        vpath = Path(j['video'])
                        break
                batch_video_rows.append({
                    'folder': r['folder'],
                    'video': r['video'],
                    'ok': r['ok'],
                    'gemini_run': str(gemini_run_txt_path(vpath)),
                    'dub_run': str(vpath.parent / f'{vpath.stem}.dub-run.txt'),
                })
                if r['ok']:
                    success_count += 1
                else:
                    fail_count += 1

    batch_log = save_batch_run_index(
        batch_video_rows, success=success_count, fail=fail_count)

    print(f'\n{"=" * 50}')
    print(f'  全部完成！成功：{success_count}  失败/跳过：{fail_count}')
    print('  输出文件：各子文件夹中的 *_C / *_C2 / *_C3 … .mp4（自动递增，旧版保留）')
    print(f'  记事本日志：{batch_log}')
    print(f'              各视频目录 *_C.gemini-run.txt / *.dub-run.txt')
    try:
        from gemini_usage_tracker import print_usage_footer
        print_usage_footer()
    except ImportError:
        pass
    try:
        from pipeline_requirements_check import print_bat_guide
        hint = '商用单文件夹验证' if product_mode else '商用多章节批量'
        print_bat_guide(highlight=hint)
    except ImportError:
        pass
    print('=' * 50)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
