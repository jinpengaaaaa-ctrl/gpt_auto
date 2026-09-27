"""Run the local desktop dashboard server.

Example:
    python -m gpt_automation.web --port 8765

The dashboard serves the built frontend and the local JSON API on 127.0.0.1.
The console's default path is the real two-stage workflow; demo mode is kept
only as an internal/test fallback and is never exposed as a run option.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .server import serve


def main(argv=None) -> int:
    # Worker threads print UTF-8 (emoji, CJK) from CloakBrowser preflight and
    # browser launch code; without this, cp932 consoles raise
    # UnicodeEncodeError and abort the run before the browser ever starts.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--dist",
        type=Path,
        default=None,
        help="frontend build directory (default: <repo>/frontend/dist)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="workflow sqlite path (default: <repo>/runtime/workflow_state.db)",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="input directory (default: <repo>/input)",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="demo run snapshot path (default: <repo>/runtime/demo_runs.json)",
    )
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parent.parent.parent
    dist_dir = args.dist or project_root / "frontend" / "dist"
    db_path = args.db or project_root / "runtime" / "workflow_state.db"
    input_dir = args.input or project_root / "input"
    snapshot_path = (
        args.snapshot or project_root / "runtime" / "demo_runs.json"
    )

    if not dist_dir.exists():
        print(
            f"[dashboard] frontend build missing: {dist_dir}. "
            "Run `npm run build` in frontend/ first.",
            file=sys.stderr,
        )
        return 1

    httpd = serve(
        project_root=project_root,
        input_dir=input_dir,
        db_path=db_path,
        snapshot_path=snapshot_path,
        dist_dir=dist_dir,
        host=args.host,
        port=args.port,
    )
    actual_port = httpd.server_address[1]
    print(f"[dashboard] http://{args.host}:{actual_port}")
    print("[dashboard] real-run console ready; demo mode is internal fallback only")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
