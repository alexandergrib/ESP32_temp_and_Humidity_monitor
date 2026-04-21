"""ESP controller event parsing helpers."""

import json
import re


_BARE_NAN_RE = re.compile(r"(?<=[:\[,])\s*nan\s*(?=[,\]}])")


def parse_esp_event_json(line):
    """Parse one controller log line, accepting firmware bare lowercase nan."""
    json_start = line.find("{")
    if json_start < 0:
        return None
    json_text = line[json_start:]
    json_text = _BARE_NAN_RE.sub("null", json_text)
    try:
        return json.loads(json_text)
    except Exception:
        return None
