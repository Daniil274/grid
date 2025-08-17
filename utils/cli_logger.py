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
            try:
                self.stream.write(f"{ts} [{level}] {message}\n")
                self.stream.flush()
            except Exception:
                # Swallow stream errors to ensure logging does not break caller flow
                # Optionally, we could attempt to fallback to stderr, but tests only
                # require graceful handling without raising.
                pass

    def mcp_call(self, server: str, method: str, args: dict):
        """Красивое логгирование MCP вызовов."""
        # Определяем иконки для серверов
        server_icons = {
            'filesystem': '📁',
            'git': '🔀',
            'sequential_thinking': '🧠',
            'coordinator': '🎯'
        }
        
        icon = server_icons.get(server, '🔧')
        
        # Форматируем аргументы
        args_summary = ""
        if args:
            args_parts = []
            for key, value in args.items():
                if isinstance(value, str) and len(value) > 40:
                    args_parts.append(f"{key}=...({len(value)} chars)")
                else:
                    args_parts.append(f"{key}={value}")
            if args_parts:
                args_summary = f" ({', '.join(args_parts)})"
        
        # Красивое форматирование вызова
        self.info(f"◦ {icon} [{server}] {method}{args_summary}")

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

# Глобальный экземпляр для использования в других модулях
cli_logger = CLILogger()
