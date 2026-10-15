from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from nas_scanner.scanner import ScanError, scan


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
            with self.assertRaises(ScanError):
                self.scan(stable_seconds=0)
        self.assertEqual(self.rows()[0][1], 'ready')

    def test_probe_failure_rolls_back(self):
        self.scan(stable_seconds=0)
        (self.root / 'bad.mp4').write_bytes(b'bad')
        self.probe.side_effect = ScanError('bad video')
        with self.assertRaises(ScanError):
            self.scan(stable_seconds=0)
        self.assertEqual(len(self.rows()), 1)

    def test_mount_guard_and_database_guard(self):
        with self.assertRaises(ScanError):
            scan(self.root, self.db)
        with self.assertRaises(ScanError):
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
        with self.assertRaises(ScanError):
            self.scan(stable_seconds=0, marker='identity.txt')
        self.assertEqual(self.rows(), [])

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
