"""실스택 검증에서 GitHub Deployments와 Grafana Prometheus를 대신하는 고정 응답 서버."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CURRENT_SHA = "b2f41c9current"
PREVIOUS_SHA = "a1e30b8previous"
DEPLOYED_AT = {"2": "2026-11-18T13:58:00+09:00", "1": "2026-11-17T10:00:00+09:00"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 규약)
        url = urlsplit(self.path)
        parts = [part for part in url.path.split("/") if part]
        if parts[:1] == ["github"] and parts[-1] == "deployments":
            self._send([
                {"id": 2, "sha": CURRENT_SHA, "ref": "v2.4.1"},
                {"id": 1, "sha": PREVIOUS_SHA, "ref": "v2.4.0"},
            ])
        elif parts[:1] == ["github"] and parts[-1] == "statuses":
            self._send([{"state": "success", "created_at": DEPLOYED_AT.get(parts[-2], DEPLOYED_AT["1"])}])
        elif parts == ["prom", "api", "v1", "query"]:
            query = parse_qs(url.query).get("query", [""])[0]
            errors = "235" if CURRENT_SHA in query else "2"
            value = errors if 'status="502"' in query else "1000"
            self._send({"status": "success", "data": {"result": [{"value": [0, value]}]}})
        else:
            self._send({"message": "not found"}, status=404)

    def _send(self, body: object, status: int = 200) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
