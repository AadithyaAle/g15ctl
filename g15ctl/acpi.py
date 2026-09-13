"""Transport for evaluating the AWCC `WMAX` ACPI method via acpi_call.

The kernel's `acpi_call` module exposes a single procfs file. You write an
expression such as::

    \\_SB.AMWW.WMAX 0 0x15 {0x02, 0x32, 0x80, 0x00}

then read the same file back to get the return value. Because that is a
write-then-read pair on shared global state, concurrent users would interleave
and read each other's results, so every evaluation here is serialised behind a
file lock that is also honoured by the daemon and the GUI.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import subprocess
import time
from typing import Iterable

from . import constants as C

log = logging.getLogger(__name__)

_LOCK_PATH = "/run/g15ctl.acpi.lock"


class AcpiError(RuntimeError):
    """Raised when the ACPI transport itself is unusable."""


class WmaxUnsupported(RuntimeError):
    """Raised when the firmware rejects a method/operation combination."""


def _lock_file():
    # /run always exists and is tmpfs; fall back to /tmp for unprivileged use.
    for path in (_LOCK_PATH, "/tmp/g15ctl.acpi.lock"):
        try:
            return open(path, "w")
        except OSError:
            continue
    return None


class _Lock:
    """Best-effort exclusive lock around the shared /proc/acpi/call file."""

    def __init__(self):
        self._fh = None

    def __enter__(self):
        self._fh = _lock_file()
        if self._fh is not None:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_EX)
            except OSError:
                pass
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        return False


def acpi_call_available() -> bool:
    return os.path.exists(C.ACPI_CALL_PROC)


def load_acpi_call() -> bool:
    """Ensure the acpi_call module is loaded. Requires root."""
    if acpi_call_available():
        return True
    if os.geteuid() != 0:
        return False
    try:
        subprocess.run(
            ["modprobe", "acpi_call"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # modprobe returns before procfs registration settles on some kernels.
    for _ in range(20):
        if acpi_call_available():
            return True
        time.sleep(0.05)
    return acpi_call_available()


def _raw_eval(expression: str) -> str:
    try:
        with open(C.ACPI_CALL_PROC, "w") as f:
            f.write(expression)
        with open(C.ACPI_CALL_PROC, "rb") as f:
            data = f.read()
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EPERM):
            raise AcpiError("permission denied on %s (run with sudo)" % C.ACPI_CALL_PROC) from e
        if e.errno == errno.ENOENT:
            raise AcpiError("acpi_call module is not loaded") from e
        raise AcpiError("ACPI evaluation failed: %s" % e) from e
    return data.split(b"\0")[0].decode("utf-8", "replace").strip()


def _parse(result: str) -> int:
    """Convert an acpi_call result string into an integer."""
    if not result:
        raise WmaxUnsupported("empty result from ACPI call")
    if result.startswith("Error"):
        raise WmaxUnsupported(result)
    if result.startswith("not called"):
        raise AcpiError("acpi_call did not evaluate the expression")
    try:
        return int(result, 16) if result.lower().startswith("0x") else int(result)
    except ValueError:
        # Buffer results come back as e.g. {0x01, 0x02}; we only ever use
        # integer-returning methods, so treat this as unsupported.
        raise WmaxUnsupported("non-integer result: %r" % result) from None


class Wmax:
    """A bound handle to the firmware's WMAX method.

    Construct via :meth:`detect` so the namespace path is discovered rather
    than assumed.
    """

    def __init__(self, path: str):
        self.path = path

    # -- construction -------------------------------------------------------

    @classmethod
    def detect(cls, candidates: Iterable[str] | None = None) -> "Wmax":
        """Find the working WMAX path by issuing a harmless read.

        We use 0x14/0x0B (get current thermal profile), which only reads EC
        state. A wrong path fails with AE_NOT_FOUND and is skipped.
        """
        if not load_acpi_call():
            raise AcpiError(
                "acpi_call is unavailable. Install it with "
                "`apt install acpi-call-dkms` and ensure you are root."
            )
        tried = []
        for path in candidates or C.WMAX_PATH_CANDIDATES:
            probe = cls(path)
            try:
                value = probe.call(C.M_THERMAL_INFO, C.OP_GET_CURRENT_PROFILE)
            except (WmaxUnsupported, AcpiError) as e:
                tried.append("%s (%s)" % (path, e))
                continue
            if value in C.WMAX_ERRORS:
                tried.append("%s (returned %#x)" % (path, value))
                continue
            log.debug("WMAX detected at %s (current profile %#x)", path, value)
            return probe
        raise AcpiError(
            "could not find a working WMAX ACPI method. Tried:\n  "
            + "\n  ".join(tried)
        )

    # -- evaluation ---------------------------------------------------------

    def call(self, method: int, op: int, arg1: int = 0, arg2: int = 0, arg3: int = 0) -> int:
        """Evaluate WMAX(0, method, {op, arg1, arg2, arg3}).

        Returns the raw integer, including the firmware's 0xFFFFFFFE/F
        "unsupported" sentinels, so callers can distinguish those from a
        transport failure.
        """
        for value in (op, arg1, arg2, arg3):
            if not 0 <= value <= 0xFF:
                raise ValueError("WMAX buffer bytes must be 0..255, got %r" % value)
        expression = "%s 0 %#x {%#x, %#x, %#x, %#x}" % (
            self.path, method, op, arg1, arg2, arg3,
        )
        with _Lock():
            raw = _raw_eval(expression)
        log.debug("WMAX %s -> %s", expression, raw)
        return _parse(raw)

    def query(self, method: int, op: int, arg1: int = 0, arg2: int = 0) -> int | None:
        """Like :meth:`call` but returns None for unsupported operations."""
        try:
            value = self.call(method, op, arg1, arg2)
        except WmaxUnsupported:
            return None
        return None if value in C.WMAX_ERRORS else value
