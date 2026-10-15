from nas_scanner.safety import StorageSafetyError
from contextlib import closing
import fcntl
import io
import os
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from nas_scanner.scanner import ScanError, scan, read_video


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'materials'
        self.root.mkdir()
        self.db = self.base / 'inventory.db'
        self.video = self.root / 'a.mp4'
        self.video.write_bytes(b'video fixture')
        self.mock = patch('nas_scanner.scanner.probe', return_value={'duration': '1', 'width': 16, 'height': 16})
        self.probe = self.mock.start()
        self.addCleanup(self.mock.stop)
        self.addCleanup(self.temp.cleanup)

    def scan(self, **kwargs):
        return scan(self.root, self.db, allow_local=True, **kwargs)

    def rows(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            return db.execute('SELECT path,status,sha256 FROM files ORDER BY path').fetchall()

    def test_stability_requires_two_observations(self):
        self.assertEqual(self.scan(stable_seconds=30)['pending'], 1)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE files SET stable_since=stable_since-31')
        self.assertEqual(self.scan()['ready'], 1)
        self.probe.assert_called_once()

    def test_change_resets_stability(self):
        self.scan(stable_seconds=0)
        self.video.write_bytes(b'longer changed video')
        self.assertEqual(self.scan()['pending'], 1)
        self.assertIsNone(self.rows()[0][2])

    def test_deduplicate_and_move(self):
        self.scan(stable_seconds=0)
        self.video.rename(self.root / 'renamed.mp4')
        self.assertEqual(self.scan(stable_seconds=0)['moved'], 1)
        shutil.copyfile(self.root / 'renamed.mp4', self.root / 'copy.mp4')
        self.assertEqual(self.scan(stable_seconds=0)['duplicates'], 1)
        self.probe.assert_called_once()

    def test_move_after_stability_window(self):
        self.scan(stable_seconds=0)
        self.video.rename(self.root / 'renamed.mp4')
        self.assertEqual(self.scan()['pending'], 1)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE files SET stable_since=stable_since-31')
        self.assertEqual(self.scan()['moved'], 1)
        self.assertEqual(self.scan()['moved'], 0)

    def test_missing(self):
        self.scan(stable_seconds=0)
        self.video.unlink()
        self.assertEqual(self.scan(stable_seconds=0)['missing'], 1)
        self.assertEqual(self.scan(stable_seconds=0)['missing'], 0)

    def test_disconnect_rolls_back(self):
        self.scan(stable_seconds=0)
        self.video.unlink()
        with patch('nas_scanner.scanner.os.walk', side_effect=OSError('disconnected')):
            with self.assertRaises((ScanError, StorageSafetyError)):
                self.scan(stable_seconds=0)
        self.assertEqual(self.rows()[0][1], 'ready')

    def test_probe_failure_rolls_back(self):
        self.scan(stable_seconds=0)
        (self.root / 'bad.mp4').write_bytes(b'bad')
        self.probe.side_effect = ScanError('bad video')
        with self.assertRaises((ScanError, StorageSafetyError)):
            self.scan(stable_seconds=0)
        self.assertEqual(len(self.rows()), 1)

    def test_mount_guard_and_database_guard(self):
        with self.assertRaises((ScanError, StorageSafetyError)):
            scan(self.root, self.db)
        with self.assertRaises((ScanError, StorageSafetyError)):
            scan(self.root, self.root / 'db', allow_local=True)

    def test_symlinks_ignored(self):
        (self.root / 'link.mp4').symlink_to(self.video)
        self.assertEqual(self.scan(stable_seconds=0)['ready'], 1)

    def test_marker_disappearance_rolls_back(self):
        marker = self.root / 'identity.txt'
        marker.write_text('test share')
        def remove_marker(*args):
            marker.unlink()
            return {}
        self.probe.side_effect = remove_marker
        with self.assertRaises((ScanError, StorageSafetyError)):
            self.scan(stable_seconds=0, marker='identity.txt')
        self.assertEqual(self.rows(), [])

    def test_unchanged_files_do_not_read_video(self):
        self.scan(stable_seconds=0)
        with patch('nas_scanner.scanner.read_video', side_effect=AssertionError('unexpected read')):
            result = self.scan(stable_seconds=0)
        self.assertEqual(result['reused'], 1)
        self.assertEqual(result['bytes_read'], 0)

    def test_budget_continues_next_run_without_missing(self):
        (self.root / 'b.mp4').write_bytes(b'second video')
        result = self.scan(stable_seconds=0, max_files=1)
        self.assertEqual(result['hashed'], 1)
        self.assertEqual(result['deferred'], 1)
        self.assertEqual(result['missing'], 0)
        result = self.scan(stable_seconds=0, max_files=1)
        self.assertEqual(result['hashed'], 1)
        self.assertEqual(result['ready'], 2)

    def test_rehash_detects_preserved_mtime_replacement(self):
        self.scan(stable_seconds=0)
        original_sha = self.rows()[0][2]
        stat = self.video.stat()
        self.video.write_bytes(b'X' * stat.st_size)
        os.utime(self.video, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(self.scan(stable_seconds=0)['reused'], 1)
        self.assertEqual(self.scan(stable_seconds=0, rehash=True)['hashed'], 1)
        self.assertNotEqual(self.rows()[0][2], original_sha)

    def test_probe_reads_local_copy_and_removes_it(self):
        cache = self.base / 'cache'
        self.scan(stable_seconds=0, cache_dir=cache)
        local_path = self.probe.call_args.args[0]
        self.assertEqual(local_path.parent, cache.resolve())
        self.assertFalse(local_path.exists())
        self.assertEqual(list(cache.iterdir()), [])

    def test_read_pacing_and_unlimited(self):
        self.video.write_bytes(b'x' * (512 * 1024))
        with patch('nas_scanner.scanner.time.monotonic', return_value=0), \
                patch('nas_scanner.scanner.time.sleep') as sleep:
            _, size = read_video(self.video, io.BytesIO(), 1)
        self.assertEqual(size, 512 * 1024)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.25, 0.25])
        with patch('nas_scanner.scanner.time.sleep') as sleep:
            read_video(self.video, io.BytesIO(), 0)
        sleep.assert_not_called()

    def test_single_instance(self):
        with Path(str(self.db) + '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ScanError, 'Another scan'):
                self.scan()
        self.assertEqual(self.scan(stable_seconds=0)['ready'], 1)

    def test_invalid_load_settings(self):
        for settings in ({'read_mib_per_second': -1}, {'file_delay': float('nan')},
                         {'max_files': -1}, {'cache_dir': self.root / 'cache'}):
            with self.subTest(settings=settings), self.assertRaises((ScanError, StorageSafetyError)):
                self.scan(**settings)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools unavailable')
    def test_real_ffprobe(self):
        self.mock.stop()
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'color=c=black:s=32x32:d=0.2', '-c:v', 'mpeg4',
                        '-y', str(self.video)], check=True)
        self.assertEqual(self.scan(stable_seconds=0)['ready'], 1)
        with closing(sqlite3.connect(self.db)) as db, db:
            metadata = json.loads(db.execute('SELECT metadata FROM contents').fetchone()[0])
        self.assertEqual(metadata['width'], 32)
        self.assertGreater(float(metadata['duration']), 0)
