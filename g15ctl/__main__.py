"""Allow `python3 -m g15ctl`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
