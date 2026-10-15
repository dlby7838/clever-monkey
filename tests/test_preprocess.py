import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from nas_scanner.scanner import probe
from video_understanding.preprocess import compressed_video
from video_understanding.pipeline import analyze


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools unavailable')
class CompressionTests(unittest.TestCase):
    def test_dimensions_and_cleanup_on_success_and_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for width, height, expected in [(1920, 1080, (1280, 720)), (1080, 1920, (720, 1280)), (320, 240, (320, 240))]:
                source = root / 'original.mp4'
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                                f'color=c=blue:s={width}x{height}:d=0.2', '-c:v', 'mpeg4', '-y', str(source)], check=True)
                before = source.read_bytes()
                with compressed_video(source, temp_dir=root) as proxy:
                    metadata = probe(proxy, 'ffprobe', 30)
                    self.assertEqual((metadata['width'], metadata['height']), expected)
                    self.assertFalse(any(s['codec_type'] == 'audio' for s in metadata['raw']['streams']))
                self.assertFalse(proxy.exists())
                self.assertEqual(source.read_bytes(), before)
            with self.assertRaisesRegex(RuntimeError, 'analysis failed'):
                with compressed_video(source, temp_dir=root) as proxy:
                    raise RuntimeError('analysis failed')
            self.assertFalse(proxy.exists())
            result = analyze(source, root / 'p1.db', temp_dir=root)
            self.assertIn('compression', result)
            self.assertEqual(list(root.glob('ai-video-p1-*')), [])
            self.assertTrue(analyze(source, root / 'p1.db', temp_dir=root)['cached'])

    def test_invalid_input_cleans_temporary_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bad = root / 'bad.mp4'
            bad.write_bytes(b'not video')
            with self.assertRaises(subprocess.CalledProcessError):
                with compressed_video(bad, temp_dir=root):
                    self.fail('Invalid input accepted')
            self.assertEqual(list(root.glob('ai-video-p1-*')), [])
