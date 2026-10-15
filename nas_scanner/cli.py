import argparse
import json
from pathlib import Path
import sys
import sqlite3
from .scanner import ScanError, scan


def main():
    parser = argparse.ArgumentParser(description='Read-only NAS video scanner (SQLite)')
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--database', type=Path, default=Path('data/inventory.sqlite3'))
    parser.add_argument('--stable-seconds', type=float, default=30)
    parser.add_argument('--ffprobe', default='ffprobe')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--marker', help='Existing identity file relative to root, read only')
    parser.add_argument('--allow-local', action='store_true', help='Explicitly allow unmounted local test directories')
    parser.add_argument('--read-mib-per-second', type=float, default=5,
                        help='Video read rate in MiB/s; 0 disables throttling (default: 5)')
    parser.add_argument('--file-delay', type=float, default=0.1)
    parser.add_argument('--directory-delay', type=float, default=0.02)
    parser.add_argument('--max-files', type=int, default=20,
                        help='Maximum videos hashed per run; 0 means unlimited')
    parser.add_argument('--rehash', action='store_true', help='Verify unchanged ready files too')
    parser.add_argument('--cache-dir', type=Path, help='Local temporary video directory')
    args = parser.parse_args()
    try:
        print(json.dumps(scan(**vars(args)), ensure_ascii=False))
        return 0
    except (ScanError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
