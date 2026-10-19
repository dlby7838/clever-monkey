"""Fail-closed storage checks. Read-only mounts are the production write barrier."""
import os
from pathlib import Path
import re
import subprocess
import sys


class StorageSafetyError(RuntimeError):
    pass


LOCAL_TYPES = {'apfs', 'hfs', 'hfsplus', 'ext2', 'ext3', 'ext4', 'xfs',
               'btrfs', 'zfs', 'tmpfs', 'overlay', 'f2fs'}


def filesystem_type(path):
    path = Path(path).resolve()
    if sys.platform == 'darwin':
        result = subprocess.run(['/sbin/mount'], capture_output=True, text=True, check=True)
        mounts = []
        for line in result.stdout.splitlines():
            match = re.search(r' on (.+) \(([^, )]+)', line)
            if match:
                mounts.append((Path(match[1]).resolve(), match[2]))
    elif sys.platform.startswith('linux'):
        mounts = []
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            left, right = line.split(' - ', 1)
            name = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), left.split()[4])
            mounts.append((Path(name).resolve(), right.split()[0]))
    else:
        raise StorageSafetyError('Storage safety checks support macOS and Linux only')
    matches = [(mount, kind) for mount, kind in mounts if path.is_relative_to(mount)]
    if not matches:
        raise StorageSafetyError('Cannot determine filesystem; refusing writes')
    return max(matches, key=lambda item: len(item[0].parts))[1]


def local_output(path, protected=(), database=False):
    path = Path(path).absolute()
    # Reject aliases rather than following write destinations into a protected share.
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            # macOS system aliases /var and /tmp are resolved and checked below.
            if str(ancestor) not in ('/var', '/tmp'):
                raise StorageSafetyError(f'Output symlink rejected: {ancestor}')
    resolved = path.resolve()
    for root in protected:
        if resolved.is_relative_to(Path(root).resolve()):
            raise StorageSafetyError('Writes inside material paths are forbidden')
    if filesystem_type(resolved) not in LOCAL_TYPES:
        raise StorageSafetyError('Output must be on a recognized local filesystem')
    if resolved.is_file() and resolved.stat().st_nlink > 1:
        raise StorageSafetyError('Hard-linked output files are forbidden')
    if database:
        for suffix in ('-wal', '-shm', '-journal', '.lock'):
            local_output(Path(str(path) + suffix), protected)
    return resolved


def readonly_source(root, allow_local=False):
    kind = filesystem_type(root)
    # --allow-local only exempts local test data. It never bypasses protection for SMB/NFS.
    if allow_local and kind in LOCAL_TYPES:
        return
    if not (os.statvfs(root).f_flag & os.ST_RDONLY):
        raise StorageSafetyError('NAS must be mounted read-only; refusing a writable source mount')


TARGET_DIRECTORY = '抖音自播'


def validate_scan_scope(root):
    """Network scans may only start inside the configured share-relative video tree."""
    root = Path(root).resolve()
    if filesystem_type(root) in LOCAL_TYPES:
        return  # Local fixtures and copied samples are not NAS inventory roots.
    mount = next((p for p in (root, *root.parents) if os.path.ismount(p)), None)
    if mount is None or mount == Path('/'):
        raise StorageSafetyError('Cannot identify NAS mount boundary')
    target = mount / TARGET_DIRECTORY
    # Reject a target symlink even when its destination happens to be on this share.
    if target.is_symlink() or not root.is_relative_to(target):
        raise StorageSafetyError('NAS scan scope must be inside 抖音自播; share root and other directories are forbidden')
    return target
