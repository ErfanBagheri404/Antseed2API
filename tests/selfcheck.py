"""Offline selfcheck for Antseed2Proxy (no buyer node needed)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
from main import DEFAULT_PORT, DEFAULT_UPSTREAM


def main():
    # 1. sanitize: trailing SSE trailer cut, valid JSON kept
    raw = b'{"a":1,"choices":[{"message":{"content":"hi"}}]}data: [DONE]\n\n'
    clean = server._sanitize(raw)
    assert clean == b'{"a":1,"choices":[{"message":{"content":"hi"}}]}', clean

    # 2. sanitize: clean body untouched
    raw2 = b'{"ok":true}'
    assert server._sanitize(raw2) == raw2

    # 3. trim: small context untouched
    msgs = [{"role": "user", "content": "hi"}]
    out, n = server._trim_messages(msgs)
    assert n == 0 and out == msgs

    # 4. trim: oversized context shrinks under budget, newest kept
    big = [{"role": "system", "content": "s"}] + [
        {"role": "user", "content": "x" * 60000} for _ in range(5)]
    out, n = server._trim_messages(big)
    import json
    assert len(json.dumps(out).encode()) <= server.BODY_BUDGET, "still over budget"
    assert n >= 1, "nothing trimmed"
    assert out[0].get("role") == "system", "system prompt dropped"
    assert out[-1]["content"].startswith("x"), "newest turn dropped"

    # 5. headers to drop contains the stale-length + encoding pair
    assert "content-length" in server.HEADERS_TO_DROP
    assert "accept-encoding" in server.HEADERS_TO_DROP

    # 6. defaults
    assert DEFAULT_PORT == 61026, DEFAULT_PORT
    assert DEFAULT_UPSTREAM == "http://127.0.0.1:8377/v1", DEFAULT_UPSTREAM

    # 7. root control endpoints must not get the /v1 prefix
    from main import upstream_get
    import main as M
    assert M.DEFAULT_UPSTREAM.removesuffix("/v1") + "/_antseed/status" == \
        "http://127.0.0.1:8377/_antseed/status"

    # 8. streaming must close the connection (no Content-Length to frame it)
    import inspect
    src = inspect.getsource(server.Handler.do_POST)
    assert 'Connection", "close"' in src, "streaming response must close the connection"

    print("ALL PASS")


if __name__ == "__main__":
    main()
