#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对照用户需求的 N 项自检：改完 Gemini/Chunk/配音相关代码后运行，
确认每项需求在代码与配置中均有体现，并提示应使用的 .bat。
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HERE = Path(__file__).parent.resolve()
ROOT = HERE.parent


@dataclass
class ReqResult:
    id: str
    title: str
    ok: bool
    detail: str = ''


@dataclass
class VerifyReport:
    results: list[ReqResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    @property
    def ok(self) -> bool:
        return self.failed == 0


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except OSError:
        return ''


def _load_config() -> dict:
    p = HERE / 'tubeget.config.json'
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}


def _fn_body_contains(module_path: Path, fn_name: str, *needles: str) -> bool:
    src = _read(module_path)
    if not src:
        return False
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            seg = ast.get_source_segment(src, node) or ''
            return all(n in seg for n in needles)
    return False


def _check_chunk_semantic_merge(_: dict) -> ReqResult:
    rid, title = 'chunk_gap_merge', 'Chunk：间隙<2s 合并语义块再送 Gemini'
    try:
        from gemini_chunk_translate import build_semantic_chunks
        cues = [
            {'start': 0.0, 'end': 1.0, 'text': 'Hello\n你好'},
            {'start': 1.5, 'end': 2.5, 'text': 'World\n世界'},
            {'start': 5.0, 'end': 6.0, 'text': 'Next\n下一句'},
        ]
        chunks = build_semantic_chunks(cues, gap_sec=2.0, chars_per_sec=4.5)
        if len(chunks) != 2:
            return ReqResult(rid, title, False, f'期望 2 块，得 {len(chunks)} 块')
        if len(chunks[0].lines) != 2:
            return ReqResult(rid, title, False, '前两行应合并为一块')
        return ReqResult(rid, title, True, f'合并后 {len(chunks)} 块')
    except Exception as e:
        return ReqResult(rid, title, False, str(e))


def _check_max_chinese_chars(cfg: dict) -> ReqResult:
    rid, title = 'max_chinese_chars', 'Max_Chinese_Chars = floor(时长×4.5) 动态注入 Prompt'
    cps = float(cfg.get('chunk_chars_per_sec', 0))
    if abs(cps - 4.5) > 0.01:
        return ReqResult(rid, title, False, f'配置 chunk_chars_per_sec={cps}，期望 4.5')
    src = _read(HERE / 'gemini_chunk_translate.py')
    if '{{Max_Chinese_Chars}}' not in src:
        return ReqResult(rid, title, False, 'Prompt 缺少 {{Max_Chinese_Chars}}')
    if 'math.floor' not in src and 'floor(' not in src:
        return ReqResult(rid, title, False, '未找到 floor 字数计算')
    try:
        from gemini_chunk_translate import ChunkLine, SubtitleChunk
        ch = SubtitleChunk(chunk_id=0, lines=[
            ChunkLine(cue_index=0, seq=1, start=0.0, end=10.0, foreign='x')])
        n = ch.max_chinese_chars(4.5)
        if n != 45:
            return ReqResult(rid, title, False, f'10s×4.5 应得 45，得 {n}')
    except Exception as e:
        return ReqResult(rid, title, False, str(e))
    return ReqResult(rid, title, True, 'Prompt 占位符 + floor(时长×4.5) 已就绪')


def _check_chunk_timeline(cfg: dict) -> ReqResult:
    rid, title = 'chunk_keep_timeline', 'Chunk 输出保持原 SRT 时间轴后拼接送配音'
    src = _read(HERE / 'gemini_chunk_translate.py')
    if 'write_translated_zh_srt' not in src:
        return ReqResult(rid, title, False, '缺少 write_translated_zh_srt')
    course = _read(HERE / 'run_batch_course.py')
    if 'translate_cues_via_semantic_chunks' not in course:
        return ReqResult(rid, title, False, 'run_batch_course 未调用 Chunk 翻译')
    if 'chunk-zh.srt' not in course:
        return ReqResult(rid, title, False, '未写出 chunk-zh.srt')
    return ReqResult(rid, title, True, 'translate → chunk-zh.srt → TTS')


