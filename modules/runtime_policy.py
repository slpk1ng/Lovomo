"""Validated operational limits shared by startup, workers and the settings UI."""
import math

# key: default, minimum, maximum (seconds, except memory values in MiB).
LIMITS = {
    "tts_ready_timeout_seconds": (300, 5, 3600),
    "tts_ready_poll_seconds": (5, 0.1, 30),
    "tts_weight_timeout_seconds": (30, 1, 120),
    "asr_local_timeout_seconds": (3600, 10, 7200),
    "local_model_load_timeout_seconds": (1800, 10, 7200),
    "todo_retry_delay_seconds": (120, 5, 3600),
    "plugin_callback_timeout_seconds": (5, 0.1, 30),
    "startup_free_ram_mb": (4096, 0, 262144),
    "startup_free_vram_mb": (3072, 0, 262144),
}


def limit(config, key):
    default, minimum, maximum = LIMITS[key]
    try:
        value = float(config.get(key, default))
        if not math.isfinite(value):
            return default
        return max(minimum, min(maximum, value))
    except (AttributeError, ValueError, TypeError, OverflowError):
        return default
