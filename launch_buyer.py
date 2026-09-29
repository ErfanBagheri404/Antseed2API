"""Launch the AntSeed buyer proxy with the identity decrypted from the app's
safeStorage blob, in a fully detached process that survives its launcher.

Why this exists: the desktop app stores its P2P identity in
~/.antseed/identity.enc (Chromium v10 AES-GCM, key DPAPI-wrapped in the app's
Local State). The bundled CLI refuses to start standalone against a data dir
that already holds such an identity, so the app's own spawn is the only
supported path — this replicates it (see dist/main/runtime/process-manager.js
resolveCommandArgs: `node cli/index.js --config <cfg> --data-dir <dir> buyer
start`, plus ANTSEED_IDENTITY_HEX and NODE_PATH to the unpacked node_modules).

Usage: python launch_buyer.py [--restart]
"""
import base64
import json
import os
import subprocess
import sys

APPDIR = r"C:\Users\mrenm\AppData\Local\Programs\AntSeed VPR"
EXE = os.path.join(APPDIR, "AntSeed VPR.exe")
CLI = os.path.join(APPDIR, "resources", "cli-dist", "cli", "index.js")
NODE_PATH = os.path.join(APPDIR, "resources", "app.asar.unpacked", "node_modules")
DATA_DIR = r"C:\Users\mrenm\.antseed"
CONFIG = os.path.join(DATA_DIR, "config.json")
LOCAL_STATE = r"C:\Users\mrenm\AppData\Roaming\AntStation Desktop\Local State"
LOG = r"C:\Users\mrenm\AppData\Local\Temp\antseed-buyer.log"


def identity_hex():
    """Decrypt ~/.antseed/identity.enc: v10 = 3-byte prefix + 12-byte nonce +
    AES-256-GCM ciphertext; the AES key is DPAPI-wrapped in Local State."""
    import ctypes
    import ctypes.wintypes as wt
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    def dpapi_unwrap(blob: bytes) -> bytes:
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        inbuf = DATA_BLOB(len(blob), ctypes.create_string_buffer(blob, len(blob)))
        outbuf = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(inbuf), None, None, None, None, 0, ctypes.byref(outbuf)):
            raise OSError("CryptUnprotectData failed")
        raw = ctypes.string_at(outbuf.pbData, outbuf.cbData)
        ctypes.windll.kernel32.LocalFree(outbuf.pbData)
        return raw

    state = json.load(open(LOCAL_STATE, encoding="utf-8"))
    wrapped = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if wrapped[:5] == b"DPAPI":
        wrapped = wrapped[5:]
    master = dpapi_unwrap(wrapped)
    blob = open(os.path.join(DATA_DIR, "identity.enc"), "rb").read()
    if blob[:3] != b"v10":
        raise SystemExit("identity.enc is not a v10 safeStorage blob")
    return AESGCM(master).decrypt(blob[3:15], blob[15:], None).decode()


def main():
    if "--restart" in sys.argv:
        subprocess.run(["taskkill", "/F", "/IM", "AntSeed VPR.exe"],
                       capture_output=True)
    env = dict(os.environ)
    env["ANTSEED_IDENTITY_HEX"] = identity_hex()
    env["NODE_PATH"] = NODE_PATH
    env["ELECTRON_RUN_AS_NODE"] = "1"
    env["ANTSEED_SKIP_PLUGIN_UPDATE_CHECK"] = "1"
    env.pop("ANTSEED_ENABLE_SETTLEMENT", None)
    log = open(LOG, "w")
    proc = subprocess.Popen(
        [EXE, CLI, "--config", CONFIG, "--data-dir", DATA_DIR, "buyer", "start"],
        env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True)
    env.pop("ANTSEED_IDENTITY_HEX", None)
    print("buyer pid:", proc.pid, "-> log:", LOG)


if __name__ == "__main__":
    main()
