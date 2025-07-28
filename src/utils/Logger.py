from typing import Any, Optional

import sys

import loguru


class Logger:
    """Static logger class."""

    _console: Optional[int] = None
    _file: Optional[int] = None

    def __init__(self):
        raise NotImplementedError()

    @classmethod
    def enable_console(cls, dest: Any = sys.stdout, level: str = "DEBUG"):
        cls.disable_console()
        cls._console = loguru.logger.add(
            lambda msg: print(msg, end="", file=dest),
            format="<green>{time:HH:mm:ss}</green> <level>[{level}]</level> <cyan>{name}</cyan>: <level>{message}</level>",
            level=level,
            colorize=True,
        )

    @classmethod
    def enable_file(cls, root_dir: str = "logs", level: str = "DEBUG"):
        cls.disable_file()
        cls._file = loguru.logger.add(
            root_dir + "/{time:YYYY-MM-DD}.log",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} [{level}] {name}: {message}",
            level=level,
            rotation="10 MB",
            retention="7 days",
            compression="zip",
        )

    @classmethod
    def disable_console(cls):
        if cls._console is not None:
            loguru.logger.remove(cls._console)
            cls._console = None

    @classmethod
    def disable_file(cls):
        if cls._file is not None:
            loguru.logger.remove(cls._file)
            cls._file = None

    @classmethod
    def reset(cls):
        loguru.logger.remove()
        loguru.logger.level("DEBUG", color="<blue>")
        loguru.logger.level("INFO", color="<white>")
        loguru.logger.level("WARNING", color="<yellow>")
        loguru.logger.level("ERROR", color="<bold><red>")

    debug = loguru.logger.debug

    info = loguru.logger.info

    warning = loguru.logger.warning

    error = loguru.logger.error
