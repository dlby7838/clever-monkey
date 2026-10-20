"""Local indexing/search; model download only on the explicit index command."""
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
    find.add_argument('--mode', choices=['keyword', 'semantic', 'hybrid'], default='hybrid')
    find.add_argument('--model-cache', type=Path, default=Path('data/models'))
    find.add_argument('--limit', type=int, default=10)
    find.add_argument('--subject', default='')
    find.add_argument('--action', default='')
    find.add_argument('--review', choices=['needs_review', 'not_reviewed'])
    find.add_argument('--min-duration', type=float)
    find.add_argument('--max-duration', type=float)
    find.add_argument('--include-ocr', action='store_true')
    web = commands.add_parser('serve', help='启动本地检索与预览页面')
    web.add_argument('--media-root', type=Path, required=True, help='允许预览的本地视频目录，不支持NAS')
    web.add_argument('--port', type=int, default=4174)
    web.add_argument('--model-cache', type=Path, default=Path('data/models'))
    build = commands.add_parser('index', help='准备本地中文模型并增量建立向量索引')
    build.add_argument('--model-cache', type=Path, default=Path('data/models'))
    args = parser.parse_args()
    try:
        if args.command == 'serve':
            from .web import serve
            serve(args.db, args.media_root, args.port, args.model_cache)
            return
        if args.command == 'import':
            manifest = json.loads(args.manifest.read_text())
            local_output(args.db, protected=[manifest['source_root'], args.manifest.parent, args.comparison, args.run], database=True)
        db = connect(args.db)
        try:
            if args.command == 'import':
                result = import_results(db, json.loads(args.comparison.read_text()),
                                        json.loads(args.run.read_text()), manifest, args.manifest.parent)
            elif args.command == 'index':
                from .semantic import LocalEncoder, index
                result = index(db, LocalEncoder(args.model_cache, download=True))
            else:
                from .semantic import LocalEncoder, retrieve
                encoder = LocalEncoder(args.model_cache) if args.mode != 'keyword' and args.query.strip() else None
                result = retrieve(db, encoder, args.query, args.limit, args.mode, subject=args.subject,
                                  action=args.action, review=args.review, min_duration=args.min_duration,
                                  max_duration=args.max_duration, include_ocr=args.include_ocr)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            db.close()
    except (ValueError, OSError) as error:
        parser.exit(2, f'{error}\n')


if __name__ == '__main__':
    main()
