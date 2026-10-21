"""Optional private IPv4 access, password bootstrap and signed short-lived sessions."""
import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
import secrets
import time
from http.cookies import SimpleCookie, CookieError

from nas_scanner.safety import local_output

NETWORKS = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
COOKIE = 'cm_session'
TTL = 8 * 60 * 60


def private_ipv4(value, loopback=False):
    try:
        ip = ipaddress.IPv4Address(value)
        return (loopback and ip.is_loopback) or any(ip in network for network in NETWORKS)
    except ipaddress.AddressValueError:
        return False


def password_file(path, protected=()):
    path = local_output(Path(path), protected=protected)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.stat().st_mode & 0o077:
            raise ValueError('访问口令文件权限须为600')
    else:
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(12)+'\n')
    password = path.read_text().strip()
    if not 12 <= len(password) <= 256:
        raise ValueError('访问口令长度须为12到256字符')
    return password


def session_cookie(secret):
    payload = f'{int(time.time())+TTL}.{secrets.token_hex(12)}'
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f'{COOKIE}={payload}.{signature}; Path=/; Max-Age={TTL}; HttpOnly; SameSite=Strict'


def valid_session(header, secret):
    try:
        cookies = SimpleCookie(); cookies.load(header or '')
        value = cookies[COOKIE].value
        expiry, nonce, signature = value.split('.')
        payload = f'{expiry}.{nonce}'
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return int(time.time()) < int(expiry) <= int(time.time())+TTL and hmac.compare_digest(signature, expected)
    except (ValueError, KeyError, CookieError, TypeError):
        return False
