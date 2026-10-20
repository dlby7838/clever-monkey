"""Offline commands; no API credentials, network calls or source video reads."""
import argparse
import json
from pathlib import Path
from .store import connect, import_results, search
from nas_scanner.safety import local_output


def main():
    parser = argparse.ArgumentParser(description='P2 本地素材导入与关键词搜索')
    parser.add_argument('--db', type=Path, default=Path('data/materials.sqlite'))
    commands = parser.add_subparsers(dest='command', required=True)
    imp = commands.add_parser('import')
    imp.add_argument('--comparison', type=Path, required=True)
    imp.add_argument('--run', type=Path, required=True)
    imp.add_argument('--manifest', type=Path, required=True)
    find = commands.add_parser('search')
    find.add_argument('query')
    find.add_argument('--limit', type=int, default=10)
    find.add_argument('--subject', default='')
    find.add_argument('--action', default='')
    find.add_argument('--review', choices=['needs_review', 'not_reviewed'])
    find.add_argument('--min-duration', type=float)
    find.add_argument('--max-duration', type=float)
    find.add_argument('--include-ocr', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'import':
            manifest = json.loads(args.manifest.read_text())
            local_output(args.db, protected=[manifest['source_root'], args.manifest.parent, args.comparison, args.run], database=True)
        db = connect(args.db)
        try:
            if args.command == 'import':
                result = import_results(db, json.loads(args.comparison.read_text()),
                                        json.loads(args.run.read_text()), manifest, args.manifest.parent)
            else:
                result = search(db, args.query, args.limit, args.subject, args.action, args.review,
                                args.min_duration, args.max_duration, args.include_ocr)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            db.close()
    except (ValueError, OSError) as error:
        parser.exit(2, f'{error}\n')


if __name__ == '__main__':
    main()
