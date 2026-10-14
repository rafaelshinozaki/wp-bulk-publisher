"""Minimal mock of the WordPress REST endpoints used by wp_bulk_publish.py (for local testing)."""
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CATEGORIES = {"blog": 1, "guides": 2, "news": 3}
POSTS = {"already-published-post": {"id": 99, "slug": "already-published-post",
                                    "status": "publish", "link": "https://client.test/already-published-post/"}}
FLAKY = {"flaky-server-example": 2}  # number of 503s before success
AUTH = "Basic " + base64.b64encode(b"bot:app pass word").decode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        if self.headers.get("Authorization") != AUTH:
            self._send(401, {"code": "rest_not_logged_in", "message": "Bad credentials"})
            return False
        return True

    def do_GET(self):
        if not self._authed():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.endswith("/categories"):
            slug = q.get("slug", [""])[0]
            return self._send(200, [{"id": CATEGORIES[slug], "slug": slug}] if slug in CATEGORIES else [])
        if u.path.endswith("/posts"):
            slug = q.get("slug", [""])[0]
            return self._send(200, [POSTS[slug]] if slug in POSTS else [])
        self._send(404, {"message": "not found"})

    def do_POST(self):
        if not self._authed():
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        slug = body["slug"]
        if FLAKY.get(slug, 0) > 0:
            FLAKY[slug] -= 1
            return self._send(503, {"message": "Service Unavailable"})
        if slug == "rejected-by-wordpress":
            return self._send(400, {"code": "rest_invalid_param", "message": "Invalid parameter(s): content"})
        post = {"id": 100 + len(POSTS), "slug": slug, "status": body["status"],
                "date_gmt": body["date_gmt"], "categories": body["categories"],
                "meta": body.get("meta", {}), "link": f"https://client.test/{slug}/"}
        POSTS[slug] = post
        self._send(201, post)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
