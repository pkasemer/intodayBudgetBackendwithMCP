"""JSON logs to stdout. Callers pass counts and record IDs only — never amounts,
notes, labels or the token (backend spec §3, §11)."""
import json
import logging
import sys
from datetime import datetime, timezone

_logger = logging.getLogger("intoday")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
        }
        out.update(getattr(record, "fields", {}))
        return json.dumps(out, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    _logger.handlers[:] = [handler]
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def log(event: str, level: int = logging.INFO, **fields) -> None:
    _logger.log(level, event, extra={"fields": fields})