def _check_no_zh_in_api(_: dict) -> ReqResult:
    rid, title = 'no_zh_in_api', 'Gemini API 载荷不送已有中文（zh/text 等）'
    from gemini_preflight import audit_api_payload
    bad, errs = audit_api_payload(
        [{'index': 0, 'zh': '测试', 'en': 'hello'}],
        'source_translate', block_on_error=False)
    if not bad:
        return ReqResult(rid, title, False, 'preflight 未拦截含 zh 的载荷')
    rewrite_src = _read(HERE / 'ai_shorten.py')
    m = re.search(
        r'def _rewrite_batch_sync\([^)]*\).*?(?=\ndef |\Z)',
        rewrite_src, re.DOTALL)
    if m and re.search(r"['\"]zh['\"]\s*:", m.group(0)):
        return ReqResult(rid, title, False, '_rewrite_batch_sync 的 payload 仍含 zh')
    if '_gemini_audit_payload' not in rewrite_src:
        return ReqResult(rid, title, False, '缺少 _gemini_audit_payload')
    return ReqResult(rid, title, True, f'preflight 拦截：{errs[0][:40]}…')


def _check_cue_source_text(_: dict) -> ReqResult:
    rid, title = 'cue_source_text', 'cue_source_text 只提取原语言，不返回中文'
    from ai_shorten import cue_source_text
    zh_only = cue_source_text({'text': '这是纯中文'})
    if zh_only.strip():
        return ReqResult(rid, title, False, '纯中文 cue 应返回空')
    mixed = cue_source_text({'text': 'Hello\n你好'})
    if mixed != 'Hello':
        return ReqResult(rid, title, False, f'双语 cue 应只取 Hello，得 {mixed!r}')
    return ReqResult(rid, title, True, '原语言提取正确')


def _check_only_problem_strategy(cfg: dict) -> ReqResult:
    rid, title = 'only_problem_cues', '仅问题句 vs 全文原语言翻译（force_source_all）'
    if 'gemini_only_problem_cues' not in cfg:
        return ReqResult(rid, title, False, 'tubeget.config.json 缺少 gemini_only_problem_cues')
    src = _read(HERE / 'ai_shorten.py')
    for sym in ('assess_gemini_translate_strategy', 'force_source_all', 'gemini_target_kind'):
        if sym not in src:
            return ReqResult(rid, title, False, f'ai_shorten 缺少 {sym}')
    return ReqResult(rid, title, True, '策略函数 + 配置项齐全')


def _check_skip_zh_hans_chunk(_: dict) -> ReqResult:
    rid, title = 'skip_zh_hans_chunk', '纯 .zh-Hans/.zh-CN 字幕跳过 Chunk，走 rewrite'
    from gemini_chunk_translate import should_use_chunk_translate
    cfg = {'gemini_chunk_translate': True}
    if should_use_chunk_translate(cfg, subtitle_path='foo.zh-Hans.srt'):
        return ReqResult(rid, title, False, '.zh-Hans 仍启用 Chunk')
    if not should_use_chunk_translate(cfg, subtitle_path='foo.bilingual.srt'):
        return ReqResult(rid, title, False, 'bilingual.srt 应启用 Chunk')
    return ReqResult(rid, title, True, 'zh-Hans 跳过 / bilingual 启用')


def _check_preflight(cfg: dict) -> ReqResult:
    rid, title = 'gemini_preflight', '送优前 preflight 自检并可拦截 API'
    if not cfg.get('gemini_preflight', True):
        return ReqResult(rid, title, False, '配置 gemini_preflight=false')
    from gemini_preflight import preflight_rewrite_plan
    pf = preflight_rewrite_plan(
        [], {'translate_mode': 'skip', 'total_zh': 0, 'with_source': 0},
        cfg={'gemini_preflight_block': True})
    if not pf.get('blocked'):
        return ReqResult(rid, title, False, '空计划应被 blocked')
    integrated = (
        'preflight_rewrite_plan' in _read(HERE / 'ai_shorten.py')
        and 'preflight_chunk_translate' in _read(HERE / 'gemini_chunk_translate.py')
    )
    if not integrated:
        return ReqResult(rid, title, False, 'preflight 未集成到 rewrite/chunk')
    return ReqResult(rid, title, True, 'rewrite + chunk 均已挂 preflight')


