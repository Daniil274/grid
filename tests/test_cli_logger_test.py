import io
from utils.cli_logger import CLILogger

# Basic test: ensure all levels emit

def test_cli_logger_basic_outputs():
    buf = io.StringIO()
    logger = CLILogger(level="DEBUG", stream=buf)
    logger.debug("debug-message")
    logger.info("info-message")
    logger.warning("warn-message")
    logger.error("error-message")
    logger.critical("critical-message")
    out = buf.getvalue()
    assert "[DEBUG] debug-message" in out
    assert "[INFO] info-message" in out
    assert "[WARNING] warn-message" in out
    assert "[ERROR] error-message" in out
    assert "[CRITICAL] critical-message" in out


def test_cli_logger_level_filtering():
    buf = io.StringIO()
    logger = CLILogger(level="WARNING", stream=buf)
    logger.info("should-not-appear")
    logger.debug("neither-should-appear")
    logger.warning("this-should-appear")
    content = buf.getvalue()
    assert "should-not-appear" not in content
    assert "neither-should-appear" not in content
    assert "[WARNING] this-should-appear" in content
