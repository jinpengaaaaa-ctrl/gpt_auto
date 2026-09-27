"""Batch-level diagnostic reporting (terminal summary + error report file)."""

from gpt_automation.reporting.batch_report import (
    BatchOutcomeCategory,
    BatchRunReport,
    RetryDisposition,
    render_error_report,
    render_terminal_summary,
)

__all__ = [
    "BatchOutcomeCategory",
    "BatchRunReport",
    "RetryDisposition",
    "render_error_report",
    "render_terminal_summary",
]
