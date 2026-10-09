from __future__ import annotations
import hashlib
import hmac
import secrets
import base64
from urllib.parse import urlsplit

def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()

def password_hash(password: str) -> str:
    if not 3 <= len(password) <= 128:
        raise ValueError('密码长度须为 3 至 128 个字符')
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600000)
    return 'pbkdf2-sha256$600000$'+base64.b64encode(salt).decode()+'$'+base64.b64encode(key).decode()

def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2-sha256' or rounds != '600000' or len(password)>128: return False
        key = hashlib.pbkdf2_hmac('sha256',password.encode(),base64.b64decode(salt),int(rounds))
        return hmac.compare_digest(key,base64.b64decode(expected))
    except (ValueError,TypeError): return False

def origin_matches(origin: str, public_url: str) -> bool:
    def parts(value):
        parsed=urlsplit(value)
        return parsed.scheme,parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80)
    try: return parts(origin)==parts(public_url)
    except ValueError: return False
