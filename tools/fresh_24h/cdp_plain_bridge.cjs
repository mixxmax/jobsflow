"use strict";

// Local CDP bridge. Two Chrome 157 toggle-mode facts force this process:
//
// 1. Playwright's WebSocket client negotiates permessage-deflate. Chrome
//    accepts the extension and then never answers Browser.getVersion. A
//    client with the extension disabled gets the answer. Playwright exposes
//    no switch, so this process accepts Playwright and opens a second socket
//    to Chrome with compression off.
// 2. connectOverCDP then waits until every existing tab answers Page.enable
//    and Page.getFrameTree. One wedged renderer holds the whole attach. A
//    child-session command that Chrome has not answered in 3.5s gets an empty
//    result so that tab is skipped and a later new page can still be opened.
//
// This process does not launch a browser.

const bundle = process.argv[2];
const upstreamUrl = process.argv[3];
if (!bundle || !upstreamUrl) {
  process.stderr.write("cdp_plain_bridge: missing arguments\n");
  process.exit(2);
}

const { ws: WebSocket, wsServer: WebSocketServer } = require(bundle);
const server = new WebSocketServer({
  host: "127.0.0.1",
  port: 0,
  perMessageDeflate: false,
});

const STALL_MS = 3500;

function asText(data) {
  return typeof data === "string" ? data : Buffer.from(data).toString("utf8");
}

function parseMessage(text) {
  try {
    const message = JSON.parse(text);
    return message && typeof message === "object" ? message : null;
  } catch {
    return null;
  }
}

function syntheticResult(message) {
  if (message.method === "Page.getFrameTree") {
    return {
      frameTree: {
        frame: {
          id: "stalled-frame",
          loaderId: "stalled-loader",
          url: "about:blank",
          mimeType: "text/plain",
          securityOrigin: "://",
        },
      },
    };
  }
  return {};
}

server.on("connection", (client) => {
  const upstream = new WebSocket(upstreamUrl, [], {
    perMessageDeflate: false,
    handshakeTimeout: 20000,
  });
  const pending = [];
  const timers = new Map();
  const synthesized = new Set();
  let upstreamOpen = false;

  function closeBoth() {
    for (const timer of timers.values()) clearTimeout(timer);
    timers.clear();
    if (client.readyState === WebSocket.OPEN) client.close();
    if (upstream.readyState === WebSocket.OPEN || upstream.readyState === WebSocket.CONNECTING) {
      upstream.close();
    }
  }

  function armStall(message) {
    if (message.id == null || !message.sessionId) return;
    const timer = setTimeout(() => {
      timers.delete(message.id);
      synthesized.add(message.id);
      if (client.readyState === WebSocket.OPEN) {
        client.send(JSON.stringify({
          id: message.id,
          sessionId: message.sessionId,
          result: syntheticResult(message),
        }));
      }
    }, STALL_MS);
    timers.set(message.id, timer);
  }

  upstream.on("open", () => {
    upstreamOpen = true;
    for (const message of pending) upstream.send(message);
    pending.length = 0;
  });
  client.on("message", (data) => {
    const text = asText(data);
    const message = parseMessage(text);
    if (message) armStall(message);
    if (upstreamOpen) upstream.send(text);
    else pending.push(text);
  });
  upstream.on("message", (data) => {
    const text = asText(data);
    const message = parseMessage(text);
    if (message && message.id != null) {
      if (synthesized.has(message.id)) return;
      const timer = timers.get(message.id);
      if (timer) {
        clearTimeout(timer);
        timers.delete(message.id);
      }
    }
    if (client.readyState === WebSocket.OPEN) client.send(text);
  });
  client.on("close", closeBoth);
  upstream.on("close", closeBoth);
  client.on("error", closeBoth);
  upstream.on("error", closeBoth);
});

server.on("listening", () => {
  const address = server.address();
  const port = address && address.port;
  if (!port) {
    process.stderr.write("cdp_plain_bridge: no listen port\n");
    process.exit(1);
  }
  require("fs").writeSync(1, `LISTEN ws://127.0.0.1:${port}\n`);
});

server.on("error", (error) => {
  process.stderr.write(`cdp_plain_bridge: ${error && error.message ? error.message : error}\n`);
  process.exit(1);
});
