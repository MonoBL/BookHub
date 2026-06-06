#!/usr/bin/env python3
"""
Converter worker entrypoint. Runs inside the gVisor-sandboxed worker container.
Full implementation in M6.

Usage: python run_convert.py <in.pdf> <out.epub> --title <title> --author <author>
"""
import sys

if __name__ == "__main__":
    print("Converter worker not yet implemented (M6)", file=sys.stderr)
    sys.exit(1)
