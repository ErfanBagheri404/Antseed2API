# Antseed2Proxy

OpenAI-compatible proxy for the **AntSeed AI VPN** desktop app (P2P inference marketplace).

The AntSeed app runs a local buyer node on `127.0.0.1:8377` that already speaks the
OpenAI chat-completions API. This project wraps it in the usual family shape: an
optional API key gate, clean JSON responses, oversized-context trimming, and a
`/healthz` that tells you whether the P2P network is actually usable.

```
your client → http://localhost:61026/v1 → Antseed2Proxy → 127.0.0.1:8377 (buyer node)
                                                                    ↓
                                                          P2P seller (WebRTC/TCP)
```

## Why models don't load out of the box

Two separate problems, both fixed locally:

**1. The buyer node isn't running.** The desktop app's process manager can die
without spawning it, leaving a stale `buyer.state.json` with a dead PID.
`launch_buyer.py` decrypts the app's identity and spawns the buyer correctly.

**2. AntSeed's DHT bootstrap nodes are unreachable.** The buyer discovers sellers
over BitTorrent-DHT (UDP 6881), seeded only by `dht1.antseed.com` /
`dht2.antseed.com`. Those hosts sit behind Cloudflare, which cannot relay UDP, so
no client can ever bootstrap through them. The sellers are already on the
**mainline BitTorrent DHT** — point `network.bootstrapNodes` in
`~/.antseed/config.json` at public routers instead and 45+ peers appear.

## Setup

```bash
# 1. Make the buyer node reachable (see above for why each step matters)
python launch_buyer.py

# 2. Start the proxy
python main.py --no-menu
```

Then point any OpenAI client at it:

```bash
curl http://localhost:61026/v1/models
curl http://localhost:61026/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hi"}]}'
```

## Menu

```
python main.py
```

| Option | Action |
|---|---|
| 1 | Status — buyer liveness, DHT node count, peer count |
| 2 | Start server |
| 3 | List models (marks free ones) |
| 4 | Test chat |
| 5 | Set/clear API key |
| 6 | Quit |

## Files

| File | Purpose |
|---|---|
| `main.py` | CLI entry point and menu |
| `server.py` | HTTP server: `/v1/models`, `/v1/chat/completions`, `/healthz` |
| `tests/selfcheck.py` | Offline unit checks |
| `launch_buyer.py` | Spawns the AntSeed buyer node (not shipped in the binary) |

## Port

Default `61026`. Override with `--port`.

## What the proxy adds over hitting :8377 directly

- Optional Bearer API key on `/v1/*`
- Strips the trailing `data: [DONE]` trailer some sellers append to non-streaming
  JSON bodies — that trailer breaks strict JSON parsers
- Trims conversation history to 240 KB before forwarding
- `/healthz` returns `ok` only when the buyer has live peers, so a 200 here
  actually means the network is usable

## Testing

```bash
python tests/selfcheck.py
```
