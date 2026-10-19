from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from nas_scanner.safety import validate_scan_scope, StorageSafetyError
from nas_scanner.scanner import scan


class ScopeTests(unittest.TestCase):
    def test_network_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            mount = Path(folder).resolve()
            target = mount / '抖音自播'
            target.mkdir()
            child = target / '产品A'
            child.mkdir()
            other = mount / '设计'
            other.mkdir()
            with patch('nas_scanner.safety.filesystem_type', return_value='smbfs'), \
                    patch('os.path.ismount', side_effect=lambda p: Path(p) == mount):
                self.assertEqual(validate_scan_scope(target), target)
                self.assertEqual(validate_scan_scope(child), target)
                for invalid in (mount, other, mount / '抖音自播备份'):
                    with self.subTest(invalid=invalid), self.assertRaises(StorageSafetyError):
                        validate_scan_scope(invalid)
                db = mount / 'should-not-exist' / 'inventory.db'
                with self.assertRaises(StorageSafetyError):
                    scan(other, db)
                self.assertFalse(db.parent.exists())

    def test_target_symlink_cannot_escape_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            mount = Path(folder).resolve()
            other = mount / '设计'
            other.mkdir()
            target = mount / '抖音自播'
            target.symlink_to(other, target_is_directory=True)
            with patch('nas_scanner.safety.filesystem_type', return_value='smbfs'), \
                    patch('os.path.ismount', side_effect=lambda p: Path(p) == mount):
                with self.assertRaises(StorageSafetyError):
                    validate_scan_scope(target)

    def test_only_video_files_are_registered(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / 'materials'
            root.mkdir()
            for name in ('clip.MP4', 'notes.txt', 'cover.png', 'sheet.xlsx'):
                (root / name).write_bytes(b'fixture')
            result = scan(root, base / 'db', allow_local=True, file_delay=0, directory_delay=0)
            self.assertEqual(result['pending'], 1)
