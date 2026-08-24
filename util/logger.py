# coding: utf-8

import os
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler
from datetime import datetime


class Logger:
    """日志系统管理器"""

    _loggers = {}

    @classmethod
    def _prune_old_logs(cls, log_dir: str, prefix: str) -> None:
        """
        删除超过保留期的历史日志。

        保留天数取 `ClientConfig.log_retention_days`，缺省 30 天；
        设为 0 或负数表示不清理。

        只删自己前缀、且形如 `<prefix>_YYYYMMDD.log[.N]` 的文件，
        不碰其它任何东西（比如外壳的 shell.err.log）。
        """
        try:
            try:
                from config_client import ClientConfig
                days = int(getattr(ClientConfig, 'log_retention_days', 30))
            except Exception:
                days = 30

            if days <= 0:
                return

            import re
            import time as _time

            cutoff = _time.time() - days * 86400
            pattern = re.compile(
                rf'^{re.escape(prefix)}_(\d{{8}})\.log(\.\d+)?$'
            )

            removed = 0
            for entry in os.scandir(log_dir):
                if not entry.is_file():
                    continue
                if not pattern.match(entry.name):
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        os.remove(entry.path)
                        removed += 1
                except OSError:
                    # 文件被占用或权限不足：跳过即可，不该影响启动
                    continue

            if removed:
                # 此时 logger 还没配好，用 print 记一笔（会进控制台/被重定向）
                print(f'[logger] 已清理 {removed} 个超过 {days} 天的 {prefix} 日志')
        except Exception as e:
            # 清理失败绝不能阻碍程序启动
            print(f'[logger] 清理历史日志时出错（已忽略）: {e}')

    @classmethod
    def setup(cls, name: str, log_dir: str = None, level: str = 'INFO', max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5, log_filename: str = None):
        """
        设置并返回一个日志记录器

        Args:
            name: 日志记录器名称（通常是 'server' 或 'client'）
            log_dir: 日志文件目录，默认为项目根目录下的 logs 文件夹
            level: 日志级别，可选值：'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
            max_bytes: 单个日志文件最大大小，默认 10MB
            backup_count: 保留的日志文件数量，默认 5 个
            log_filename: 自定义日志文件名前缀，如果不填则默认使用 name 或 'root'

        Returns:
            logging.Logger: 配置好的日志记录器
        """
        # 设置日志级别
        log_level = getattr(logging, level.upper(), logging.INFO)

        # 如果已经初始化过，更新级别并返回
        if name in cls._loggers:
            logger = cls._loggers[name]
            logger.setLevel(log_level)
            # 更新所有 handler 的级别
            for handler in logger.handlers:
                handler.setLevel(log_level)
            return logger

        # 创建日志记录器
        # 创建日志记录器 (如果 name 为空字符串，则获取 root logger)
        logger = logging.getLogger(name if name else None)
        logger.setLevel(log_level)

        # 确保不会传播到 root logger (仅对非 root logger 有效)
        if name:
            logger.propagate = False

        # 确定日志目录
        if log_dir is None:
            from config_client import BASE_DIR
            log_dir = os.path.join(BASE_DIR, 'logs')

        # 创建日志目录
        Path(log_dir).mkdir(parents=True, exist_ok=True)

        # 日志文件名（包含日期）
        if log_filename:
             file_name_prefix = log_filename
        elif not name:
             file_name_prefix = 'root'
        else:
             file_name_prefix = name
        
        log_file = os.path.join(log_dir, f'{file_name_prefix}_{datetime.now().strftime("%Y%m%d")}.log')

        # 清理过期日志。
        #
        # RotatingFileHandler 只按大小轮转**同名**文件，而文件名带日期，
        # 所以每天都会新开一个名字、旧日期的文件永久留存。
        # 实测某台机器 logs/ 累积到 906MB / 205 个文件，其中 819MB
        # 是 30 天前的。这里按保留天数清理，默认 30 天。
        cls._prune_old_logs(log_dir, file_name_prefix)

        # 创建格式化器
        formatter = logging.Formatter(
            fmt='%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
            datefmt='%H:%M:%S'
        )

        # 文件处理器（带轮转）
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # 缓存日志记录器
        cls._loggers[name] = logger

        return logger

    @classmethod
    def get_logger(cls, name: str):
        """
        获取已创建的日志记录器，如果不存在则创建一个默认的

        Args:
            name: 日志记录器名称

        Returns:
            logging.Logger: 日志记录器
        """
        if name not in cls._loggers:
            # 如果 logger 还没有被初始化，先创建一个默认的（INFO 级别）
            # 之后 core_client.py/core_server.py 会用正确的级别重新初始化
            return cls.setup(name, level='INFO')
        return cls._loggers[name]


# 便捷函数
def setup_logger(name: str, log_dir: str = None, level: str = 'INFO', **kwargs):
    """设置日志记录器的便捷函数"""
    return Logger.setup(name, log_dir, level, **kwargs)


def get_logger(name: str):
    """获取日志记录器的便捷函数"""
    return Logger.get_logger(name)
