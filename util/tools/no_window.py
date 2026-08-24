# coding: utf-8
"""
无窗口子进程启动辅助

Windows 上用 `pythonw.exe` 或打包后的 GUI 程序启动子进程时，如果不显式传
`CREATE_NO_WINDOW`，每次 `Popen` 都会弹出一个黑色控制台窗口然后消失。

这在热路径上尤其扎眼：录音时用 ffmpeg 压缩音频是**每次按下快捷键**都会做的事，
于是每说一句话就闪一个黑窗。

用法：

    from util.tools.no_window import no_window_kwargs

    Popen(cmd, stdin=PIPE, **no_window_kwargs())

非 Windows 平台返回空字典，不影响行为。
"""

from __future__ import annotations

import sys
from typing import Any, Dict

# Windows CreateProcess 标志：不为子进程分配控制台
# 见 https://learn.microsoft.com/windows/win32/procthread/process-creation-flags
CREATE_NO_WINDOW = 0x0800_0000


def no_window_kwargs() -> Dict[str, Any]:
    """
    返回可直接展开给 subprocess 的 kwargs，用于抑制控制台窗口。

    Returns:
        Windows 上是 {'creationflags': CREATE_NO_WINDOW}，其它平台是 {}
    """
    if sys.platform == 'win32':
        return {'creationflags': CREATE_NO_WINDOW}
    return {}


def asyncio_no_window_kwargs() -> Dict[str, Any]:
    """
    `asyncio.create_subprocess_exec` 同样接受 creationflags。

    单独提供一个函数只是为了让调用处语义清晰；实现与同步版本一致。
    """
    return no_window_kwargs()
