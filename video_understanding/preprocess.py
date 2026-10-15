"""Disposable analysis proxies; input videos are never deleted or overwritten."""
from contextlib import contextmanager
from pathlib import Path
import subprocess
import tempfile

PROFILE = 'h264-fit1280x720-crf24-noaudio-v1'


@contextmanager
def compressed_video(source, ffmpeg='ffmpeg', timeout=180, temp_dir=None):
    # Orientation-aware bounding box; preserve aspect ratio and never upscale.
    scale = ("scale=w='if(gte(iw,ih),min(1280,iw),min(720,iw))':"
             "h='if(gte(iw,ih),min(720,ih),min(1280,ih))':"
             "force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1")
    with tempfile.TemporaryDirectory(prefix='ai-video-p1-', dir=temp_dir) as folder:
        output = Path(folder) / 'analysis.mp4'
        subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-threads', '1', '-i', str(source),
                        '-map', '0:v:0', '-an', '-sn', '-dn', '-vf', scale,
                        '-c:v', 'libx264', '-threads', '1', '-preset', 'veryfast',
                        '-crf', '24', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                        '-y', str(output)], check=True, capture_output=True, timeout=timeout)
        if not output.is_file() or not 0 < output.stat().st_size <= 40 * 1024 * 1024:
            raise ValueError('Compressed video must be nonempty and <=40 MiB')
        yield output
