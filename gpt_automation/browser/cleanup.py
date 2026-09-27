"""Browser-neutral profile directory cleanup.

``cleanup_profile`` only touches the local profile directory on disk; it does
not depend on any concrete browser backend (CloakBrowser or Firefox private).
Business layers may import this module directly without violating the rule
that workflow/phases must never import a concrete browser Provider.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import config

logger = logging.getLogger(__name__)


def cleanup_profile(profile_id: str) -> bool:
    """Delete the persistent browser profile directory after successful registration.

    This frees disk space and ensures stale session data doesn't accumulate.
    Only call this after the entire registration flow has completed successfully.

    Args:
        profile_id: The identifier used when creating the profile (e.g. email prefix).

    Returns:
        True if the profile was deleted, False if it didn't exist or deletion failed.
    """
    profile_root = Path(config.PROFILE_DIR).resolve()
    profile_path = (profile_root / f"profile_{profile_id}").resolve()
    if profile_path.parent != profile_root:
        logger.error("cleanup_profile: rejected path outside profile root: %s", profile_id)
        return False
    if not os.path.exists(profile_path):
        logger.info("cleanup_profile: profile not found, skipping: %s", profile_path)
        return False

    try:
        shutil.rmtree(profile_path)
        logger.info("cleanup_profile: deleted profile directory: %s", profile_path)
        return True
    except Exception as exc:
        logger.warning("cleanup_profile: failed to delete %s: %s", profile_path, exc)
        return False