def _check_chunk_control_flow(_: dict) -> ReqResult:
    rid, title = 'chunk_no_overwrite', 'Chunk 分支独立，不会被 rewrite 覆盖'
    src = _read(HERE / 'run_batch_course.py')
    idx_chunk = src.find('elif chunk_translate:')
    idx_rewrite = src.find('await rewrite_cues_with_gemini(')
    if idx_chunk < 0:
        return ReqResult(rid, title, False, '缺少 elif chunk_translate')
    if idx_rewrite < 0:
        return ReqResult(rid, title, False, '缺少 rewrite_cues_with_gemini')
    if idx_chunk > idx_rewrite:
        return ReqResult(rid, title, False, 'Chunk 分支在 rewrite 之后（会覆盖）')
    between = src[idx_chunk:idx_rewrite]
    if 'translate_cues_via_semantic_chunks' not in between:
        return ReqResult(rid, title, False, 'Chunk 分支未调用 translate_cues_via_semantic_chunks')
    return ReqResult(rid, title, True, 'elif chunk_translate → translate_cues… 先于 rewrite')


def _check_outputs(_: dict) -> ReqResult:
    rid, title = 'gemini_outputs', '产出 gemini-preflight / chunk-raw / gemini-run 日志'
    missing = []
    if 'save_preflight_report' not in _read(HERE / 'gemini_preflight.py'):
        missing.append('preflight.json')
    if 'gemini-chunk-raw' not in _read(HERE / 'run_batch_course.py'):
        missing.append('chunk-raw.srt')
    if not (HERE / 'gemini_log.py').is_file():
        missing.append('gemini_log.py')
    if missing:
        return ReqResult(rid, title, False, '缺少：' + ', '.join(missing))
    return ReqResult(rid, title, True, 'preflight + chunk-raw + gemini_log')


def _check_vad_after_chunk(_: dict) -> ReqResult:
    rid, title = 'vad_after_chunk', 'VAD 口播窗口在 Chunk 翻译之后应用（保留原行粒度）'
    src = _read(HERE / 'run_batch_course.py')
    if 'translate_cues_via_semantic_chunks' not in src:
        return ReqResult(rid, title, False, '无 Chunk 翻译调用')
    chunk_block = src.split('elif chunk_translate:', 1)
    if len(chunk_block) < 2:
        return ReqResult(rid, title, False, '缺少 elif chunk_translate 分支')
    tail = chunk_block[1].split('else:', 1)[0]
    if 'apply_speech_windows_to_cues' not in tail:
        return ReqResult(rid, title, False, 'Chunk 分支内未在翻译后应用 VAD 窗口')
    if "'merge_zh_sentences': False" not in src and '"merge_zh_sentences": False' not in src:
        return ReqResult(rid, title, False, 'Chunk 模式未关闭提前 merge')
    return ReqResult(rid, title, True, 'Chunk 后 apply_speech_windows + 不提前 merge')


def _check_source_lang(_: dict) -> ReqResult:
    rid, title = 'source_lang_ko_ja', '原语言识别含 en/ru/ko/ja（非仅「英文」）'
    from ai_shorten import detect_source_lang
    if detect_source_lang('안녕') != 'ko':
        return ReqResult(rid, title, False, '韩语未识别')
    if detect_source_lang('こんにちは') != 'ja':
        return ReqResult(rid, title, False, '日语未识别')
    chunk_prompt = _read(HERE / 'gemini_chunk_translate.py')
    if '原语言' not in chunk_prompt:
        return ReqResult(rid, title, False, 'Chunk 提示词未用「原语言」')
    return ReqResult(rid, title, True, 'ko/ja/ru + 原语言术语')


