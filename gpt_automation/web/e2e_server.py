"""Isolated server entrypoint for Playwright end-to-end tests.

Creates temporary input/state under a workspace-local temp directory so the
e2e suite never touches production input files or the workflow database.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import config
from .server import serve


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent.parent
    work = Path(tempfile.mkdtemp(prefix=".e2e-", dir=config.TMP_DIR))
    results_path = work / "accounts" / "results.txt"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_bytes(
        b"account----password----totp----email-password\n"
        b"demo@example.com----demo-password----demo-totp----mail-password\n"
        b"session-demo----fingerprint-demo----SUCCESS\n"
    )
    db_path = work / "state.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS resource_leases ("
            "resource_type TEXT NOT NULL,"
            " resource_fingerprint TEXT NOT NULL,"
            " session_id TEXT,"
            " state TEXT NOT NULL,"
            " created_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL,"
            " PRIMARY KEY (resource_type, resource_fingerprint))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS workflow_sessions ("
            "session_id TEXT PRIMARY KEY,"
            " email_fingerprint TEXT NOT NULL,"
            " stage TEXT NOT NULL,"
            " status TEXT NOT NULL,"
            " last_error TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS workflow_step_runs ("
            "session_id TEXT NOT NULL, step_id TEXT NOT NULL,"
            " status TEXT NOT NULL, outcome_code TEXT NOT NULL,"
            " attempt_no INTEGER NOT NULL, evidence_digest TEXT NOT NULL,"
            " last_error TEXT NOT NULL, created_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS workflow_evidence ("
            "session_id TEXT NOT NULL, step_id TEXT NOT NULL,"
            " evidence_type TEXT NOT NULL, evidence_digest TEXT NOT NULL,"
            " details_json TEXT NOT NULL, verifier_version TEXT NOT NULL,"
            " created_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS action_journal ("
            "session_id TEXT NOT NULL, action TEXT NOT NULL,"
            " step_id TEXT NOT NULL DEFAULT '',"
            " effect_class TEXT NOT NULL DEFAULT 'UNKNOWN',"
            " state TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS workflow_step_events ("
            "event_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_id TEXT NOT NULL, step_id TEXT NOT NULL,"
            " attempt_no INTEGER NOT NULL, status TEXT NOT NULL,"
            " outcome_code TEXT NOT NULL, evidence_digest TEXT NOT NULL,"
            " last_error TEXT NOT NULL, created_at TEXT NOT NULL)"
        )

    httpd = serve(
        project_root=work,
        input_dir=work / "input",
        db_path=db_path,
        snapshot_path=work / "demo.json",
        dist_dir=root / "frontend" / "dist",
        port=args.port,
    )
    print(f"[e2e] http://127.0.0.1:{httpd.server_address[1]}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
