from __future__ import annotations
import secrets
import uuid
import unicodedata
from psycopg.errors import UniqueViolation
from datetime import datetime, timedelta, timezone
from pathlib import Path
from corpus_registry.db import connect
from corpus_registry.config import DatabaseSettings
from .security import digest, password_hash, password_matches

def normalized_project_name(name):
    return ' '.join(unicodedata.normalize('NFKC',name).split())

def project_name_key(name):
    return normalized_project_name(name).casefold()

class PortalStore:
    def __init__(self, settings=None):
        self.settings=settings
    def initialize(self):
        with connect(self.settings) as c:
            c.execute(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8-sig'))
            c.execute('SELECT id FROM corpus_portal.users ORDER BY id FOR UPDATE')
            rows=c.execute('SELECT id,owner_id,name,name_key FROM corpus_portal.projects ORDER BY created_at,id FOR UPDATE').fetchall()
            used={(str(row['owner_id']),row['name_key']) for row in rows if row['name_key'] is not None}
            for row in rows:
                key=project_name_key(row['name']);identity=(str(row['owner_id']),key)
                if row['name_key'] is None and identity not in used:
                    c.execute('UPDATE corpus_portal.projects SET name_key=%s WHERE id=%s',(key,row['id']))
                    used.add(identity)
    def has_users(self):
        with connect(self.settings) as c:
            return bool(c.execute("SELECT EXISTS(SELECT 1 FROM corpus_portal.users WHERE active) AS present").fetchone()["present"])
    def add_user(self, username, password):
        username=username.strip().lower()
        if not 3<=len(username)<=64 or not all(x.isascii() and (x.isalnum() or x in '_-.') for x in username):
            raise ValueError('账号使用 3 至 64 位英文字母、数字或 _-.')
        uid=uuid.uuid4()
        with connect(self.settings) as c:
            c.execute('INSERT INTO corpus_portal.users(id,username,password_hash) VALUES (%s,%s,%s)',(uid,username,password_hash(password)))
        return uid
    def login(self, username, password, peer):
        # Both account and peer throttles persist across restarts and workers.
        now=datetime.now(timezone.utc)
        keys=[digest('name:'+username.strip().lower()),digest('peer:'+peer)]
        with connect(self.settings) as c:
            for key in keys:
                c.execute('INSERT INTO corpus_portal.login_attempts(key_hash) VALUES (%s) ON CONFLICT DO NOTHING',(key,))
            rows=list(c.execute('SELECT * FROM corpus_portal.login_attempts WHERE key_hash=ANY(%s) ORDER BY key_hash FOR UPDATE',(keys,)).fetchall())
            for row in rows:
                if now-row['window_start']>=timedelta(minutes=15):
                    c.execute('UPDATE corpus_portal.login_attempts SET failures=0,window_start=%s WHERE key_hash=%s',(now,row['key_hash']))
                elif row['failures']>=10:
                    return None
            row=c.execute('SELECT * FROM corpus_portal.users WHERE username=%s AND active', (username.strip().lower(),)).fetchone()
            valid=row is not None and password_matches(password,row['password_hash'])
            if not valid:
                # Equalize an unknown account with a real password-hash operation.
                if row is None: password_matches(password,'pbkdf2-sha256$600000$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
                c.execute('UPDATE corpus_portal.login_attempts SET failures=failures+1 WHERE key_hash=ANY(%s)',(keys,))
                return None
            c.execute('UPDATE corpus_portal.login_attempts SET failures=0 WHERE key_hash=%s',(keys[0],))
            token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
            c.execute("INSERT INTO corpus_portal.sessions(token_hash,user_id,csrf_token,expires_at) VALUES (%s,%s,%s,'infinity'::timestamptz)",(digest(token),row['id'],csrf))
            return token,csrf
    def session(self, token):
        with connect(self.settings) as c:
            return c.execute('SELECT u.id,u.username,s.csrf_token FROM corpus_portal.sessions s JOIN corpus_portal.users u ON u.id=s.user_id WHERE s.token_hash=%s AND s.expires_at>now() AND u.active',(digest(token),)).fetchone()
    def logout(self, token):
        with connect(self.settings) as c:
            c.execute('DELETE FROM corpus_portal.sessions WHERE token_hash=%s',(digest(token),))
    def list_projects(self, owner):
        with connect(self.settings) as c:
            return list(c.execute('SELECT * FROM corpus_portal.projects WHERE owner_id=%s ORDER BY created_at',(owner,)).fetchall())
    def project(self, owner, project_id):
        with connect(self.settings) as c:
            return c.execute('SELECT * FROM corpus_portal.projects WHERE id=%s AND owner_id=%s',(project_id,owner)).fetchone()
    def add_project(self, owner, pid, name):
        name=normalized_project_name(name)
        if not name or len(name)>120 or '\x00' in name: raise ValueError('项目名称无效')
        key=project_name_key(name)
        try:
            with connect(self.settings) as c:
                # The owner row lock serializes duplicate checks and quota checks.
                c.execute('SELECT id FROM corpus_portal.users WHERE id=%s FOR UPDATE',(owner,))
                rows=c.execute('SELECT name FROM corpus_portal.projects WHERE owner_id=%s',(owner,)).fetchall()
                if any(project_name_key(row['name'])==key for row in rows):
                    raise ValueError('已有同名语料库项目，请重新命名')
                if len(rows)>=5: raise ValueError('每个账号当前最多创建 5 个项目')
                c.execute('INSERT INTO corpus_portal.projects(id,owner_id,name,name_key) VALUES (%s,%s,%s,%s)',(pid,owner,name,key))
        except UniqueViolation as exc:
            raise ValueError('已有同名语料库项目，请重新命名') from exc
    def add_share(self, owner, project_id, key, version, checksum, expires=None):
        if not self.project(owner,project_id): raise PermissionError('项目不存在或不可访问')
        sid,token=uuid.uuid4(),secrets.token_urlsafe(32)
        with connect(self.settings) as c:
            c.execute('INSERT INTO corpus_portal.shares(id,project_id,token_hash,corpus_key,version,checksum_sha256,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',(sid,project_id,digest(token),key,version,checksum,expires))
        return sid,token
    def share(self, token):
        with connect(self.settings) as c:
            return c.execute('SELECT * FROM corpus_portal.shares WHERE token_hash=%s AND active AND (expires_at IS NULL OR expires_at>now())',(digest(token),)).fetchone()
    def list_shares(self, owner, project_id):
        if not self.project(owner,project_id): raise PermissionError('项目不存在或不可访问')
        with connect(self.settings) as c:
            return list(c.execute('SELECT id,corpus_key,version,active,created_at,expires_at FROM corpus_portal.shares WHERE project_id=%s ORDER BY created_at',(project_id,)).fetchall())
    def revoke_share(self, owner, sid):
        with connect(self.settings) as c:
            row=c.execute('UPDATE corpus_portal.shares s SET active=FALSE FROM corpus_portal.projects p WHERE s.project_id=p.id AND p.owner_id=%s AND s.id=%s RETURNING s.id',(owner,sid)).fetchone()
            return row is not None
