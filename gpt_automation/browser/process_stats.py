"""Firefox process / handle / memory statistics for soak validation.

Soak tests must prove that repeated launch/close cycles return to a stable
process baseline.  A stats query that fails must raise, never return a
sentinel that a test could compare against itself and misread as success.
"""

from __future__ import annotations

import json
import subprocess
from typing import Dict


def firefox_process_stats() -> Dict[str, int]:
    """Return (count, handles, working_set_bytes) for ms-playwright firefox.

    Raises:
        RuntimeError: when the query itself fails; a soak test must fail,
            not compare ``-1 == -1`` and claim there is no leak.
    """
    script = (
        "$p = Get-Process firefox -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Path -like '*ms-playwright*' }; "
        "[pscustomobject]@{"
        "count = @($p).Count; "
        "handles = (@($p | Measure-Object -Property HandleCount -Sum).Sum); "
        "rss = (@($p | Measure-Object -Property WorkingSet64 -Sum).Sum)"
        "} | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(
            f"firefox process stats query failed: {type(exc).__name__}"
        ) from exc
    try:
        payload = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("firefox process stats returned invalid JSON") from exc
    try:
        return {
            "count": int(payload.get("count", 0)),
            "handles": int(payload.get("handles", 0) or 0),
            "rss": int(payload.get("rss", 0) or 0),
        }
    except (TypeError, ValueError) as exc:
        raise RuntimeError("firefox process stats payload is malformed") from exc
