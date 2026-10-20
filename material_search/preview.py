"""Diskless fragmented MP4 preview. Process lifetime follows the HTTP response."""
from contextlib import contextmanager
import math
import subprocess
import sys
import threading

SCALE = ("scale=w='if(gte(iw,ih),min(854,iw),min(480,iw))':"
         "h='if(gte(iw,ih),min(480,ih),min(854,ih))':"
         "force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps=24")


def hardware_available(ffmpeg='ffmpeg'):
    if sys.platform != 'darwin':
        return False
    try:
        result = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                                 'color=s=64x64:d=0.04', '-an', '-c:v', 'h264_videotoolbox',
                                 '-f', 'null', '-'], capture_output=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def command(source, start=0, hardware=False, ffmpeg='ffmpeg'):
    if not math.isfinite(start) or start < 0:
        raise ValueError('Invalid preview start time')
    args = [ffmpeg, '-nostdin', '-v', 'error']
    if hardware:
        args += ['-hwaccel', 'videotoolbox']
    args += ['-threads', '2', '-ss', str(start), '-i', str(source), '-map', '0:v:0',
             '-an', '-sn', '-dn', '-vf', SCALE]
    if hardware:
        args += ['-c:v', 'h264_videotoolbox', '-b:v', '1200k']
    else:
        args += ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28', '-threads', '2']
    args += ['-pix_fmt', 'yuv420p', '-g', '12', '-movflags',
             'frag_keyframe+empty_moov+default_base_moof', '-frag_duration', '500000',
             '-f', 'mp4', 'pipe:1']
    return args


@contextmanager
def stream_preview(source, start=0, hardware=False, timeout=180):
    """Read first bytes before sending HTTP success; hardware failure retries software once."""
    process = None
    timer = None
    try:
        for accelerated in ([True, False] if hardware else [False]):
            process = subprocess.Popen(command(source, start, accelerated), stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
            timer = threading.Timer(timeout, process.kill)
            timer.daemon = True
            timer.start()
            first = process.stdout.read1(8192)
            if first:
                break
            process.wait(timeout=5)
            timer.cancel()
            process.stdout.close()
        if not first:
            raise ValueError('无法生成预览，请检查本地视频或FFmpeg')
        yield first, process.stdout
    finally:
        if timer:
            timer.cancel()
        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            if process.stdout:
                process.stdout.close()
