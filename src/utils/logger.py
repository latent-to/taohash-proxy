import logging

import os

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_file = os.environ.get("LOG_FILE")
    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            try:
                os.makedirs(log_dir, exist_ok=True)
            except Exception as e:
                print(f"Failed to create log directory {log_dir}: {e}")

        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            logging.getLogger().addHandler(file_handler)
        except Exception as e:
            print(f"Failed to setup file logging to {log_file}: {e}")

logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the specified name.

    Returns:
        A configured logger instance
    """
    return logging.getLogger(name)


def get_message_type(message: dict) -> str:
    """
    Determine the type of the Stratum message, treating messages with method and no meaningful id as notifications.

    Args:
        message (dict): The message dictionary.

    Returns:
        str: The message type ('response', 'notification', or 'unknown').
    """
    if "method" in message and message.get("id") is None:
        return "notification"
    if "id" in message and message.get("id") is not None:
        return "response"
    if "method" in message:
        return "notification"
    return "unknown"


def log_stratum_message(
    logger: logging.Logger, message: dict, prefix: str = "", level: int = logging.DEBUG
):
    """
    Log a Stratum message with its type and an optional prefix.

    Args:
        logger (logging.Logger): The logger instance.
        message (dict): The message dictionary to log.
        prefix (str): An optional prefix to add context to the log message.
        level (int): The logging level (default is DEBUG).
    """
    msg_type = get_message_type(message)
    log_message = (
        f"{prefix}: Received {msg_type} message: {message}"
        if prefix
        else f"Received {msg_type} message: {message}"
    )
    logger.log(level, log_message)
