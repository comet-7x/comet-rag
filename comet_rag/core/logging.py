import logging
import sys
import threading
from pathlib import Path
from typing import Any

from loguru import logger

from .tracing import trace_ctx

_setup_lock = threading.Lock()
_is_configured = False

_patched_logger = logger


def _wrapping_patcher(record: dict[str, Any]) -> None:
    """
    通过 logger.patch() 注册，返回新实例后所有经该实例写入的日志都会触发此函数。
    务必保证不抛异常，否则该条日志会静默丢失。
    """
    try:
        record["extra"]["trace_id"] = trace_ctx.trace_id or "no-trace"
    except Exception:
        record["extra"]["trace_id"] = "no-trace"

    record["extra"].setdefault("module", "system")


class InterceptHandler(logging.Handler):
    """
    将标准库 logging 的日志桥接到 loguru。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = _patched_logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        _patched_logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


class Logger:
    """
    loguru 的轻量包装，保持惯用的 logger.info() 调用风格。
    """

    def __init__(self, name: str):
        self.name = name

    def _get_logger(self):
        return _patched_logger.bind(module=self.name)

    def _log(self, level: str, msg: str, *args: Any, **kwargs: Any) -> None:
        self._get_logger().opt(depth=2).log(level, msg, *args, **kwargs)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log("DEBUG", msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log("INFO", msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log("WARNING", msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log("ERROR", msg, *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._get_logger().opt(depth=2, exception=True).error(msg, *args, **kwargs)


def setup_logging(
    log_dir: str = "logs",
    level: str = "INFO",
    module_files: dict[str, str] | None = None,
    enable_error_file: bool = True,
    debug_mode: bool = False,
) -> None:
    """
    初始化全局日志配置，幂等，多次调用只生效一次。

    Args:
        log_dir:           日志目录，默认 "logs"
        level:             控制台最低输出级别，默认 "INFO"
        module_files:      按模块分离日志，格式 {"模块名": "文件名(不含扩展名)"}
        enable_error_file: 是否单独输出 error.log，默认 True
        debug_mode:        开启后启用 backtrace + diagnose，方便本地排查。
    """
    global _is_configured, _patched_logger

    with _setup_lock:
        if _is_configured:
            return

        logger.remove()

        _patched_logger = logger.patch(_wrapping_patcher)  # pyright: ignore[reportArgumentType]

        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        fmt = (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<magenta>{extra[trace_id]}</magenta> | "
            "<blue>P:{process.id}|T:{thread.id}</blue> | "
            "<cyan>{extra[module]}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        )

        _patched_logger.add(
            sys.stdout,
            level=level,
            format=fmt,
            colorize=True,
        )

        _patched_logger.add(
            log_path / "all.log",
            level="DEBUG",
            format=fmt,
            rotation="00:00",
            retention="30 days",
            compression="zip",
            enqueue=True,
            backtrace=debug_mode,
            diagnose=debug_mode,
        )

        if enable_error_file:
            _patched_logger.add(
                log_path / "error.log",
                level="ERROR",
                format=fmt,
                rotation="00:00",
                retention="30 days",
                compression="zip",
                enqueue=True,
                backtrace=debug_mode,
                diagnose=debug_mode,
            )

        if module_files:
            for m_name, f_name in module_files.items():
                _patched_logger.add(
                    log_path / "{time:YYYY-MM-DD}" / f"{f_name}.log",
                    level="DEBUG",
                    format=fmt,
                    filter=lambda r, m=m_name: r["extra"].get("module") == m,
                    rotation="00:00",
                    retention="30 days",
                    compression="zip",
                    enqueue=True,
                )

        intercept = InterceptHandler()
        logging.basicConfig(handlers=[intercept], level=0, force=True)
        for name in logging.root.manager.loggerDict:
            lib_logger = logging.getLogger(name)
            lib_logger.handlers = [intercept]
            lib_logger.propagate = False

        _is_configured = True


def get_logger(name: str) -> Logger:
    """获取指定模块名的 Logger 实例。"""
    return Logger(name)
