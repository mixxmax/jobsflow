"""Chrome 157 answers a plain CDP socket and ignores a deflate client.

Playwright always offers permessage-deflate.  These tests stay on localhost:
a fake DevTools server speaks uncompressed CDP, and the bridge must still
deliver Browser.getVersion to a client that offers compression.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

from tools.fresh_24h.portal_jd_browser import (
    _cdp_connect_runtime_error,
    _playwright_driver_node,
    _start_cdp_plain_bridge,
    _stop_cdp_bridge,
)


def test_timeout_after_websocket_is_not_a_missing_port():
    exc = TimeoutError(
        "Timeout 60000ms exceeded.\nCall log:\n  - <ws connected> ws://127.0.0.1:9222/devtools/browser/secret"
    )
    assert str(_cdp_connect_runtime_error(exc)) == "cdp_attach_timeout"
    assert str(_cdp_connect_runtime_error(ConnectionRefusedError("ECONNREFUSED"))) == (
        "cdp_endpoint_unavailable"
    )


def test_bridge_delivers_get_version_from_a_deflate_client(tmp_path: Path):
    node = _playwright_driver_node()
    bundle = node.parent / "package" / "lib" / "utilsBundle.js"
    upstream = tmp_path / "upstream.cjs"
    upstream.write_text(
        textwrap.dedent(
            """
            const { ws: WebSocket, wsServer: WebSocketServer } = require(process.argv[2]);
            const server = new WebSocketServer({
              host: "127.0.0.1",
              port: 0,
              perMessageDeflate: false,
            });
            server.on("connection", (socket) => {
              socket.on("message", (data) => {
                const message = JSON.parse(String(data));
                if (message.method === "Browser.getVersion") {
                  socket.send(JSON.stringify({
                    id: message.id,
                    result: { product: "Chrome/test" },
                  }));
                }
              });
            });
            server.on("listening", () => {
              require("fs").writeSync(1, `UP ${server.address().port}\\n`);
            });
            """
        ),
        encoding="utf-8",
    )
    upstream_proc = subprocess.Popen(
        [str(node), str(upstream), str(bundle)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    bridge = None
    try:
        assert upstream_proc.stdout is not None
        line = upstream_proc.stdout.readline()
        assert line.startswith("UP ")
        port = int(line.split()[1])
        bridge, local = _start_cdp_plain_bridge(f"ws://127.0.0.1:{port}/devtools/browser/test")
        client = tmp_path / "client.cjs"
        client.write_text(
            textwrap.dedent(
                """
                const { ws: WebSocket } = require(process.argv[2]);
                const url = process.argv[3];
                const deflate = {
                  clientNoContextTakeover: true,
                  zlibDeflateOptions: { level: 3 },
                  zlibInflateOptions: { chunkSize: 10 * 1024 },
                  threshold: 10 * 1024,
                };
                const sock = new WebSocket(url, [], { perMessageDeflate: deflate, handshakeTimeout: 5000 });
                const timer = setTimeout(() => {
                  console.log("TIMEOUT");
                  process.exit(2);
                }, 5000);
                sock.on("open", () => sock.send(JSON.stringify({ id: 1, method: "Browser.getVersion" })));
                sock.on("message", (data) => {
                  clearTimeout(timer);
                  const product = JSON.parse(String(data)).result.product;
                  console.log(product);
                  sock.close();
                });
                """
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [str(node), str(client), str(bundle), local],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "Chrome/test"
        stalled = subprocess.run(
            [
                str(node),
                "-e",
                (
                    "const {ws:WebSocket}=require(process.argv[1]);"
                    "const sock=new WebSocket(process.argv[2],[],{perMessageDeflate:false,handshakeTimeout:5000});"
                    "const timer=setTimeout(()=>{console.log('TIMEOUT');process.exit(2);},5000);"
                    "sock.on('open',()=>sock.send(JSON.stringify({id:7,sessionId:'page',method:'Page.getFrameTree'})));"
                    "sock.on('message',(data)=>{clearTimeout(timer);const frame=JSON.parse(String(data)).result.frameTree.frame;"
                    "console.log(frame.id);sock.close();});"
                ),
                str(bundle),
                local,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert stalled.returncode == 0, stalled.stderr
        assert stalled.stdout.strip() == "stalled-frame"
    finally:
        _stop_cdp_bridge(bridge)
        if upstream_proc.poll() is None:
            upstream_proc.terminate()
            try:
                upstream_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                upstream_proc.kill()
