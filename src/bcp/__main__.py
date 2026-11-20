"""Entry point for ``python -m bcp``."""

import sys

from bcp.cli import main

if __name__ == "__main__":
    sys.exit(main())
