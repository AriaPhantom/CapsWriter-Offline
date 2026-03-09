# coding: utf-8
"""
快捷键任务模块

管理单个快捷键的录音任务状态
"""

import asyncio
import time
from threading import Event
from typing import TYPE_CHECKING, Optional

from . import logger
from util.tools.my_status import Status

if TYPE_CHECKING:
    from util.client.shortcut.shortcut_config import Shortcut
    from util.client.state import ClientState
    from util.client.audio.recorder import AudioRecorder



class ShortcutTask:
    """
    单个快捷键的录音任务

    跟踪每个快捷键独立的录音状态，防止互相干扰。
    """

    def __init__(self, shortcut: 'Shortcut', state: 'ClientState', recorder_class=None):
        """
        初始化快捷键任务

        Args:
            shortcut: 快捷键配置
            state: 客户端状态实例
            recorder_class: AudioRecorder 类（可选，用于延迟导入）
        """
        self.shortcut = shortcut
        self.state = state
        self._recorder_class = recorder_class

        # 任务状态
        self.task: Optional[asyncio.Future] = None
        self.recording_start_time: float = 0.0
        self.is_recording: bool = False

        # hold_mode 状态跟踪
        self.pressed: bool = False
        self.released: bool = True
        self.event: Event = Event()

        # 线程池（用于 countdown）
        self.pool = None

        # 录音状态动画
        self._status = Status('开始录音', spinner='point')

    def _queue_event(self, event_type: str, timestamp: float) -> None:
        """向音频队列提交 begin/finish 事件，并记录异步异常。"""
        if not self.state.loop or not self.state.queue_in:
            raise RuntimeError("事件循环或音频队列尚未初始化")

        future = asyncio.run_coroutine_threadsafe(
            self.state.queue_in.put({'type': event_type, 'time': timestamp, 'data': None}),
            self.state.loop,
        )

        def _on_done(done_future):
            if done_future.cancelled():
                logger.debug(f"[{self.shortcut.key}] queue<{event_type}> 已取消")
                return
            try:
                done_future.result()
                logger.debug(f"[{self.shortcut.key}] queue<{event_type}> 入队完成")
            except Exception as e:
                logger.error(f"[{self.shortcut.key}] queue<{event_type}> 入队失败: {e}", exc_info=True)

        future.add_done_callback(_on_done)

    def _on_recorder_done(self, done_future) -> None:
        """录音协程完成回调，避免异常静默吞掉。"""
        self.task = None
        if done_future.cancelled():
            logger.debug(f"[{self.shortcut.key}] 录音协程已取消")
            return
        try:
            done_future.result()
        except Exception as e:
            logger.error(f"[{self.shortcut.key}] 录音协程异常退出: {e}", exc_info=True)

    def _safe_stop_status(self) -> None:
        """安全停止状态动画，避免异常传播到监听线程。"""
        try:
            self._status.stop()
        except Exception as e:
            logger.debug(f"[{self.shortcut.key}] 停止状态动画失败（已忽略）: {e}")

    def _get_recorder(self) -> 'AudioRecorder':
        """获取 AudioRecorder 实例"""
        if self._recorder_class is None:
            from util.client.audio.recorder import AudioRecorder
            self._recorder_class = AudioRecorder
        return self._recorder_class(self.state)

    def launch(self) -> None:
        """启动录音任务"""
        logger.info(f"[{self.shortcut.key}] 触发：开始录音")

        try:
            # 记录开始时间
            self.recording_start_time = time.time()
            self.is_recording = True

            # 将开始标志放入队列
            self._queue_event('begin', self.recording_start_time)

            # 更新录音状态
            self.state.start_recording(self.recording_start_time)

            # 打印动画：正在录音
            self._status.start()

            # 启动识别任务
            recorder = self._get_recorder()
            self.task = asyncio.run_coroutine_threadsafe(
                recorder.record_and_send(),
                self.state.loop,
            )
            self.task.add_done_callback(self._on_recorder_done)
        except Exception as e:
            logger.error(f"[{self.shortcut.key}] 启动录音失败: {e}", exc_info=True)
            self.is_recording = False
            self.state.stop_recording()
            self._safe_stop_status()
            self.task = None

    def cancel(self) -> None:
        """取消录音任务（时间过短）"""
        logger.debug(f"[{self.shortcut.key}] 取消录音任务（时间过短）")

        self.is_recording = False
        self.state.stop_recording()
        self._safe_stop_status()

        if self.task is not None:
            self.task.cancel()
        self.task = None

    def finish(self) -> None:
        """完成录音任务"""
        logger.info(f"[{self.shortcut.key}] 释放：完成录音")

        try:
            self.is_recording = False
            self.state.stop_recording()
            logger.debug(f"[{self.shortcut.key}] finish: 录音状态已停止")

            self._queue_event('finish', time.time())
            logger.debug(f"[{self.shortcut.key}] finish: 结束事件已入队")
        except Exception as e:
            logger.error(f"[{self.shortcut.key}] 完成录音时发生异常: {e}", exc_info=True)
        finally:
            self._safe_stop_status()

        # 执行 restore（可恢复按键 + 非阻塞模式）
        # 阻塞模式下按键不会发送到系统，状态不会改变，不需要恢复
        if self.shortcut.is_toggle_key() and not self.shortcut.suppress:
            self._restore_key()

    def _restore_key(self) -> None:
        """恢复按键状态（防自捕获逻辑由 ShortcutManager 处理）"""
        # 通知管理器执行 restore
        # 防自捕获：管理器会设置 flag 再发送按键
        manager = self._manager_ref()
        if manager:
            logger.debug(f"[{self.shortcut.key}] 自动恢复按键状态 (suppress={self.shortcut.suppress})")
            manager.schedule_restore(self.shortcut.key)
        else:
            logger.warning(f"[{self.shortcut.key}] manager 引用丢失，无法 restore")
