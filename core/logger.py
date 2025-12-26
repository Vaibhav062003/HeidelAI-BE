import logging
import sys
import os
from datetime import datetime
from zoneinfo import ZoneInfo
import inspect
import json

# ANSI color codes
COLORS = {
    "INFO": "\033[94m",     # Blue
    "WARNING": "\033[93m",  # Yellow
    "ERROR": "\033[91m",    # Red
    "SUCCESS": "\033[92m",  # Green
    "RESET": "\033[0m",
}

EMOJIS = {
    "INFO": "ℹ️",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "SUCCESS": "✅",
}

# Custom SUCCESS level
SUCCESS_LEVEL = 25
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")


class CustomFormatter(logging.Formatter):
    def format(self, record):
        # Date & time
        now = datetime.now(tz=ZoneInfo("Asia/Kolkata"))
        date_str = now.strftime("%d-%m-%y")
        time_str = now.strftime("%H:%M:%S")

        # Caller info
        frame = inspect.stack()[8]
        file_name = os.path.basename(frame.filename)
        function_name = frame.function
        location = f"{file_name}:{function_name}"

        level = record.levelname
        color = COLORS.get(level, "")
        emoji = EMOJIS.get(level, "")
        reset = COLORS["RESET"]

        message = record.getMessage()

        return (
            f"{color}{date_str} {time_str} "
            f"[{location}] "
            f"[{level}] {emoji} {message}{reset}"
        )


class CustomLogger(logging.Logger):
    def _join_args(self, message, args):
        if not args:
            return message

        formatted_args = []
        for arg in args:
            if isinstance(arg, (dict, list)):
                formatted_args.append(json.dumps(arg, indent=2, ensure_ascii=False))
            else:
                formatted_args.append(str(arg))

        return f"{message} " + " ".join(formatted_args)

    def info(self, message, *args, **kwargs):
        if args:
            # Check if message is likely a format string (contains %)
            # This preserves uvicorn logging while allowing print-style logging
            if isinstance(message, str) and "%" in message:
                super().info(message, *args, **kwargs)
            else:
                message = self._join_args(message, args)
                super().info(message, **kwargs)
        else:
            super().info(message, **kwargs)

    def warning(self, message, *args, **kwargs):
        message = self._join_args(message, args)
        super().warning(message, **kwargs)

    def error(self, message, *args, **kwargs):
        message = self._join_args(message, args)
        super().error(message, **kwargs)

    def success(self, message, *args, **kwargs):
        message = self._join_args(message, args)
        if self.isEnabledFor(SUCCESS_LEVEL):
            self._log(SUCCESS_LEVEL, message, (), **kwargs)



def get_logger(name: str = "app", level=logging.INFO) -> CustomLogger:
    logging.setLoggerClass(CustomLogger)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger  # Prevent duplicate handlers

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CustomFormatter())
    logger.addHandler(handler)

    return logger
