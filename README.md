# Antseed2Proxy

OpenAI-compatible proxy for the **AntSeed P2P inference marketplace** — no desktop
app required.

This project ships its own buyer node (`node.mjs`): it joins the BitTorrent DHT,
discovers sellers advertising `antseed:*` services, and forwards chat completions
over the P2P channel. The Python side wraps that endpoint in the usual family
shape: an optional API key gate, clean JSON responses, oversized-context trimming,
and a `/healthz` that tells you whether the P2P network is actually usable.

```
your client → http://localhost:61026/v1 → Antseed2Proxy → 127.0.0.1:8377 (buyer node)
                                                                    ↓
                                                          P2P seller (WebRTC/TCP)
```

## Why models don't load out of the box

Two separate problems, both handled by `buyer.py`:

**1. The AntSeed app's DHT bootstrap nodes are unreachable.** The network is
seeded only by `dht1.antseed.com` / `dht2.antseed.com`. Those hosts sit behind
Cloudflare, which cannot relay UDP, so no client can ever bootstrap through them.
The sellers are already on the **mainline BitTorrent DHT** — `buyer.py` points
`network.bootstrapNodes` in `~/.antseed/config.json` at public routers instead and
45+ peers appear.

**2. The identity is encrypted in app format.** If the AntSeed app ever ran, it
stored the P2P private key in `~/.antseed/identity.enc` (Electron safeStorage,
DPAPI-wrapped). `buyer.py` decrypts it with stdlib ctypes and passes the key to
the node via `ANTSEED_IDENTITY_HEX`. No app install needed.

## Requirements

- Python 3.9+
- Node.js 18+ on `PATH`
- `~/.antseed/plugins/node_modules/@antseed/*` — the AntSeed packages that ship
  with the app (they stay after uninstall; no running app needed)
- First launch of AntSeed is only needed once if those packages are missing

## Setup

```bash
# Start proxy + buyer node (buyer auto-starts)
python main.py --no-menu

# Or just the buyer node
python buyer.py          # starts it and prints DHT/peer/model counts
python buyer.py --stop
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
| 1 | Status — buyer liveness, DHT node count, peers, models |
| 2 | Start server (auto-starts buyer) |
| 3 | List models (free ones listed first) |
| 4 | Test chat |
| 5 | Set/clear API key |
| 6 | Quit |

## Free models

`/v1/models` entries carry `antseed.free` (true price is 0 input/0 output per
million) plus `antseed.tags` and prices. Sellers sometimes tag paid models
`"free"`, so trust the price, not the tag — `gemma-3-27b`, `mistral-small-3.2-24b`
and `grok-4.7-build-fast` do that. 48 models were genuinely free at last check.

Verified working chat models: `deepseek-v4-flash`, `nemotron-120b-free`,
`step-3.5-flash`, `step-3.7-flash`, `agnes-2.5-flash`, `agnes-3-flash`,
`space-bunny-alpha`. The buyer fails over to the next seller when one demands
payment or is offline, so transient `502`s usually self-heal. Reasoning models
need `max_tokens ≥ ~300` or the thinking budget eats the reply.

## Files

| File | Purpose |
|---|---|
| `main.py` | CLI entry point and menu |
| `server.py` | HTTP server: `/v1/models`, `/v1/chat/completions`, `/healthz` |
| `buyer.py` | Buyer lifecycle: identity decrypt, bootstrap fix, start/stop |
| `node.mjs` | Standalone buyer node + OpenAI-shaped HTTP server on 8377 |
| `tests/selfcheck.py` | Offline unit checks (URL prefixes, SSE framing) |
