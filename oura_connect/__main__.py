"""Module entrypoint for `python3 -m oura_connect`."""

import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
