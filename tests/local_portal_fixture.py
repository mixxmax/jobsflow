"""Local portal fixture server for browser-recovery tests (handbook §14.2).

Serves the six deterministic endpoints used as the Phase-1 feedback loop:
challenge (header/title), rate-limit with Retry-After, a real-looking JD,
an empty shell and a redirect into a challenge.  No real portal is involved.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHALLENGE_HTML = (
    "<html><head><title>Just a moment...</title></head><body>"
    "<div>cf-browser-verification checking your browser</div></body></html>"
)

VALID_JD_HTML = (
    "<html><head><title>Paralegal - Example Firm</title></head><body>"
    '<div data-automation="jobAdDetails">'
    "Key Responsibilities: draft and review commercial contracts for the Hong "
    "Kong office, support litigation teams with document review, and manage "
    "due diligence exercises for M&A transactions. "
    "Requirements: degree in law from a recognised university, three years of "
    "experience in a law firm or in-house legal department, and fluency in "
    "English and Chinese. "
    "We offer a competitive salary, medical benefits and study support for "
    "professional qualifications."
    "</div></body></html>"
)

EMPTY_SHELL_HTML = (
    "<html><head><title>JobsDB - Hong Kong</title></head><body>"
    "<nav>Home Jobs Companies Advice</nav><div>No job description available.</div>"
    "</body></html>"
)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output clean
        pass

    def _send(self, status, body, headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/challenge-header":
            self._send(200, CHALLENGE_HTML, {"cf-mitigated": "challenge"})
        elif path == "/challenge-title":
            self._send(200, CHALLENGE_HTML)
        elif path == "/rate-limit":
            self._send(429, "Too Many Requests", {"Retry-After": "120"})
        elif path == "/valid-jd":
            self._send(200, VALID_JD_HTML)
        elif path == "/empty-shell":
            self._send(200, EMPTY_SHELL_HTML)
        elif path == "/redirect-challenge":
            self.send_response(302)
            self.send_header("Location", "/challenge-header")
            self.end_headers()
        else:
            self._send(404, "not found")


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
