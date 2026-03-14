# coding: utf-8
"""
音频流管理模块

提供 AudioStreamManager 类用于管理音频输入流，包括流的创建、
启动、停止和设备检测。
"""

from __future__ import annotations

import sys
import time
import threading
from typing import TYPE_CHECKING, Optional

import numpy as np
import sounddevice as sd

from util.client.state import console, get_state
from . import logger
from util.common.lifecycle import lifecycle

if TYPE_CHECKING:
    from util.client.state import ClientState



class AudioStreamManager:
    """
    音频流管理器
    
    负责管理音频输入流的生命周期，包括：
    - 检测和选择音频设备
    - 创建和启动音频流
    - 处理音频数据回调
    - 流的重启和关闭
    
    Attributes:
        state: 客户端状态实例
        sample_rate: 采样率（默认 48000Hz）
        block_duration: 每个数据块的时长（秒，默认 0.05s）
    """
    
    SAMPLE_RATE = 48000
    BLOCK_DURATION = 0.05  # 50ms
    DEVICE_POLL_INTERVAL = 1.5
    
    def __init__(self, state: 'ClientState'):
        """
        初始化音频流管理器
        
        Args:
            state: 客户端状态实例
        """
        self.state = state
        self._channels = 1
        self._device_index = None
        self._running = False  # 标志是否应该运行
        self._reopen_lock = threading.Lock()
        self._reopen_pending = False
        self._device_watch_stop = threading.Event()
        self._device_watch_thread: Optional[threading.Thread] = None
        self._last_default_input = self._get_default_input_signature()
        self._last_device_snapshot = self._get_input_device_snapshot()

    def _get_default_input_signature(self):
        """返回当前默认输入设备签名，用于监听系统默认麦克风变化。"""
        try:
            default_device = sd.default.device
            device_index = None
            if isinstance(default_device, (list, tuple)) and default_device:
                device_index = default_device[0]
            elif isinstance(default_device, int):
                device_index = default_device

            if device_index in (-1, None):
                return None

            device = sd.query_devices(device_index)
            return (
                int(device_index),
                device.get('name', ''),
                int(device.get('max_input_channels', 0)),
            )
        except Exception:
            return None

    def _get_input_device_snapshot(self):
        """返回当前输入设备列表快照，用于监听设备热插拔变化。"""
        try:
            devices = sd.query_devices()
        except Exception:
            return None

        snapshot = []
        for index, device in enumerate(devices):
            if device.get('max_input_channels', 0) > 0:
                snapshot.append(
                    (
                        int(index),
                        device.get('name', ''),
                        int(device.get('max_input_channels', 0)),
                    )
                )
        return tuple(snapshot)

    def _ensure_device_watcher(self) -> None:
        if self._device_watch_thread and self._device_watch_thread.is_alive():
            return

        self._device_watch_stop.clear()

        def worker() -> None:
            while not self._device_watch_stop.wait(self.DEVICE_POLL_INTERVAL):
                if lifecycle.is_shutting_down:
                    return

                current_default = self._get_default_input_signature()
                previous_default = self._last_default_input
                current_snapshot = self._get_input_device_snapshot()
                snapshot_changed = current_snapshot != self._last_device_snapshot
                default_changed = current_default != previous_default
                if not default_changed and not snapshot_changed:
                    continue

                self._last_default_input = current_default
                self._last_device_snapshot = current_snapshot
                reasons = []
                if default_changed:
                    reasons.append("默认输入设备变化")
                if snapshot_changed:
                    reasons.append("输入设备列表变化")
                reason_text = " / ".join(reasons)
                logger.info(
                    f"检测到{reason_text}: {previous_default} -> {current_default}，准备重启音频流"
                )
                self._schedule_reopen()

        self._device_watch_thread = threading.Thread(target=worker, daemon=True)
        self._device_watch_thread.start()

    def _resolve_input_device(self):
        """解析输入设备；默认设备不可用时，回退到第一个可用输入设备。"""
        try:
            device = sd.query_devices(kind='input')
            default_device = sd.default.device
            device_index = None
            if isinstance(default_device, (list, tuple)) and default_device:
                device_index = default_device[0]
                if device_index == -1:
                    device_index = None
            return device_index, device
        except sd.PortAudioError as default_error:
            try:
                devices = sd.query_devices()
            except Exception:
                raise default_error

            candidates = []
            for index, candidate in enumerate(devices):
                if candidate.get('max_input_channels', 0) > 0:
                    name = candidate.get('name', '未知设备')
                    lower_name = name.lower()
                    score = 0

                    preferred_tokens = ('麦克风', 'microphone', 'mic', 'headset', 'realtek', 'usb')
                    virtual_tokens = (
                        'sonic studio',
                        'virtual',
                        'vad',
                        'wave speaker',
                        'stereo mix',
                        'what u hear',
                        'loopback',
                        'mix',
                        'output',
                        'speaker',
                    )

                    if any(token in lower_name for token in preferred_tokens):
                        score -= 10
                    if any(token in lower_name for token in virtual_tokens):
                        score += 100

                    candidates.append((score, index, candidate))

            if candidates:
                _, index, candidate = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
                logger.warning(
                    f"默认输入设备不可用，回退到输入设备 #{index}: {candidate.get('name', '未知设备')}"
                )
                return index, candidate

            raise default_error
    
    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info,
        status: sd.CallbackFlags
    ) -> None:
        """
        音频数据回调函数
        
        当音频流接收到新数据时调用，将数据放入异步队列中。
        """
        # 只在录音状态时处理数据
        if not self.state.recording:
            return
        
        import asyncio
        
        # 将数据放入队列
        if self.state.loop and self.state.queue_in:
            asyncio.run_coroutine_threadsafe(
                self.state.queue_in.put({
                    'type': 'data',
                    'time': time.time(),
                    'data': indata.copy(),
                }),
                self.state.loop
            )
    
    def _on_stream_finished(self) -> None:
        """音频流结束回调"""
        if not threading.main_thread().is_alive():
            return
        
        # 只有在应该运行且不是手动停止、且系统未处于关闭状态的情况下才重启
        if self._running and not lifecycle.is_shutting_down:
            logger.info("音频流意外结束，正在尝试重启...")
            self._schedule_reopen()
        else:
            logger.debug("音频流已正常结束")

    def _schedule_reopen(self) -> None:
        """异步调度音频流重启，避免在 PortAudio/CFFI 回调线程里直接重开。"""
        with self._reopen_lock:
            if self._reopen_pending:
                logger.debug("音频流重启任务已在进行中，跳过重复调度")
                return
            self._reopen_pending = True

        def worker() -> None:
            try:
                time.sleep(0.2)
                self.reopen()
            finally:
                with self._reopen_lock:
                    self._reopen_pending = False

        threading.Thread(target=worker, daemon=True).start()
    
    def open(self) -> Optional[sd.InputStream]:
        """
        打开音频流
        
        Returns:
            创建的音频输入流，如果失败返回 None
        """
        self._ensure_device_watcher()

        # 检测音频设备
        try:
            self._device_index, device = self._resolve_input_device()
            self._channels = min(2, device['max_input_channels'])
            device_name = device.get('name', '未知设备')
            self._last_default_input = self._get_default_input_signature()
            self._last_device_snapshot = self._get_input_device_snapshot()
            console.print(
                f'使用输入设备：[italic]{device_name}，声道数：{self._channels}',
                end='\n\n'
            )
            logger.info(f"找到音频设备: {device_name}, 声道数: {self._channels}")
        except UnicodeDecodeError:
            console.print(
                "由于编码问题，暂时无法获得麦克风设备名字",
                end='\n\n',
                style='bright_red'
            )
            logger.warning("无法获取音频设备名称（编码问题）")
        except sd.PortAudioError:
            console.print("没有找到麦克风设备", end='\n\n', style='bright_red')
            logger.error("未找到麦克风设备")
            self.state.stream = None
            self._running = False
            return None
        
        # 创建音频流
        try:
            stream = sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                blocksize=int(self.BLOCK_DURATION * self.SAMPLE_RATE),
                device=self._device_index,
                dtype="float32",
                channels=self._channels,
                callback=self._audio_callback,
                finished_callback=self._on_stream_finished,
            )
            stream.start()
            
            self.state.stream = stream
            self._running = True
            logger.debug(
                f"音频流已启动: 采样率={self.SAMPLE_RATE}, "
                f"块大小={int(self.BLOCK_DURATION * self.SAMPLE_RATE)}"
            )
            return stream
            
        except Exception as e:
            logger.error(f"创建音频流失败: {e}", exc_info=True)
            return None
    
    def close(self) -> None:
        """关闭音频流"""
        self._running = False  # 标记为停止
        if self.state.stream is not None:
            try:
                self.state.stream.close()
                logger.debug("音频流已关闭")
            except Exception as e:
                logger.debug(f"关闭音频流时发生错误: {e}")
            finally:
                self.state.stream = None

    def shutdown(self) -> None:
        """彻底停止音频流管理，包括默认设备监听线程。"""
        self._device_watch_stop.set()
        self.close()
    
    def reopen(self) -> Optional[sd.InputStream]:
        """
        重新打开音频流
        
        Returns:
            新创建的音频输入流
        """
        logger.info("正在重启音频流...")
        
        # 关闭旧流
        self.close()
        
        # 重载 PortAudio，更新设备列表
        try:
            sd._terminate()
            sd._ffi.dlclose(sd._lib)
            sd._lib = sd._ffi.dlopen(sd._libname)
            sd._initialize()
        except Exception as e:
            logger.warning(f"重载 PortAudio 时发生警告: {e}")
        
        # 等待设备稳定
        time.sleep(0.1)
        
        # 打开新流
        return self.open()
