# coding: utf-8
"""
Toast 适配层

统一对外提供 Toast 能力，内部在两套实现之间选择：

- **外壳（Tauri）**：在线时优先使用。WebView2 渲染，流式增量追加，
  Markdown 一次性定稿，GPU 合成。
- **Tkinter**：外壳未启动时的回退实现，行为与历史版本完全一致。

这样做的意义是：外壳是纯增强。它没装、没启动、崩了，语音输入链路
都照常工作，只是回到原来的观感。

调用方只需用 `ToastSession`，不必关心底层是哪套。
"""

from __future__ import annotations

import uuid
from typing import Optional

from . import logger
from . import shell_bridge


class ToastSession:
    """
    一次 Toast 会话（创建 -> 流式更新 -> 定稿 -> 关闭）

    用法：
        s = ToastSession.open(streaming=True, role='翻译', bg='#075077')
        s.append('增量文本')      # 流式；注意传增量而非全量
        s.finish()               # 定稿，开始自动关闭倒计时
        s.close()                # 或者立即关闭
    """

    def __init__(self, msg_id: str, backend: str) -> None:
        self.msg_id = msg_id
        self.backend = backend          # 'shell' | 'tk'
        self._closed = False
        # Tk 回退时用于把增量还原成全量（旧接口只吃全量）
        self._accum = ''

    # ----------------------------------------------------
    # 创建
    # ----------------------------------------------------

    @classmethod
    def open(
        cls,
        text: str = '',
        *,
        streaming: bool = False,
        width: float = 0.5,
        height: int = 0,
        font_family: str = '',
        font_size: int = 14,
        fg: str = 'white',
        bg: str = '#075077',
        duration: int = 3000,
        editable: bool = False,
        markdown: bool = True,
        role: str = '',
        stop_callback=None,
    ) -> 'ToastSession':
        """开启一次 Toast 会话，自动选择后端"""

        if shell_bridge.use_shell():
            msg_id = str(uuid.uuid4())
            shell_bridge.toast_open(
                msg_id,
                text=text,
                streaming=streaming,
                width=width,
                height=height,
                font_family=font_family,
                font_size=font_size,
                fg=_normalize_color(fg),
                bg=_normalize_color(bg),
                duration=duration,
                editable=editable,
                markdown=markdown,
                role=role,
            )
            session = cls(msg_id, 'shell')
            session._accum = text
            return session

        # ---- 回退到 Tkinter ----
        from .toast_manager import ToastMessageManager, ToastMessage

        manager = ToastMessageManager()
        msg = ToastMessage(
            text=text,
            font_size=font_size,
            font_family=font_family,
            bg=bg,
            fg=fg,
            duration=duration,
            initial_width=width,
            initial_height=height,
            streaming=streaming,
            window_type='text',
            markdown=markdown,
            editable=editable,
            stop_callback=stop_callback,
        )
        msg_id = manager.add_message(msg) or str(uuid.uuid4())
        session = cls(msg_id, 'tk')
        session._accum = text
        return session

    # ----------------------------------------------------
    # 流式更新
    # ----------------------------------------------------

    def append(self, delta: str) -> None:
        """追加增量文本"""
        if self._closed or not delta:
            return
        self._accum += delta

        if self.backend == 'shell':
            shell_bridge.toast_append(self.msg_id, delta)
        else:
            # 旧接口只接受全量文本
            from .toast_manager import ToastMessageManager
            ToastMessageManager().update_toast(self.msg_id, self._accum)

    def finish(self) -> None:
        """流式结束，触发定稿与自动关闭"""
        if self._closed:
            return
        if self.backend == 'shell':
            shell_bridge.toast_finish(self.msg_id)
        else:
            from .toast_manager import ToastMessageManager
            ToastMessageManager().finish_toast(self.msg_id)

    def close(self) -> None:
        """立即关闭"""
        if self._closed:
            return
        self._closed = True
        if self.backend == 'shell':
            shell_bridge.toast_close(self.msg_id)
        else:
            from .toast_manager import ToastMessageManager
            ToastMessageManager().close_toast(self.msg_id)

    @property
    def text(self) -> str:
        """当前累积的完整文本"""
        return self._accum


# ============================================================
# 简单通知（一次性提示，不需要会话对象）
# ============================================================

def notify(
    message: str,
    *,
    level: str = 'info',
    duration: int = 3000,
    bg: str = '#075077',
    fg: str = 'white',
    font_size: int = 14,
    width: float = 0.5,
) -> None:
    """
    显示一条一次性提示。

    外壳在线时用轻量气泡（右上角），否则回退到 Tk Toast。
    """
    if shell_bridge.use_shell():
        shell_bridge.notify(message, level=level, duration=duration)
        return

    try:
        from .toast import toast
        toast(
            message,
            font_size=font_size,
            bg=bg,
            fg=fg,
            duration=duration,
            initial_width=width,
        )
    except Exception as e:
        logger.warning(f"显示提示失败: {e}")


# ============================================================
# 状态推送（仅外壳支持，Tk 侧无对应 UI，静默跳过）
# ============================================================

def recording_state(active: bool, elapsed: float = 0.0) -> None:
    """推送录音状态（用于浮层的听写指示动画）"""
    if shell_bridge.use_shell():
        shell_bridge.recording_state(active, elapsed)


def recognition(
    text: str,
    original: str = '',
    latency: float = 0.0,
    hotwords: Optional[list] = None,
) -> None:
    """推送识别摘要到 Dashboard"""
    if shell_bridge.use_shell():
        shell_bridge.recognition(text, original, latency, hotwords)


# ============================================================
# 工具
# ============================================================

_NAMED_COLORS = {
    'white': '#ffffff',
    'black': '#000000',
    'red': '#ff0000',
    'green': '#008000',
    'blue': '#0000ff',
    'yellow': '#ffff00',
    'gray': '#808080',
    'grey': '#808080',
    'cyan': '#00ffff',
    'magenta': '#ff00ff',
    'orange': '#ffa500',
}


def _normalize_color(c: str) -> str:
    """
    把 Tk 颜色名转成 CSS 十六进制。

    Tk 接受 'white' 这类名字，CSS 也大多接受，但角色配置里可能出现
    Tk 专有名（如 'gray50'），统一转换更稳。未知值原样透传。
    """
    if not c:
        return c
    key = c.strip().lower()
    return _NAMED_COLORS.get(key, c)
