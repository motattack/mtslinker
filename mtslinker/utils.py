import logging
import os

LOGS_ROOT = 'logs'
LOG_FILENAME = 'mtslinker.log'
LOG_FILEPATH = os.path.join(LOGS_ROOT, LOG_FILENAME)


def initialize_logger():
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not root_logger.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S'
        ))
        root_logger.addHandler(console_handler)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)


def create_directory_if_not_exists(directory: str) -> str:
    if not os.path.exists(directory):
        os.makedirs(directory)
    return directory
