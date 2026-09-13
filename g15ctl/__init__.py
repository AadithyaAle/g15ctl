"""g15ctl -- Alienware Command Center style fan and thermal control for Linux.

Targets Dell G-series laptops (developed and verified on a Dell G15 5530,
BIOS 1.34.0) by driving the same AWCC/WMAX firmware interface that Alienware
Command Center uses on Windows.
"""

from .constants import APP_NAME, APP_VERSION

__all__ = ["APP_NAME", "APP_VERSION", "__version__"]
__version__ = APP_VERSION
