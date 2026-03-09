# coding: utf-8
"""
Rich Status 扩展模块

提供增强版的 Status 类，支持状态追踪。
"""

import sys

from rich.console import RenderableType
from rich.style import StyleType
from rich.status import Status as RichStatus


class Status(RichStatus):
    """
    增强版 Rich Status
    
    扩展 rich.status.Status，添加 started 属性用于追踪状态。
    防止重复启动和停止动画。
    
    Attributes:
        started: 是否已启动动画
    """
    
    def __init__(
        self,
        status: RenderableType,
        *,
        spinner: str = "dots",
        spinner_style: StyleType = "status.spinner",
        speed: float = 1.0,
        refresh_per_second: float = 12.5
    ):
        """
        初始化 Status
        
        Args:
            status: 要显示的状态文本
            spinner: 动画类型名称
            spinner_style: 动画样式
            speed: 动画速度
            refresh_per_second: 刷新频率
        """
        super().__init__(
            status,
            console=None,
            spinner=spinner,
            spinner_style=spinner_style,
            speed=speed,
            refresh_per_second=refresh_per_second,
        )
        self.started = False
        try:
            stream = getattr(sys, "stdout", None)
            is_tty = bool(getattr(stream, "isatty", lambda: False)()) if stream else False
            encoding = (getattr(stream, "encoding", "") or "").lower() if stream else ""
            # Rich 的部分 spinner（如 point）包含 Unicode 字符，非 UTF 控制台容易抛编码异常
            self.enabled = is_tty and ("utf" in encoding or "65001" in encoding)
        except Exception:
            self.enabled = False

    def start(self) -> None:
        """启动动画（如果尚未启动）"""
        if not self.started:
            self.started = True
            if self.enabled:
                try:
                    super().start()
                except Exception:
                    # 控制台不可写/编码异常时，自动降级为禁用状态动画
                    self.enabled = False

    def stop(self) -> None:
        """停止动画（如果已启动）"""
        if self.started:
            self.started = False
            if self.enabled:
                try:
                    super().stop()
                except Exception:
                    self.enabled = False
