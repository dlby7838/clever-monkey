from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from nas_scanner.safety import StorageSafetyError, local_output, readonly_source
from nas_scanner.scanner import scan
from video_understanding.pipeline import analyze
from video_understanding.preprocess import compressed_video


class SafetyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.source = self.base / 'nas'
        self.source.mkdir()
        self.video = self.source / 'original.mp4'
        self.video.write_bytes(b'original')

    def test_all_writes_in_source_rejected_before_creation(self):
        for destination in (self.source / 'inventory.db', self.source / 'nested' / 'inventory.db'):
            with self.assertRaises(StorageSafetyError):
                scan(self.source, destination, allow_local=True)
            self.assertFalse(destination.exists())
        self.assertEqual(list(self.source.iterdir()), [self.video])

    def test_remote_and_unknown_outputs_rejected(self):
        for kind in ('smbfs', 'cifs', 'nfs', 'unknown'):
            with patch('nas_scanner.safety.filesystem_type', return_value=kind):
                with self.assertRaises(StorageSafetyError):
                    local_output(self.base / 'output')

    def test_alias_and_sidecar_rejected(self):
        link = self.base / 'alias'
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(StorageSafetyError):
            local_output(link / 'output')
        db = self.base / 'local.db'
        Path(str(db) + '-wal').symlink_to(self.video)
        with self.assertRaises(StorageSafetyError):
            local_output(db, database=True)
        self.assertFalse(db.exists())

    def test_hardlinked_output_rejected(self):
        output = self.base / 'linked.db'
        os.link(self.video, output)
        with self.assertRaises(StorageSafetyError):
            local_output(output)

    def test_allow_local_cannot_bypass_writable_smb(self):
        flags = type('Flags', (), {'f_flag': 0})()
        with patch('nas_scanner.safety.filesystem_type', return_value='smbfs'), patch('os.statvfs', return_value=flags):
            with self.assertRaises(StorageSafetyError):
                readonly_source(self.source, allow_local=True)
        flags.f_flag = os.ST_RDONLY
        with patch('nas_scanner.safety.filesystem_type', return_value='smbfs'), patch('os.statvfs', return_value=flags):
            readonly_source(self.source)

    def test_compression_and_analysis_remote_temp_rejected(self):
        with patch('nas_scanner.safety.filesystem_type', return_value='cifs'):
            with self.assertRaises(StorageSafetyError):
                analyze(self.video, self.base / 'p1.db')
            with self.assertRaises(StorageSafetyError):
                with compressed_video(self.video, temp_dir=self.source):
                    self.fail('Remote temporary output allowed')
        self.assertEqual(self.video.read_bytes(), b'original')
