"""Start and supervise the standalone AntSeed buyer node (no desktop app).

The desktop app stored its P2P identity in ~/.antseed/identity.enc as an Electron
safeStorage blob ("v10" + 12-byte nonce + AES-256-GCM ciphertext, AES key
DPAPI-wrapped in the app's Local State). The @antseed libraries refuse to mint a
second wallet in a data dir that already holds such a file, so we decrypt the key
here and hand it over via ANTSEED_IDENTITY_HEX, which loadOrCreateIdentity()
accepts directly. Everything else runs from the plain @antseed packages under
~/.antseed/plugins/node_modules -- no Electron, no app install.

Discovery note: AntSeed's own dht1/dht2.antseed.com seeds sit behind Cloudflare,
which drops their UDP. node.mjs therefore takes mainline BitTorrent routers as
bootstrap nodes; sellers announce `antseed:subnet:*` infohashes there.
"""
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA_DIR = Path(os.environ.get("ANTSEED_DATA", Path.home() / ".antseed"))
# PyInstaller unpacks bundled data into _MEIPASS; from source it sits next to us.
_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
NODE_MJS = _BASE / "node.mjs"
NODE_MODULES = DATA_DIR / "plugins" / "node_modules"
LOCAL_STATE = Path(os.environ.get("APPDATA", "")) / "AntStation Desktop" / "Local State"

STATUS_URL = "http://127.0.0.1:8377/_antseed/status"


class BuyerUnavailable(RuntimeError):
    pass


def _dpapi_unwrap(blob: bytes) -> bytes:
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    inbuf = DATA_BLOB(len(blob), ctypes.create_string_buffer(blob, len(blob)))
    outbuf = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(inbuf), None, None, None, None, 0, ctypes.byref(outbuf)
    ):
        raise OSError("CryptUnprotectData failed")
    raw = ctypes.string_at(outbuf.pbData, outbuf.cbData)
    ctypes.windll.kernel32.LocalFree(outbuf.pbData)
    return raw


def identity_hex() -> str:
    """Return the 64-hex private key, or "" when the node should mint a new one."""
    key_path = DATA_DIR / "identity.key"
    if key_path.exists():
        return key_path.read_text(encoding="utf-8").strip()
    enc_path = DATA_DIR / "identity.enc"
    if not enc_path.exists() or not LOCAL_STATE.exists():
        return ""
    state = json.loads(LOCAL_STATE.read_text(encoding="utf-8"))
    wrapped = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if wrapped[:5] == b"DPAPI":
        wrapped = wrapped[5:]
    master = _dpapi_unwrap(wrapped)
    blob = enc_path.read_bytes()
    if blob[:3] != b"v10":
        raise BuyerUnavailable("identity.enc is not a v10 safeStorage blob")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(master).decrypt(blob[3:15], blob[15:], None).decode().removeprefix("0x")


def is_up(timeout=3) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", 8377)) == 0
    finally:
        s.close()


def status(timeout=10):
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def ensure_bootstrap_nodes():
    """AntSeed's official seeds are Cloudflare-fronted and drop UDP, so seed the
    config with public mainline BitTorrent routers that carry the same
    `antseed:subnet:*` announcements. ponytail: rewrite the list if AntSeed ever
    publishes reachable native seeds."""
    cfg_path = DATA_DIR / "config.json"
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except ValueError:
            cfg = {}
    net = cfg.setdefault("network", {})
    seeds = [
        "router.bittorrent.com:6881",
        "dht.transmissionbt.com:6881",
        "router.utorrent.com:6881",
        "dht.peersam.com:6881",
        "router.bitcomet.com:6881",
        "dht.libtorrent.org:25401",
    ]
    if net.get("bootstrapNodes") != seeds:
        net["bootstrapNodes"] = seeds
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def start(wait=60, verbose=True):
    """Start the buyer if it is not already listening. Returns the child process
    (None when someone else -- e.g. the desktop app -- already owns the port)."""
    if is_up():
        return None
    if not NODE_MJS.exists():
        raise BuyerUnavailable(f"missing {NODE_MJS}")
    node_bin = shutil.which("node")
    if not node_bin:
        raise BuyerUnavailable("node not found on PATH (need Node 18+ to run the buyer)")
    if not NODE_MODULES.exists():
        raise BuyerUnavailable(
            f"missing {NODE_MODULES} -- the @antseed packages ship with the AntSeed app's "
            "first run; reinstall AntSeed once, or copy its plugins/node_modules here"
        )
    ensure_bootstrap_nodes()
    env = dict(os.environ)
    key = identity_hex()
    if key:
        env["ANTSEED_IDENTITY_HEX"] = key
    env["ANTSEED_DATA"] = str(DATA_DIR)
    env["ANTSEED_NM"] = str(NODE_MODULES)
    env["NODE_PATH"] = str(NODE_MODULES)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    proc = subprocess.Popen(
        [node_bin, str(NODE_MJS)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        close_fds=True,
    )
    # Never let the private key linger in this process' environment.
    env.pop("ANTSEED_IDENTITY_HEX", None)
    deadline = time.time() + wait
    while time.time() < deadline:
        if is_up():
            if verbose:
                print("  Buyer node started (pid %d)" % proc.pid)
            return proc
        if proc.poll() is not None:
            raise BuyerUnavailable(f"buyer exited immediately (code {proc.returncode})")
        time.sleep(0.5)
    raise BuyerUnavailable(f"buyer did not open 127.0.0.1:8377 within {wait}s")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--stop" in argv:
        subprocess.run(["taskkill", "/F", "/IM", "node.exe"], capture_output=True)
        print("buyer stopped (all node.exe killed)")
        return 0
    try:
        start()
    except BuyerUnavailable as e:
        print("error:", e, file=sys.stderr)
        return 1
    s = status() or {}
    print("  peerId :", s.get("peerId", "?"))
    print("  DHT    :", s.get("dhtNodeCount", "?"))
    print("  peers  :", s.get("peerCount", "?"), "| models:", s.get("modelCount", "?"),
          "| free:", s.get("freeCount", "?"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
