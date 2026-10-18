"""Persist only the three network fields; never apply them to the live service."""
import ipaddress
import re
import shutil
import tempfile
import threading
import uuid
from datetime import datetime
from urllib.parse import urlsplit,urlunsplit
from dotenv import dotenv_values
from scripts.workflow.atomic_files import replace_with_retry

_LOCK=threading.Lock()
FIELDS={"api_host":"API_HOST","api_port":"API_PORT","public_url":"PORTAL_PUBLIC_URL"}


def validate(values):
    host=values["api_host"].strip()
    if host!="localhost":
        try:ipaddress.ip_address(host)
        except ValueError:raise ValueError("监听地址须为有效IP或localhost，例如127.0.0.1或0.0.0.0") from None
    port=int(values["api_port"])
    if not 1<=port<=65535:raise ValueError("端口须为1至65535")
    raw=values["public_url"].strip()
    if any(ord(char)<33 for char in raw):raise ValueError("对外访问地址不能包含空白或控制字符")
    try:
        url=urlsplit(raw);hostname=url.hostname or "";external_port=url.port
    except ValueError:raise ValueError("对外访问地址格式或端口无效") from None
    if url.scheme not in {"http","https"} or not hostname or url.username or url.password or url.query or url.fragment or url.path not in {"","/"}:
        raise ValueError("对外地址须为http(s)主机地址，可带端口，不能包含账号、路径、查询或片段")
    if hostname in {"0.0.0.0","::"}:raise ValueError("0.0.0.0/::是监听地址，不能用作对外访问地址")
    try:ipaddress.ip_address(hostname)
    except ValueError:
        try:hostname=hostname.encode("idna").decode("ascii")
        except UnicodeError:raise ValueError("对外访问域名无效") from None
        if not all(re.fullmatch(r"(?!-)[a-zA-Z0-9-]{1,63}(?<!-)",part) for part in hostname.split('.')):raise ValueError("对外访问域名无效")
    if external_port is not None and not 1<=external_port<=65535:raise ValueError("对外地址端口须为1至65535")
    authority=("["+hostname+"]") if ':' in hostname else hostname
    if external_port is not None:authority+=":"+str(external_port)
    return {"api_host":host,"api_port":port,"public_url":urlunsplit((url.scheme,authority,"","",""))}


def configured(path,active):
    values=dotenv_values(path,interpolate=False) if path.is_file() else {}
    return {key:(int(values.get(env) or active[key]) if key=="api_port" else str(values.get(env) or active[key])) for key,env in FIELDS.items()}


def update_environment(path,changes):
    """Keep unrelated lines verbatim; callers supply validated, newline-free values."""
    with _LOCK:
        text=path.read_text(encoding="utf-8-sig") if path.is_file() else ""
        backup=None
        if path.is_file():
            folder=path.parent/"logs/network-settings-backups";folder.mkdir(parents=True,exist_ok=True)
            backup=folder/(datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:8]+".env")
            shutil.copy2(path,backup)
        lines=text.splitlines(keepends=True);pending=dict(changes);output=[]
        pattern=re.compile(r"^\s*(?:export\s+)?([A-Z_]+)\s*=")
        for line in lines:
            match=pattern.match(line)
            if match and match[1] in changes:
                key=match[1]
                if key in pending:output.append(key+"="+str(pending.pop(key))+"\n")
            else:output.append(line)
        if output and not output[-1].endswith(('\n','\r')):output[-1]+="\n"
        output.extend(key+"="+str(value)+"\n" for key,value in pending.items())
        path.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=path.parent,prefix=".env-network-",delete=False) as stream:
            temporary=__import__('pathlib').Path(stream.name);stream.write(''.join(output))
        try:replace_with_retry(temporary,path)
        finally:temporary.unlink(missing_ok=True)
        return backup.name if backup else None


def save(path,values):
    clean=validate(values)
    backup=update_environment(path,{FIELDS[key]:value for key,value in clean.items()})
    return clean,backup
