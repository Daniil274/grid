# Simple CLI logger helper

import sys
import datetime

LOG_LEVELS = {
    'DEBUG': 10,
    'INFO': 20,
    'WARNING': 30,
    'ERROR': 40,
    'CRITICAL': 50,
}

class CLILogger:
    def __init__(self, level: str = 'INFO', stream=None):
        self.level = level.upper()
        self.level_num = LOG_LEVELS.get(self.level, 20)
        self.stream = stream or sys.stdout

    def log(self, level: str, message: str):
        if LOG_LEVELS.get(level, 0) >= self.level_num:
            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.stream.write(f"{ts} [{level}] {message}\n")
            self.stream.flush()

    def debug(self, message: str):
        self.log('DEBUG', message)

    def info(self, message: str):
        self.log('INFO', message)

    def warning(self, message: str):
        self.log('WARNING', message)

    def error(self, message: str):
        self.log('ERROR', message)

    def critical(self, message: str):
        self.log('CRITICAL', message)
