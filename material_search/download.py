"""Stream original local files as ZIP64 without staging archives or video copies."""
from contextlib import contextmanager, ExitStack, suppress
from datetime import datetime
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import zipfile
from zoneinfo import ZoneInfo


MAX_FILES = 100


def build_plan(db, ids, root, resolve):
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_FILES:
        raise ValueError('请选择1到100条素材')
    if any(isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in ids):
        raise ValueError('素材编号无效')
    plan, seen, names = [], set(), set()
    for analysis_id in dict.fromkeys(ids):
        row = db.execute('SELECT sha256 FROM search_analyses WHERE id=?', (analysis_id,)).fetchone()
        if not row:
            raise ValueError(f'素材 {analysis_id} 不存在')
        if row['sha256'] in seen:
            continue
        path = resolve(db, analysis_id, root)
        metadata = path.stat()
        # Retain folder hierarchy, but make ZIP names safe on common desktop platforms.
        parts = [re.sub(r'[<>:"\\|?*\x00-\x1f]', '_', p).rstrip(' .') or '_'
                 for p in path.relative_to(root).parts]
        name = '/'.join(parts)
        if name in names:
            source = Path(parts[-1]); parts[-1] = f'{source.stem}-{analysis_id}{source.suffix}'
            name = '/'.join(parts)
            while name in names:
                name = '_'+name
        names.add(name); seen.add(row['sha256'])
        plan.append({'id': analysis_id, 'sha256': row['sha256'], 'path': path, 'name': name,
                     'size': metadata.st_size, 'mtime_ns': metadata.st_mtime_ns})
    return plan


@contextmanager
def open_plan(db, plan, root, resolve):
    """Validate every member before HTTP success, pin read-only descriptors until completion."""
    with ExitStack() as stack:
        opened = []
        for item in plan:
            path = resolve(db, item['id'], root)
            if path != item['path']:
                raise ValueError('素材路径已变化，请重新选择下载')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            source = stack.enter_context(os.fdopen(fd, 'rb'))
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_size != item['size'] or metadata.st_mtime_ns != item['mtime_ns']):
                raise ValueError('素材发生变化，请重新选择下载')
            opened.append((item, source))
        yield opened


class ZipSink:
    def __init__(self, stream):
        self.stream = stream
        self.offset = 0
        self.failed = False

    def write(self, data):
        if self.failed:
            raise OSError('ZIP transfer aborted')
        self.stream.write(data)
        self.offset += len(data)
        return len(data)

    def tell(self):
        return self.offset

    def seek(self, *_):
        raise io.UnsupportedOperation('Streaming ZIP')

    def flush(self):
        self.stream.flush()


def stream_archive(opened, output):
    sink = ZipSink(output)
    archive = zipfile.ZipFile(sink, 'w', compression=zipfile.ZIP_STORED, allowZip64=True)
    try:
        for item, source in opened:
            timestamp=datetime.fromtimestamp(item['mtime_ns']/1e9,ZoneInfo('Asia/Shanghai'))
            date_time=timestamp.timetuple()[:6] if 1980 <= timestamp.year <= 2107 else (1980,1,1,0,0,0)
            entry = zipfile.ZipInfo(item['name'],date_time=date_time)
            entry.file_size = item['size']
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            digest = hashlib.sha256()
            remaining = item['size']
            with archive.open(entry, 'w', force_zip64=True) as member:
                while remaining:
                    block = source.read(min(1024*1024, remaining))
                    if not block:
                        raise OSError('素材读取中断')
                    member.write(block); digest.update(block); remaining -= len(block)
            metadata = os.fstat(source.fileno())
            if (metadata.st_size != item['size'] or metadata.st_mtime_ns != item['mtime_ns']
                    or digest.hexdigest() != item['sha256']):
                raise OSError('素材指纹发生变化，下载已中断')
        archive.close()
        output.flush()
    except BaseException:
        # Never finalize a seemingly valid archive when a source changed or transmission failed.
        sink.failed = True
        with suppress(Exception):
            archive.close()
        raise


def archive_filename():
    return 'materials-'+datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')+'.zip'


class ChunkedWriter:
    """HTTP chunk termination is emitted only after the ZIP is fully verified."""
    def __init__(self, output): self.output=output
    def write(self, data):
        if data:
            self.output.write(f'{len(data):X}\r\n'.encode('ascii'))
            self.output.write(data);self.output.write(b'\r\n')
        return len(data)
    def flush(self):self.output.flush()
    def finish(self):self.output.write(b'0\r\n\r\n');self.output.flush()
