"""Deploy Roomprint into Home Assistant via Samba (same pattern as
~/workspace/ha-gate/write-scripts-samba.py).

Uploads custom_components/roomprint/* to the HA config share and reads
each file back to verify it landed byte-identical. Run with the HA venv:
    ~/workspace/venvs/ha/bin/python deploy-samba.py
Deploying files alone does NOT activate the integration — it loads when
added in Settings -> Devices & Services.
"""
import json
import os
import subprocess
import time
import urllib.parse
import uuid

import websocket
from smbprotocol.connection import Connection
from smbprotocol.exceptions import SMBResponseException
from smbprotocol.open import (
    CreateDisposition,
    CreateOptions,
    FileAttributes,
    FilePipePrinterAccessMask,
    ImpersonationLevel,
    Open,
    ShareAccess,
)
from smbprotocol.session import Session
from smbprotocol.tree import TreeConnect

HA_HOST = "100.126.134.90"
TOKEN_FILE = os.path.expanduser("~/.config/maple/ha-livingroom-token")
PW_FILE = os.path.expanduser("~/.config/maple/ha-samba-password")
LOCAL_PORT = 44448
SRC = os.path.expanduser("~/workspace/bluetooth-room-positioning/integration/custom_components/roomprint")

FILES = [
    "__init__.py",
    "config_flow.py",
    "const.py",
    "manifest.json",
    "matcher.py",
    "sensor.py",
    "simulate.py",
    "util.py",
    "websocket.py",
    "frontend/roomprint-panel.js",
    "translations/en.json",
]
DIRS = ["custom_components", "custom_components/roomprint",
        "custom_components/roomprint/frontend",
        "custom_components/roomprint/translations"]


def mkdir(tree, name):
    d = Open(tree, name)
    try:
        d.create(
            ImpersonationLevel.Impersonation,
            FilePipePrinterAccessMask.GENERIC_WRITE,
            FileAttributes.FILE_ATTRIBUTE_DIRECTORY,
            ShareAccess.FILE_SHARE_READ,
            CreateDisposition.FILE_CREATE,
            CreateOptions.FILE_DIRECTORY_FILE,
        )
    except SMBResponseException as exc:
        # Already exists is fine; anything else is a real error.
        if "STATUS_OBJECT_NAME_COLLISION" not in str(exc):
            raise
    finally:
        d.close()


def write_file(tree, name, data):
    fo = Open(tree, name)
    try:
        fo.create(
            ImpersonationLevel.Impersonation,
            FilePipePrinterAccessMask.GENERIC_WRITE,
            FileAttributes.FILE_ATTRIBUTE_NORMAL,
            ShareAccess.FILE_SHARE_READ,
            CreateDisposition.FILE_OVERWRITE_IF,
            CreateOptions.FILE_NON_DIRECTORY_FILE,
        )
        fo.write(data, 0)
    finally:
        fo.close()


def read_file(tree, name):
    fo = Open(tree, name)
    try:
        fo.create(
            ImpersonationLevel.Impersonation,
            FilePipePrinterAccessMask.GENERIC_READ,
            FileAttributes.FILE_ATTRIBUTE_NORMAL,
            ShareAccess.FILE_SHARE_READ,
            CreateDisposition.FILE_OPEN,
            CreateOptions.FILE_NON_DIRECTORY_FILE,
        )
        size = fo.end_of_file
        return fo.read(0, size)
    finally:
        fo.close()


def main():
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    with open(PW_FILE) as f:
        samba_pw = f.read().strip()
    p = urllib.parse.urlparse(os.environ["HTTPS_PROXY"])
    proxy_host, proxy_user, proxy_pass = p.hostname, p.username, p.password

    # Reachability proof: HA websocket auth must succeed first.
    ws = websocket.create_connection(
        f"ws://{HA_HOST}/api/websocket",
        http_proxy_host=proxy_host, http_proxy_port=3130,
        http_proxy_auth=(proxy_user, proxy_pass), timeout=30,
    )
    assert json.loads(ws.recv())["type"] == "auth_required"
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    assert json.loads(ws.recv())["type"] == "auth_ok"
    ws.close()
    print("HA websocket auth ok")

    socat = subprocess.Popen([
        "socat",
        f"TCP-LISTEN:{LOCAL_PORT},bind=127.0.0.1,reuseaddr,fork",
        f"PROXY:{proxy_host}:{HA_HOST}:445,proxyport=3130,proxyauth={proxy_user}:{proxy_pass}",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    try:
        conn = Connection(uuid.uuid4(), "127.0.0.1", LOCAL_PORT)
        conn.connect(timeout=20)
        sess = Session(conn, "homeassistant", samba_pw)
        sess.connect()
        tree = TreeConnect(sess, r"\\127.0.0.1\config")
        tree.connect()
        for d in DIRS:
            mkdir(tree, d)
        for rel in FILES:
            local = os.path.join(SRC, rel)
            remote = f"custom_components/roomprint/{rel}"
            with open(local, "rb") as f:
                data = f.read()
            write_file(tree, remote, data)
            back = read_file(tree, remote)
            assert back == data, f"verify failed: {remote}"
            print(f"wrote+verified {remote}: {len(data)} bytes")
        conn.disconnect()
        print("deploy complete, all files byte-identical")
    finally:
        socat.terminate()


if __name__ == "__main__":
    main()
