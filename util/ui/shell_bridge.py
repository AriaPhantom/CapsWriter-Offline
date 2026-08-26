# coding: utf-8
"""
外壳桥接模块（Shell Bridge）

把 UI 事件推送给 Tauri 外壳（`shell/`）。设计原则：

1. **绝不阻塞调用方**：所有发送都进本地队列，由后台守护线程写 socket。
   即便外壳没启动、卡死或中途退出，识别与上屏链路都不受影响。
2. **可用性自动降级**：外壳不在线时 `is_available()` 返回 False，
   调用方回退到原有的 Tkinter 实现。所以外壳是「增强」，不是「依赖」。
3. **只发增量**：流式文本发送 delta 而非全量，避免旧实现里
   每 chunk 传全量再 diff 的 O(n²) 开销。

协议：本机 TCP `127.0.0.1:6020`，每条消息一行 JSON（NDJSON）。
"""

from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
from typing import Any, Dict, Optional

from . import logger

# 与 shell/src-tauri/src/bridge.rs 的 BRIDGE_PORT 保持一致
SHELL_HOST = '127.0.0.1'
SHELL_PORT = 6020

# 队列上限。外壳掉线时事件会堆积，超过就按策略丢弃，
# 避免内存无界增长（UI 事件过期即无价值）。
_MAX_QUEUE = 20000

# 单次 sendall 最多合并多少条消息。批量发送把 syscall 次数降两个数量级，
# 否则 LLM 高速吐字时生产端速度远超发送端，队列会溢出并丢字。
_SEND_BATCH = 256

# 文本类事件不可丢（丢了就是内容缺失），队列满时最多阻塞这么久
_CRITICAL_PUT_TIMEOUT = 2.0

# 这些事件类型是「状态快照」，丢了无所谓，下一次推送会覆盖
_DROPPABLE = frozenset({'heartbeat', 'recording_state', 'recognition'})

# 连接失败后的重试间隔（秒），避免外壳未启动时疯狂重连
_RECONNECT_INTERVAL = 3.0

# 心跳间隔（秒），让外壳知道 Python 客户端还活着
_HEARTBEAT_INTERVAL = 2.0


