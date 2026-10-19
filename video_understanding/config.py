"""Load project-local secrets as plain data, never as shell commands."""
import os
from pathlib import Path

from nas_scanner.safety import local_output

PROJECT_ENV = Path(__file__).resolve().parent.parent / '.env'


def load_local_config(path=None):
    path = Path(path) if path is not None else PROJECT_ENV
    if not path.exists():
        return
    path = local_output(path)
    if path.stat().st_mode & 0o077:
        raise ValueError('Local .env must be accessible only by its owner (chmod 600)')
    settings = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, separator, value = line.partition('=')
        if not separator or name.strip() not in ('ARK_API_KEY', 'ARK_MODEL'):
            raise ValueError('Invalid local configuration entry')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        settings[name.strip()] = value
    for name, value in settings.items():
        if value:
            os.environ.setdefault(name, value)
