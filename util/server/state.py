# coding: utf-8
"""
服务端状态管理模块
"""
from dataclasses import dataclass
from typing import Optional, Any
from multiprocessing import Process
import threading

@dataclass
class ServerState:
    """
    服务端运行状态
    """
    recognize_process: Optional[Process] = None
    # Manager 也是一个独立进程（`Manager().list()` 会 fork/spawn 出来）。
    # 不持有它就没法在退出时显式关闭，只能指望 atexit —— 而父进程被
    # TerminateProcess 硬杀时 atexit 不会跑，那个进程就成了孤儿。
    sockets_id_manager: Optional[Any] = None

# 模块级全局实例 (Python 模块天生是单例)
_global_state = ServerState()

def get_state() -> ServerState:
    return _global_state
