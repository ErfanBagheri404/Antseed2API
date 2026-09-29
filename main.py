"""Antseed2Proxy - OpenAI-compatible proxy for the AntSeed P2P model network.

Runs its own buyer node (no AntSeed desktop app needed): `node.mjs` joins the
BitTorrent DHT, discovers sellers advertising `antseed:*` services, and forwards
chat completions over the P2P channel. This process then wraps that endpoint
with the family shape (menu CLI, health checks, model catalog, optional key).

Upstream: http://127.0.0.1:8377/v1  (buyer node, auto-started with the server)
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from version import __version__

BANNER = r"""
+=====================================================+
|              Antseed2Proxy  v{ver:<6}              |
|   OpenAI-compatible proxy for AntSeed P2P models  |
|   Upstream: 127.0.0.1:8377/v1 (buyer node)         |
+====================================================+
""".format(ver=__version__)

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 61026
DEFAULT_UPSTREAM = "http://127.0.0.1:8377/v1"

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "antseed2proxy.json")


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def upstream_get(path, timeout=30):
    """GET from the buyer node.

    `/v1/*` lives under the API prefix; the buyer's own control endpoints
    (`/_antseed/status`) sit at the root, so those skip the prefix.
    """
    base = DEFAULT_UPSTREAM if path.startswith("/v1/") else DEFAULT_UPSTREAM.removesuffix("/v1")
    url = base + path
    req = urllib.request.Request(url, headers={"User-Agent": "Antseed2Proxy/" + __version__})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body), body
            except Exception:
                return r.status, None, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body), body
        except Exception:
            return e.code, None, body
    except Exception as e:
        return 0, None, str(e)


def cmd_status():
    cfg = load_config()
    print("  Upstream   :", DEFAULT_UPSTREAM)
    print("  Config     :", CONFIG_PATH)
    print("  API key    :", "(set)" if cfg.get("api_key") else "(none)")
    st, data, raw = upstream_get("/_antseed/status", timeout=15)
    if st == 200 and data:
        print("  Buyer      : connected")
        print("  DHT nodes  :", data.get("dhtNodeCount"))
        print("  Peers      :", data.get("peerCount"))
        print("  Models     :", data.get("modelCount"), "| free:", data.get("freeCount"))
    else:
        print("  Buyer      : DOWN  (start server to auto-start it, or run: py buyer.py)")
        print("  Detail     :", (raw or "")[:200])


def cmd_models():
    st, data, raw = upstream_get("/v1/models", timeout=60)
    if st != 200 or not data:
        print("  Cannot reach buyer node:", (raw or "")[:200])
        return
    models = data.get("data", [])
    free = [m for m in models if (m.get("antseed", {}) or {}).get("free")]
    print(f"  {len(models)} models total, {len(free)} free:")
    for m in free:
        print(f"    - {m.get('id')}")
    if len(models) > len(free):
        print(f"    ... plus {len(models) - len(free)} paid models (option 3 to list all)")


def cmd_test_chat():
    from server import proxy_once
    model = input("  Model [deepseek-v4-flash]: ").strip() or "deepseek-v4-flash"
    msg = input("  Message [Reply with exactly OK]: ").strip() or "Reply with exactly OK"
    t0 = time.time()
    try:
        text, usage = proxy_once(model, msg, timeout=150)
    except Exception as e:
        print(f"  Error: {type(e).__name__}: {str(e)[:300]}")
        return
    print(f"  [{time.time()-t0:.1f}s] {text[:600]}")
    if usage:
        print("  usage:", usage)


def ensure_buyer():
    """Start the buyer node if it is not already listening."""
    import buyer
    try:
        return buyer.start(wait=60)
    except buyer.BuyerUnavailable as e:
        print("  Buyer unavailable:", str(e)[:200])
        return None


def cmd_start_server(host, port, api_key, headless=False):
    from server import start_server
    ensure_buyer()
    srv = start_server(host, port, api_key=api_key, upstream=DEFAULT_UPSTREAM)
    url = f"http://{host}:{port}"
    print(f"\n  Antseed2Proxy listening")
    print(f"  OpenAI base : {url}/v1")
    print(f"  Models      : {url}/v1/models")
    print(f"  Health      : {url}/healthz")
    if api_key:
        print("  API key     : (client must send it as Bearer key)")
    print("  Ctrl+C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        srv.server_close()


def main():
    ap = argparse.ArgumentParser(prog="Antseed2Proxy")
    ap.add_argument("--no-menu", action="store_true", help="run server directly")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--api-key", default=None, help="require this key on /v1/*")
    args = ap.parse_args()

    if args.no_menu:
        cmd_start_server(args.host, args.port, args.api_key, headless=True)
        return

    print(BANNER)
    while True:
        print("  1. Status")
        print("  2. Start server")
        print("  3. List models")
        print("  4. Test chat")
        print("  5. Set API key")
        print("  6. Quit")
        c = input("\n  > ").strip()
        if c == "1":
            cmd_status()
        elif c == "2":
            cmd_start_server(args.host, args.port, args.api_key)
        elif c == "3":
            cmd_models()
        elif c == "4":
            cmd_test_chat()
        elif c == "5":
            key = input("  API key (empty to clear): ").strip()
            cfg = load_config()
            if key:
                cfg["api_key"] = key
            else:
                cfg.pop("api_key", None)
            save_config(cfg)
            print("  Saved.")
        elif c in ("6", "q", "Q"):
            break
    print("  Bye.")


if __name__ == "__main__":
    main()
