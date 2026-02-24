import logging
import re


KEY_PATTERNS = [
    re.compile(r"(apikey=)([^&\s]+)", flags=re.IGNORECASE),
    re.compile(r"(X-Api-Key[:=]\s*)([A-Za-z0-9_\-]+)", flags=re.IGNORECASE),
]


def redact_secrets(message: str) -> str:
    redacted = message
    for pattern in KEY_PATTERNS:
        redacted = pattern.sub(r"\1***", redacted)
    return redacted


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact_secrets(rendered)


def setup_logging(level: str) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactingFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    root.addHandler(handler)
