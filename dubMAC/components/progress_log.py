"""终端阶段提示：长耗时步骤前/中输出，避免用户以为程序卡住。"""
from __future__ import annotations


def stage(msg: str) -> None:
    print(msg, flush=True)


def pipeline_step(
    step: int,
    total: int,
    msg: str,
    *,
    quiet: bool = False,
    milestone: bool = True,
) -> None:
    """关键流程节点：milestone 时即使 quiet 也输出（如多进程子任务）。"""
    if milestone or not quiet:
        print(f'  [步骤 {step}/{total}] {msg}', flush=True)


def progress_line(label: str, current: int, total: int, *, end: bool = False) -> None:
    """单行刷新进度（current/total）。"""
    print(f'\r    {label} {current}/{total}', end='\n' if end else '', flush=True)
