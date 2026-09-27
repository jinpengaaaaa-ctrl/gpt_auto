"""
CAPTCHA / 通知音工具 (Feature #5).

Plays a short audible cue to alert the operator that the script is
blocked on a manual step (CAPTCHA, password verification, etc.).

Implementation:
    * Primary:    winsound.PlaySound(.wav file)  if config.CAPTCHA_BEEP.sound_file is set
    * Fallback:   winsound.Beep()  (Windows only)
    * Last fallback: \a terminal bell

Never raises.  All errors are swallowed and logged so a missing
audio device does not abort the run.
"""

from __future__ import annotations

import logging
import sys
import time

import config

logger = logging.getLogger(__name__)


def play_captcha_beep() -> None:
    """Play the configured beep pattern (Feature #5)."""
    cfg = config.CAPTCHA_BEEP
    frequency: int = int(cfg.get("frequency", 880))
    duration_ms: int = int(cfg.get("duration_ms", 600))
    repeat: int = int(cfg.get("repeat", 3))
    pause_ms: int = int(cfg.get("pause_ms", 300))
    sound_file: str | None = cfg.get("sound_file")

    logger.info(
        "beep: CAPTCHA signal — %d × %dms @ %dHz%s",
        repeat, duration_ms, frequency,
        f" + WAV: {sound_file}" if sound_file else "",
    )

    # 1. Windows: winsound
    try:
        import winsound  # type: ignore[import-not-found]

        # 1a. WAV file wins over synthetic Beep when configured
        if sound_file:
            import os
            wav_path = os.path.join(config.BASE_DIR, sound_file)
            for _ in range(max(1, repeat)):
                try:
                    winsound.PlaySound(wav_path, winsound.SND_FILENAME)
                except RuntimeError:
                    pass
                if pause_ms > 0:
                    time.sleep(pause_ms / 1000.0)
            return

        # 1b. Traditional synthetic Beep (backwards compatible)
        for _ in range(max(1, repeat)):
            try:
                winsound.Beep(frequency, duration_ms)
            except RuntimeError:
                pass
            if pause_ms > 0:
                time.sleep(pause_ms / 1000.0)
        return
    except ImportError:
        pass

    # 2. macOS / Linux: try shutil / system bell
    try:
        for _ in range(max(1, repeat)):
            sys.stdout.write("\a")
            sys.stdout.flush()
            if pause_ms > 0:
                time.sleep(pause_ms / 1000.0)
        return
    except Exception:
        pass

    # 3. Last resort: silent sleep
    total_ms: int = repeat * duration_ms + max(0, repeat - 1) * pause_ms
    time.sleep(total_ms / 1000.0)


def notify_operator(message: str) -> None:
    """Combined audible + logged operator notification."""
    logger.warning("需操作员介入：%s", message)
    print(f"\n\U0001f6a8 [手动] {message}")
    play_captcha_beep()


def play_completion_sound() -> None:
    """Play the configured completion chime when all processing is done."""
    cfg = config.COMPLETION_SOUND
    sound_file: str | None = cfg.get("sound_file")
    repeat: int = int(cfg.get("repeat", 1))
    pause_ms: int = int(cfg.get("pause_ms", 500))

    if not sound_file:
        logger.info("completion sound: no sound_file configured, skipping")
        return

    import os
    wav_path = os.path.join(config.BASE_DIR, sound_file)
    if not os.path.isfile(wav_path):
        logger.warning("completion sound: WAV not found at %s", wav_path)
        return

    logger.info("completion sound: playing %s × %d", sound_file, repeat)

    try:
        import winsound  # type: ignore[import-not-found]
        for _ in range(max(1, repeat)):
            try:
                winsound.PlaySound(wav_path, winsound.SND_FILENAME)
            except RuntimeError:
                pass
            if pause_ms > 0:
                time.sleep(pause_ms / 1000.0)
    except ImportError:
        # Fallback: system bell
        sys.stdout.write("\a")
        sys.stdout.flush()


def play_ip_switch_prompt() -> None:
    """Play the configured beep pattern for manual IP switch prompt."""
    cfg = config.MANUAL_MODE_CONFIG
    frequency: int = 660   # slightly lower than CAPTCHA beep (880Hz) to distinguish
    duration_ms: int = 400
    repeat: int = int(cfg.get("beep_repeat", 3))
    pause_ms: int = int(cfg.get("beep_pause_ms", 500))

    logger.info(
        "beep: IP switch signal — %d × %dms @ %dHz",
        repeat, duration_ms, frequency,
    )

    try:
        import winsound  # type: ignore[import-not-found]
        for _ in range(max(1, repeat)):
            try:
                winsound.Beep(frequency, duration_ms)
            except RuntimeError:
                pass
            if pause_ms > 0:
                time.sleep(pause_ms / 1000.0)
        return
    except ImportError:
        pass

    # Fallback: system bell
    try:
        for _ in range(max(1, repeat)):
            sys.stdout.write("\a")
            sys.stdout.flush()
            if pause_ms > 0:
                time.sleep(pause_ms / 1000.0)
    except Exception:
        pass
