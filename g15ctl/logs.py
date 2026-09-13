"""Logging setup.

Logs go to two places so they are always retrievable the way a Linux user
expects:

* stderr / journald -- when run under systemd, stderr is captured by the
  journal, so `journalctl -u g15ctl` works with no extra plumbing.
* ``/var/log/g15ctl/g15ctl.log`` -- a size-capped rotating file, so history
  survives even with a volatile journal (Ubuntu's default journal is
  persistent, but a plain file is easier to hand to someone for support).

`g15ctl logs` reads both back.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import shutil
import subprocess
import sys

from . import constants as C

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup(verbose: bool = False, quiet: bool = False, to_file: bool = True) -> None:
    """Configure root logging. Safe to call more than once."""
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # When running under systemd the journal already timestamps entries, so a
    # bare format avoids duplicated timestamps in `journalctl`.
    under_systemd = bool(os.environ.get("JOURNAL_STREAM") or os.environ.get("INVOCATION_ID"))
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO))
    stream.setFormatter(
        logging.Formatter("%(levelname)-7s %(message)s" if under_systemd else _FMT, _DATEFMT)
    )
    root.addHandler(stream)

    if not to_file or os.geteuid() != 0:
        return
    try:
        os.makedirs(C.LOG_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            C.LOG_PATH, maxBytes=1 << 20, backupCount=3
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(_FMT, _DATEFMT))
        root.addHandler(handler)
    except OSError as e:
        root.warning("file logging disabled: %s", e)


def read_logs(lines: int = 50, follow: bool = False, journal: bool = True) -> int:
    """Print recent log output. Returns a process exit code."""
    if journal and shutil.which("journalctl"):
        cmd = ["journalctl", "-u", "%s.service" % C.APP_NAME,
               "-u", "%s-restore.service" % C.APP_NAME,
               "-n", str(lines), "--no-pager"]
        if follow:
            cmd.append("-f")
        result = subprocess.run(cmd)
        # An unregistered unit yields no output; fall through to the file.
        if result.returncode == 0 and not follow:
            pass
        elif result.returncode != 0:
            print("(journalctl returned %d; falling back to %s)"
                  % (result.returncode, C.LOG_PATH), file=sys.stderr)

    if not os.path.exists(C.LOG_PATH):
        print("No log file at %s yet." % C.LOG_PATH, file=sys.stderr)
        return 0
    print("\n--- %s (last %d lines) ---" % (C.LOG_PATH, lines))
    if follow:
        return subprocess.run(["tail", "-n", str(lines), "-f", C.LOG_PATH]).returncode
    try:
        with open(C.LOG_PATH, errors="replace") as f:
            tail = f.readlines()[-lines:]
        sys.stdout.writelines(tail)
    except OSError as e:
        print("could not read %s: %s" % (C.LOG_PATH, e), file=sys.stderr)
        return 1
    return 0
