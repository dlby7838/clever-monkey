import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from nas_scanner.safety import StorageSafetyError
from nas_scanner.scanner import ScanError
from .config import load_local_config
from .pipeline import AnalysisError, analyze


def main():
    try:
        load_local_config()
    except (ValueError, OSError, StorageSafetyError):
        print('Unable to load local .env; check format, local path and owner-only permissions', file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description='P1 local sample video understanding; mock by default')
    parser.add_argument('--video', type=Path, required=True, help='Stable local sample, not a NAS path')
    parser.add_argument('--database', type=Path, default=Path('data/p1.sqlite3'))
    parser.add_argument('--provider', choices=['mock', 'ark'], default='mock')
    parser.add_argument('--model', default=os.environ.get('ARK_MODEL', ''))
    parser.add_argument('--fps', type=float, default=1.0)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--max-duration', type=float, default=30)
    parser.add_argument('--ffprobe', default='ffprobe')
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--compression-timeout', type=float, default=180)
    parser.add_argument('--temp-dir', type=Path, help='Existing local temporary directory')
    try:
        print(json.dumps(analyze(**vars(parser.parse_args())), ensure_ascii=False, indent=2))
        return 0
    except (AnalysisError, ScanError, StorageSafetyError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
