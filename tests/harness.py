"""Shared test harness.

Starts the fixture log server in a background thread, prepares 5- and 9-leaf
trees, and offers small helpers.  Every test talks to the server over HTTP
exactly like the CLI does; nothing shortcuts through the server objects.
"""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import urllib.request

from ctmirror.client import HttpTransport, Manifest, MirrorClient, \
    SourceConfig, register_manifest
from ctmirror.store import Store
from fixture.server import make_server


def start_server(sources=("alpha",)):
    server, state = make_server([(sid, 7 + i) for i, sid in enumerate(sources)])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state


def stop_server(server):
    server.shutdown()
    server.server_close()


def admin(server, path: str, body=None):
    url = f"http://127.0.0.1:{server.server_port}{path}"
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def make_manifest(server, sources) -> Manifest:
    """Build a Manifest from the fixture's admin view (trust anchor only)."""
    state = admin(server, "/admin/state")
    m = Manifest()
    for sid in sources:
        info = state["sources"][sid]
        key = bytes.fromhex(info["public_keys"]["0"])
        m.sources[sid] = SourceConfig(sid, 0, key, ())
    return m


def make_client(server, sources, db_path=None, page_size=2,
                manifest=None) -> tuple[Store, MirrorClient, str]:
    manifest = manifest or make_manifest(server, sources)
    db_path = db_path or tempfile.mktemp(suffix=".sqlite", prefix="ctmirror-")
    store = Store(db_path)
    register_manifest(store, manifest)
    transport = HttpTransport(f"http://127.0.0.1:{server.server_port}")
    client = MirrorClient(store, transport, manifest, page_size=page_size)
    return store, client, db_path


def seed(server, source_id, leaves):
    admin(server, "/admin/append",
          {"source_id": source_id, "leaves": [base64.b64encode(x).decode()
                                              for x in leaves]})


def reopen(db_path, server, manifest, page_size=2) -> tuple[Store,
                                                            MirrorClient]:
    store = Store(db_path)
    register_manifest(store, manifest)
    transport = HttpTransport(f"http://127.0.0.1:{server.server_port}")
    return store, MirrorClient(store, transport, manifest,
                               page_size=page_size)


def leaves_for(source_id, n):
    return [f"{source_id}:leaf:{i}".encode() for i in range(n)]