def _project_root() -> str:
    """
    本客户端所属的项目根目录（含 config_client.py 的那一级）。

    用于让外壳区分同机的多份 CapsWriter 安装。
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # util/ui -> util
    root = os.path.dirname(here)                                        # util -> 项目根
    try:
        return os.path.realpath(root)
    except OSError:
        return root


class _ShellBridge:
    """单例：维护到外壳的连接并异步发送事件"""

    def __init__(self) -> None:
        self._q: 'queue.Queue[Optional[str]]' = queue.Queue(maxsize=_MAX_QUEUE)
        self._sock: Optional[socket.socket] = None
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._last_attempt = 0.0
        self._lock = threading.Lock()
        self._dropped = 0

        self._sender = threading.Thread(
            target=self._sender_loop, daemon=True, name='ShellBridgeSender'
        )
        self._sender.start()

        self._hb = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name='ShellBridgeHeartbeat'
        )
        self._hb.start()

    # ----------------------------------------------------
    # 连接管理
    # ----------------------------------------------------

    def _try_connect(self) -> bool:
        """尝试连接外壳。失败不抛异常，只返回 False。"""
        now = time.monotonic()
        if now - self._last_attempt < _RECONNECT_INTERVAL:
            return False
        self._last_attempt = now

        try:
            s = socket.create_connection((SHELL_HOST, SHELL_PORT), timeout=0.6)
            s.settimeout(None)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self._sock = s
            self._connected.set()
            logger.info(f"已连接 UI 外壳 {SHELL_HOST}:{SHELL_PORT}")
            return True
        except OSError:
            # 外壳未启动是正常情况（用户可能只用命令行），不刷日志
            return False

    def _drop_connection(self) -> None:
        self._connected.clear()
        with self._lock:
            s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def is_available(self) -> bool:
        """外壳当前是否可用（决定是否回退到 Tk 实现）"""
        return self._connected.is_set()

    # ----------------------------------------------------
    # 发送
    # ----------------------------------------------------

    def send(self, payload: Dict[str, Any]) -> None:
        """
        把事件放入队列。

        丢弃策略按事件类型区分：
        - 文本类（toast_open/append/finish/close）：丢了就是内容缺失，
          队列满时短暂阻塞等待发送端跟上；真超时才丢并告警。
        - 状态类（heartbeat/recording_state/recognition）：本质是快照，
          丢了下次推送会覆盖，直接丢最旧的。
        """
        if self._stop.is_set():
            return
        try:
            line = json.dumps(payload, ensure_ascii=False) + '\n'
        except (TypeError, ValueError) as e:
            logger.warning(f"外壳事件序列化失败: {e}")
            return

        droppable = payload.get('type') in _DROPPABLE

        try:
            self._q.put_nowait(line)
            return
        except queue.Full:
            pass

        if droppable:
            # 状态类：丢最旧的一条再放入，保证新状态优先
            try:
                self._q.get_nowait()
                self._q.put_nowait(line)
            except (queue.Empty, queue.Full):
                pass
            return

        # 文本类：宁可让调用方稍等，也不能丢字。
        # 未连接时不阻塞——外壳不在线时本就该走 Tk 回退，
        # 阻塞只会拖慢识别链路。
        if not self._connected.is_set():
            self._note_drop()
            return
        try:
            self._q.put(line, timeout=_CRITICAL_PUT_TIMEOUT)
        except queue.Full:
            self._note_drop()

    def _note_drop(self) -> None:
        self._dropped += 1
        if self._dropped % 100 == 1:
            logger.warning(f"外壳事件队列积压，已丢弃 {self._dropped} 条")

    def _sender_loop(self) -> None:
        while not self._stop.is_set():
            # 未连接则先尝试连接
            if not self._connected.is_set():
                if not self._try_connect():
                    # 连不上就等一会儿，同时把过期事件清掉
                    time.sleep(0.25)
                    self._drain_stale()
                    continue

            try:
                line = self._q.get(timeout=0.3)
            except queue.Empty:
                continue

            if line is None:  # 停止哨兵
                break

            # 批量抽干：一次 sendall 发送多条，而不是每条一次 syscall。
            # LLM 高速吐字时生产端可达 20 万条/秒，逐条发送会让队列溢出丢字。
            batch = [line]
            stop_sentinel = False
            while len(batch) < _SEND_BATCH:
                try:
                    nxt = self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    stop_sentinel = True
                    break
                batch.append(nxt)

            with self._lock:
                s = self._sock
            if s is None:
                continue

            try:
                s.sendall(''.join(batch).encode('utf-8'))
            except OSError as e:
                logger.info(f"外壳连接断开，将重连: {e}")
                self._drop_connection()

            if stop_sentinel:
                break

    def _drain_stale(self) -> None:
        """外壳离线期间清空队列。UI 事件过期无价值，留着只会在
        重连瞬间灌一堆陈旧内容。"""
        n = 0
        while True:
            try:
                self._q.get_nowait()
                n += 1
            except queue.Empty:
                break

    def _heartbeat_loop(self) -> None:
        pid = os.getpid()
        # 带上自己的安装目录：同一台机器可能装了多份 CapsWriter，
        # 外壳据此确认心跳确实来自它所管的那一份。
        root = _project_root()
        while not self._stop.is_set():
            if self._connected.is_set():
                self.send({'type': 'heartbeat', 'pid': pid, 'root': root})
            time.sleep(_HEARTBEAT_INTERVAL)

    def close(self) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        self._drop_connection()


# ============================================================
# 单例与公共 API
# ============================================================

_bridge: Optional[_ShellBridge] = None
_bridge_lock = threading.Lock()


def get_bridge() -> _ShellBridge:
    global _bridge
    if _bridge is None:
        with _bridge_lock:
            if _bridge is None:
                _bridge = _ShellBridge()
    return _bridge


def shell_available() -> bool:
    """外壳是否在线。调用方据此决定用外壳还是回退 Tk。"""
    try:
        return get_bridge().is_available()
    except Exception:
        return False


def shell_enabled() -> bool:
    """
    是否启用外壳 UI。

    读 `ClientConfig.use_shell_ui`；没有该配置项时默认 True
    （外壳不在线会自动回退，所以默认开启是安全的）。
    """
    try:
        from config_client import ClientConfig as Config
        return bool(getattr(Config, 'use_shell_ui', True))
    except Exception:
        return True


def use_shell() -> bool:
    """综合判断：既启用又在线，才走外壳"""
    return shell_enabled() and shell_available()


def start_bridge(wait: float = 0.0) -> bool:
    """
    预先建立到外壳的连接。**应在客户端启动阶段调用一次。**

    为什么需要这一步：`use_shell()` 要求「已经连上」，而桥是懒加载的
    —— 第一次推送事件时才创建单例并开始连。两件事撞在同一毫秒里，
    于是**第一次按住快捷键必定被判为「外壳不在线」而丢弃**，浮层不亮。

    这在开机场景下尤其明显：自启动脚本先拉客户端，外壳可能还没监听，
    错过的不只是第一次 —— `_try_connect` 有 3 秒退避，`_sender_loop`
    只在有事件时才推进，用户的观感就是「重启后横幅彻底没了」。

    Args:
        wait: 最多等待多少秒直到连上。0 表示只触发不等待。
              启动阶段给一个小值（如 1.5s）即可，连不上也不影响主链路。

    Returns:
        bool: 返回时是否已连上（未启用外壳时恒为 False）
    """
    if not shell_enabled():
        return False

    bridge = get_bridge()      # 创建单例，sender 线程随即开始尝试连接

    if wait > 0:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if bridge.is_available():
                break
            time.sleep(0.05)

    ok = bridge.is_available()
    if ok:
        logger.info("外壳桥已就绪")
    else:
        # 不是错误：用户可能就没开外壳。后续会自动重连，届时浮层自然可用。
        logger.info("外壳暂未连上，将在后台持续重试（不影响识别与上屏）")
    return ok


# ----------------------------------------------------
# Toast 事件
# ----------------------------------------------------

def toast_open(
    msg_id: str,
    text: str = '',
    streaming: bool = False,
    *,
    width: float = 0.5,
    height: int = 0,
    font_family: str = '',
    font_size: int = 14,
    fg: str = '#ffffff',
    bg: str = '#075077',
    duration: int = 3000,
    editable: bool = False,
    markdown: bool = True,
    role: str = '',
) -> None:
    """开启一个浮层会话"""
    get_bridge().send({
        'type': 'toast_open',
        'id': msg_id,
        'streaming': streaming,
        'text': text,
        'style': {
            'width': width,
            'height': height,
            'font_family': font_family,
            'font_size': font_size,
            'fg': fg,
            'bg': bg,
            'duration': duration,
            'editable': editable,
            'markdown': markdown,
            'role': role,
        },
    })


def toast_append(msg_id: str, delta: str) -> None:
    """追加流式增量（注意：是增量，不是全量）"""
    if not delta:
        return
    get_bridge().send({'type': 'toast_append', 'id': msg_id, 'delta': delta})


def toast_finish(msg_id: str) -> None:
    """流式结束，触发 Markdown 定稿与自动关闭倒计时"""
    get_bridge().send({'type': 'toast_finish', 'id': msg_id})


def toast_close(msg_id: str) -> None:
    """立即关闭浮层"""
    get_bridge().send({'type': 'toast_close', 'id': msg_id})


# ----------------------------------------------------
# 其它 UI 事件
# ----------------------------------------------------

def notify(message: str, level: str = 'info', duration: int = 3000) -> None:
    """通用提示气泡（对应原 toast() 的简单用法）"""
    get_bridge().send({
        'type': 'notify',
        'message': message,
        'level': level,
        'duration': duration,
    })


def recording_state(active: bool, elapsed: float = 0.0) -> None:
    """录音状态指示"""
    get_bridge().send({
        'type': 'recording_state',
        'active': bool(active),
        'elapsed': float(elapsed),
    })


def recognition(
    text: str,
    original: str = '',
    latency: float = 0.0,
    hotwords: Optional[list] = None,
) -> None:
    """一次识别完成的摘要，供 Dashboard 显示"""
    get_bridge().send({
        'type': 'recognition',
        'text': text,
        'original': original,
        'latency': float(latency),
        'hotwords': list(hotwords or []),
    })


def close_bridge() -> None:
    """退出时关闭桥（幂等）"""
    global _bridge
    if _bridge is not None:
        _bridge.close()
        _bridge = None
