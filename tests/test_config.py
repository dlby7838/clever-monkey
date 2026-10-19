import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from video_understanding.config import load_local_config


class ConfigTests(unittest.TestCase):
    def test_load_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('ARK_API_KEY=test-only\nARK_MODEL="test-model"\n')
            path.chmod(0o600)
            with patch.dict(os.environ, {}, clear=True):
                load_local_config(path)
                self.assertEqual(os.environ['ARK_API_KEY'], 'test-only')
                self.assertEqual(os.environ['ARK_MODEL'], 'test-model')
            with patch.dict(os.environ, {'ARK_API_KEY': 'existing'}, clear=True):
                load_local_config(path)
                self.assertEqual(os.environ['ARK_API_KEY'], 'existing')

    def test_reject_public_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('ARK_API_KEY=test-only\n')
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_local_config(path)