def _check_py312_bat(_: dict) -> ReqResult:
    rid, title = 'py312_bat', '入口 .bat 使用 py -3.12'
    bats = [
        ROOT / 'commercial_dub_test.bat',
        HERE / 'commercial_dub_batch.bat',
        HERE / 'config.bat',
    ]
    bad = [b.name for b in bats if b.is_file() and 'py -3.12' not in _read(b)]
    if bad:
        return ReqResult(rid, title, False, f'未用 py -3.12：{", ".join(bad)}')
    return ReqResult(rid, title, True, f'已检查 {len(bats)} 个 bat')


def _check_pipeline_split(_: dict) -> ReqResult:
    rid, title = 'pipeline_split', '产品测试 vs 课程管线分流正确'
    product = _read(ROOT / 'commercial_dub_test.bat')
    if 'run_batch_course.py' not in product and 'run_batch_course.py' not in _read(ROOT / 'product_dub_test.bat'):
        return ReqResult(rid, title, False, 'commercial_dub_test.bat must use run_batch_course.py')
    batch = _read(HERE / 'commercial_dub_batch.bat')
    if 'run_batch_course.py' not in batch and 'run_batch_course.py' not in _read(HERE / 'course_batch_dub.bat'):
        return ReqResult(rid, title, False, 'commercial_dub_batch.bat must use run_batch_course.py')
    if 'find_bilingual_sub' not in _read(HERE / 'run_batch_course.py'):
        return ReqResult(rid, title, False, '课程流程未要求 bilingual.srt')
    return ReqResult(rid, title, True, '产品→run_batch+zh-Hans / 课程→course+bilingual')


_CHECKS: list[Callable[[dict], ReqResult]] = [
    _check_chunk_semantic_merge,
    _check_max_chinese_chars,
    _check_chunk_timeline,
    _check_no_zh_in_api,
    _check_cue_source_text,
    _check_only_problem_strategy,
    _check_skip_zh_hans_chunk,
    _check_preflight,
    _check_chunk_control_flow,
    _check_outputs,
    _check_vad_after_chunk,
    _check_source_lang,
    _check_py312_bat,
    _check_pipeline_split,
]


def verify_all_requirements(*, verbose: bool = True) -> VerifyReport:
    cfg = _load_config()
    report = VerifyReport()
    for fn in _CHECKS:
        try:
            report.results.append(fn(cfg))
        except Exception as e:
            report.results.append(ReqResult(
                fn.__name__, fn.__name__, False, str(e)))
    if verbose:
        print_requirements_report(report)
        print_bat_guide()
    return report


def print_requirements_report(report: VerifyReport) -> None:
    print('')
    print('=' * 54)
    print('  用户需求对照自检（共 {} 项）'.format(len(report.results)))
    print('=' * 54)
    for i, r in enumerate(report.results, 1):
        mark = 'OK' if r.ok else 'FAIL'
        line = f'  [{mark}] {i:2d}. {r.title}'
        if r.detail:
            line += f' — {r.detail}'
        print(line)
    print('-' * 54)
    status = '全部就绪' if report.ok else f'{report.failed} 项未通过'
    print(f'  通过 {report.passed}/{len(report.results)}  [{status}]')
    print('=' * 54)


def print_bat_guide(*, highlight: str | None = None) -> None:
    """避免 bat 误用：明确各入口适用场景。"""
    lines = [
        '',
        '【两条路线 — 见 WORKFLOWS.txt】',
        '  路线 A 学习实验：quick_bilingual_dub.bat（NotebookLM + 直配）',
        '  路线 B 商用验证：',
        '      commercial_dub_test.bat  -> run_batch_course.py --work-dir',
        '      commercial_dub_batch.bat -> run_batch_course.py',
        '      fit_translate.bat        -> fit-dub.srt（Gemini 进框翻译）',
        '  · Settings: components\\config.bat',
        '  · 改代码后自检：py -3.12 components\\check_env.py',
    ]
    if highlight:
        lines.insert(2, f'  >>> 当前场景：{highlight}')
    print('\n'.join(lines))


def main() -> int:
    report = verify_all_requirements(verbose=True)
    return 0 if report.ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
