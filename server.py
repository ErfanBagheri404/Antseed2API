"""OpenAI-compatible passthrough for the AntSeed buyer node (:8377).

Value over hitting the buyer directly:
  - optional Bearer API key gate
  - strips the trailing `data: [DONE]` trailer the buyer appends to
    non-streaming JSON bodies (breaks strict JSON clients)
  - trims oversized context to 240KB before forwarding
  - /healthz reflects actual buyer liveness (DHT nodes + peers)
"""
import http.client
import json
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

_server_api_key = None
_server_upstream = "http://127.0.0.1:8377/v1"

# Upstream tolerance for request bodies; stay under it.
BODY_BUDGET = 240 * 1024

HEADERS_TO_DROP = {"host", "connection", "content-length",
                   "transfer-encoding", "accept-encoding"}


def _log(*parts):
    try:
        with open("server.log", "a", encoding="utf-8") as f:
            f.write(" ".join(str(p) for p in parts) + "\n")
    except Exception:
        pass


def _sanitize(body: bytes) -> bytes:
    """Cut the trailing SSE trailer some sellers append to JSON bodies."""
    s = body.decode("utf-8", "replace")
    cut = s.find("data: [DONE]")
    if cut != -1:
        s = s[:cut]
    end = s.rfind("}")
    if end != -1:
        s = s[:end + 1]
    return s.strip().encode()


def _upstream_json(method, path, payload=None, timeout=150):
    url = _server_upstream + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        raise ConnectionError(f"buyer unreachable ({type(e).__name__}): {e}") from e
    finally:
        _log(f"{method} {path} {time.time()-t0:.1f}s")


def _trim_messages(messages):
    """Drop oldest non-system turns until the JSON fits BODY_BUDGET."""
    msgs = list(messages)
    trimmed = 0
    while len(json.dumps(msgs).encode()) > BODY_BUDGET and len(msgs) > 1:
        idx = next((i for i, m in enumerate(msgs)
                    if m.get("role") != "system"), 0)
        del msgs[idx]
        trimmed += 1
    return msgs, trimmed


def proxy_once(model, message, timeout=150):
    """One non-streaming completion. Returns (text, usage)."""
    st, body = _upstream_json("POST", "/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": message}],
        "max_tokens": 60,
    }, timeout=timeout)
    if st != 200:
        raise RuntimeError(f"upstream {st}: {body[:300]!r}")
    d = json.loads(_sanitize(body))
    msg = d["choices"][0]["message"]
    text = msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or ""
    return str(text), d.get("usage")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Antseed2Proxy"

    def log_message(self, *a):
        pass

    def _auth_ok(self):
        if not _server_api_key:
            return True
        got = self.headers.get("authorization", "")
        return got.replace("Bearer ", "", 1).strip() == _server_api_key

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _err(self, status, msg, etype="antseed_error"):
        self._json(status, {"error": {"message": str(msg), "type": etype,
                                      "code": status}})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            st, data, _raw = None, None, ""
            try:
                from main import upstream_get
                st, data, _raw = upstream_get("/_antseed/status", timeout=10)
            except Exception as e:
                return self._json(200, {"ok": False, "error": str(e)[:100]})
            ok = st == 200 and bool(data) and (data.get("peerCount") or 0) > 0
            return self._json(200, {"ok": ok,
                                    "dht": (data or {}).get("dhtNodeCount"),
                                    "peers": (data or {}).get("peerCount")})
        if path == "/v1/models":
            if not self._auth_ok():
                return self._err(401, "bad api key", "invalid_api_key")
            try:
                st, body = _upstream_json("GET", "/models", timeout=60)
            except ConnectionError as e:
                return self._err(502, str(e), "upstream_unreachable")
            if st != 200:
                return self._err(502, f"buyer /v1/models -> {st}", "upstream_error")
            try:
                data = json.loads(body)
                slim = [{"id": m.get("id"), "object": "model",
                         "created": m.get("created", int(time.time())),
                         "owned_by": "antseed",
                         # Keep the P2P metadata (tags/pricing) so clients can
                         # tell free models from paid ones.
                         "antseed": m.get("antseed")}
                        for m in data.get("data", [])]
                return self._json(200, {"object": "list", "data": slim})
            except Exception as e:
                return self._err(502, f"bad buyer payload: {e}", "upstream_error")
        return self._err(404, "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/v1/chat/completions":
            return self._err(404, "not found")
        if not self._auth_ok():
            return self._err(401, "bad api key", "invalid_api_key")
        try:
            ln = int(self.headers.get("content-length", 0))
        except ValueError:
            ln = 0
        try:
            payload = json.loads(self.rfile.read(ln) or b"{}")
        except Exception:
            return self._err(400, "invalid JSON")
        stream = bool(payload.get("stream"))
        msgs = payload.get("messages") or []
        trimmed_msgs, trimmed = _trim_messages(msgs)
        if trimmed:
            payload["messages"] = trimmed_msgs
            _log(f"trimmed={trimmed}")
        # Forward to the buyer over a raw connection so streaming passes through.
        try:
            host = _server_upstream.split("://", 1)[1].split("/", 1)[0]
            conn = http.client.HTTPConnection(host, timeout=180)
            fwd = {k: v for k, v in dict(self.headers).items()
                   if k.lower() not in HEADERS_TO_DROP}
            fwd["Content-Type"] = "application/json"
            conn.request("POST", "/v1/chat/completions",
                         body=json.dumps(payload).encode(), headers=fwd)
            resp = conn.getresponse()
        except Exception as e:
            return self._err(502, f"buyer unreachable: {e}", "upstream_unreachable")
        if not stream:
            try:
                body = resp.read()
            except Exception as e:
                return self._err(502, f"buyer read failed: {e}", "upstream_error")
            try:
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json")
                clean = _sanitize(body)
                self.send_header("Content-Length", str(len(clean)))
                self.end_headers()
                self.wfile.write(clean)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass
            return
        # Streaming: relay SSE frames verbatim. SSE has no Content-Length and
        # we do not re-chunk, so the connection must close for the client to
        # see end-of-stream (keep-alive would hang strict HTTP/1.1 readers).
        try:
            self.close_connection = True
            self.send_response(resp.status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionAbortedError,
                        ConnectionResetError):
                    break
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        finally:
            conn.close()


def start_server(host, port, api_key=None, upstream=None):
    global _server_api_key, _server_upstream
    if api_key is None:
        try:
            from main import load_config
            api_key = load_config().get("api_key")
        except Exception:
            api_key = None
    _server_api_key = api_key
    if upstream:
        _server_upstream = upstream.rstrip("/")
    return ThreadingHTTPServer((host, port), Handler)
