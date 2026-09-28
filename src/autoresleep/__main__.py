"""`python -m autoresleep ...` - works with no installation at all."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())