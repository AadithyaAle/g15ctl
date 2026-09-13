"""Configuration and persisted state.

Two separate files, deliberately:

* ``/etc/g15ctl/config.json`` -- user intent and policy. Survives upgrades and
  is the file a human edits.
* ``/var/lib/g15ctl/state.json`` -- what the tool last applied. Used to restore
  the mode after a reboot or resume. Machine-owned; safe to delete.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time

from . import constants as C

log = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    # Mode reapplied at boot when restore_last_mode is false.
    "default_mode": "balanced",
    # Reapply whatever was last set, matching AWCC's behaviour.
    "restore_last_mode": True,
    # Reset fan boost and G-Mode before shutdown/reboot. Keep this on: it is
    # what guarantees a dual-booted Windows + AWCC inherits a clean EC.
    "reset_on_shutdown": True,
    # Release manual fan control when the machine resumes from suspend, since
    # the EC state after resume is not guaranteed to match what we set.
    "reapply_on_resume": True,
    "watchdog": {
        "enabled": True,
        "critical_c": C.WATCHDOG_TEMP_C,
        "force_boost_c": C.WATCHDOG_BOOST_TEMP_C,
    },
    "curve": {
        "enabled": False,
        # "hottest" tracks the highest relevant sensor; or name one explicitly
        # such as "CPU" or "GPU".
        "sensor": "hottest",
        # [temperature C, fan percent]; linearly interpolated between points.
        "points": [[50, 0], [60, 15], [70, 35], [78, 60], [85, 85], [90, 100]],
        # Degrees the temperature must fall before the fan steps back down.
        "hysteresis_c": 3,
        "interval_s": 3.0,
    },
}


def _merge(base: dict, override: dict) -> dict:
    """Recursive merge so a partial config file still gets new defaults."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _write_atomic(path: str, payload: dict) -> None:
    """Write JSON atomically so a crash or power loss cannot truncate it."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("ignoring unreadable %s: %s", path, e)
        return {}


def load_config(path: str = C.CONFIG_PATH) -> dict:
    return _merge(DEFAULT_CONFIG, _read_json(path))


def save_config(config: dict, path: str = C.CONFIG_PATH) -> None:
    _write_atomic(path, config)


def load_state(path: str = C.STATE_PATH) -> dict:
    return _read_json(path)


def save_state(mode: str | None = None, fan: object = None,
               gmode: bool | None = None, path: str = C.STATE_PATH) -> None:
    """Record what we applied. Fields left as None keep their previous value."""
    state = load_state(path)
    if mode is not None:
        state["mode"] = mode
    if fan is not None:
        state["fan"] = fan
    if gmode is not None:
        state["gmode"] = gmode
    state["updated"] = time.time()
    state["version"] = C.APP_VERSION
    try:
        _write_atomic(path, state)
    except OSError as e:
        # Never let a read-only /var stop us from controlling the hardware.
        log.warning("could not persist state to %s: %s", path, e)
