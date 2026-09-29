// Standalone AntSeed buyer node + OpenAI-compatible HTTP server.
// Replaces the AntSeed VPR desktop app: same @antseed libraries, no Electron,
// no app install. Serves OpenAI /v1 endpoints on 8377 by discovering peers over
// DHT and forwarding the request over the P2P channel.
//
// Identity: the app stored its key in ~/.antseed/identity.enc (Electron
// safeStorage, DPAPI-wrapped). The lib refuses to create a second wallet when
// that file exists, so the launcher decrypts it and passes ANTSEED_IDENTITY_HEX,
// which loadOrCreateIdentity() honours directly.
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { randomUUID } from "node:crypto";

const NM = process.env.ANTSEED_NM || join(homedir(), ".antseed", "plugins", "node_modules");
const base = "file:///" + NM.replace(/\\/g, "/") + "/@antseed";
const DATA = process.env.ANTSEED_DATA || join(homedir(), ".antseed");
const PORT = Number(process.env.BUYER_PORT || 8377);
const DHT_PORT = Number(process.env.DHT_PORT || 6882);

const { AntseedNode } = await import(base + "/node/dist/index.js");
const cfg = JSON.parse(readFileSync(join(DATA, "config.json"), "utf8"));

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const enc = new TextEncoder();

function bootstrap() {
  return (cfg.network?.bootstrapNodes || []).map((b) =>
    typeof b === "string"
      ? { host: b.split(":")[0], port: Number(b.split(":")[1] || 6881) }
      : b
  );
}

const node = new AntseedNode({
  role: "buyer",
  dataDir: DATA,
  dhtPort: DHT_PORT,
  bootstrapNodes: bootstrap(),
  payments: cfg.buyer?.payments,
});

let peers = [];
let discovering = null;
async function refresh() {
  if (discovering) return discovering;
  discovering = (async () => {
    try {
      peers = await node.discoverPeers();
      log(`discovery: ${peers.length} peers`);
    } catch (e) {
      log("discovery failed:", e.message);
    } finally {
      discovering = null;
    }
  })();
  return discovering;
}

// Peer metadata shape: { peerId, displayName, lastSeen, providers[],
//   providerPricing{}, providerServiceCategories{} }
// Peer carries two flattened maps built by AntseedNode:
//   providerPricing{provider}.services{svc} -> {inputUsdPerMillion, outputUsdPerMillion}
//   providerServiceCategories{provider}.services{svc} -> tag array (may include "free")
function servicesOf(peer) {
  const out = new Map(); // serviceId -> {peer, tags, in, out, provider}
  const cats = peer.providerServiceCategories || {};
  const pricing = peer.providerPricing || {};
  for (const prov of Object.keys(cats)) {
    const svcCats = cats[prov]?.services || {};
    const svcPrice = pricing[prov]?.services || {};
    const defaults = pricing[prov]?.defaults || {};
    const svcIds = new Set([...Object.keys(svcCats), ...Object.keys(svcPrice)]);
    for (const svc of svcIds) {
      const price = svcPrice[svc] ?? defaults;
      out.set(svc, {
        peer,
        provider: prov,
        tags: Array.isArray(svcCats[svc]) ? svcCats[svc] : [],
        in: price?.inputUsdPerMillion ?? null,
        out: price?.outputUsdPerMillion ?? null,
      });
    }
  }
  return out;
}

function catalog() {
  const byId = new Map();
  for (const p of peers) {
    for (const [svc, info] of servicesOf(p)) {
      const prev = byId.get(svc);
      if (!prev || (info.in === 0 && prev.in !== 0)) byId.set(svc, { id: svc, info });
    }
  }
  return [...byId.values()].map(({ id, info }) => ({
    id,
    object: "model",
    owned_by: "antseed",
    antseed: {
      peer: info.peer.displayName || info.peer.peerId.slice(0, 12),
      tags: info.tags,
      input_usd_per_million: info.in,
      output_usd_per_million: info.out,
      // Sellers mislabel paid models as "free"; price is the only truth.
      free: info.in === 0 && info.out === 0,
    },
  }));
}

// Route by the seller-side service id; try an exact match first, then a suffix
// match so `deepseek-v4-flash` finds `ant/deepseek-v4-flash` or `vendor/...`.
function resolve(model) {
  const want = String(model || "").toLowerCase();
  const cands = [];
  for (const p of peers) {
    for (const [svc, info] of servicesOf(p)) {
      const s = svc.toLowerCase();
      let rank = -1;
      if (s === want) rank = 0;
      else if (s.endsWith("/" + want)) rank = 1;
      else if (s.includes(want)) rank = 2;
      if (rank >= 0) cands.push({ rank, svc, p, info });
    }
  }
  if (!cands.length) return null;
  // Prefer free, then exact service ids, then freshest.
  cands.sort((a, b) => {
    const af = a.info.in === 0 ? 0 : 1;
    const bf = b.info.in === 0 ? 0 : 1;
    if (af !== bf) return af - bf;
    if (a.rank !== b.rank) return a.rank - b.rank;
    return (b.p.lastSeen || 0) - (a.p.lastSeen || 0);
  });
  const best = cands[0];
  return {
    service: best.svc,
    peer: best.p,
    // Every other seller offering the same service, in preference order, so the
    // caller can retry when one peer demands payment or has gone offline.
    alternates: cands.filter((c) => c.p.peerId !== best.p.peerId).map((c) => c.p),
    peers: new Set(cands.map((c) => c.p.peerId)).size,
  };
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const c = [];
    req.on("data", (d) => c.push(d));
    req.on("end", () => resolve(Buffer.concat(c)));
    req.on("error", reject);
  });
}

const json = (res, status, obj, extra = {}) => {
  res.writeHead(status, { "content-type": "application/json", connection: "close", ...extra });
  res.end(JSON.stringify(obj));
};

const server = createServer(async (req, res) => {
  try {
    const path = new URL(req.url, "http://x").pathname;

    if (path === "/_antseed/status") {
      const cat = catalog();
      return json(res, 200, {
        ok: true,
        peerId: node.peerId,
        dhtNodeCount: node.dhtNodeCount ?? null,
        peerCount: peers.length,
        modelCount: cat.length,
        freeCount: cat.filter((m) => m.antseed.free).length,
      });
    }

    if (path === "/v1/models") {
      return json(res, 200, { object: "list", data: catalog() });
    }

    if (path === "/v1/chat/completions" && req.method === "POST") {
      let body;
      try {
        body = JSON.parse((await readBody(req)).toString("utf8"));
      } catch (e) {
        return json(res, 400, { error: { message: "bad JSON: " + e.message, type: "invalid_request_error" } });
      }
      const hit = resolve(body.model);
      if (!hit) {
        return json(res, 404, {
          error: {
            message: `No seller advertises "${body.model}" (${peers.length} peers, ${catalog().length} services known)`,
            type: "model_not_found",
          },
        });
      }
      const wire = {
        requestId: randomUUID(),
        method: "POST",
        path: "/v1/chat/completions",
        headers: { "content-type": "application/json" },
        body: enc.encode(JSON.stringify({ ...body, model: hit.service })),
      };

      // Sellers sometimes mislabel a paid model as free, or go offline mid-request.
      // Walk the alternates until one answers with a real completion.
      const queue = [hit.peer, ...(hit.alternates || [])];
      let lastErr = null;
      let lastResp = null;
      for (const peer of queue) {
        try {
          if (body.stream) {
            const r = await node.sendRequestStream(peer, wire, {
              onResponseStart: (resp) => {
                // Only genuine streams get SSE headers; a peer refusal
                // (statusCode >= 400) must stay unwritten so we can fail over.
                if (resp.statusCode >= 400 || res.headersSent) return;
                res.writeHead(200, {
                  "content-type": "text/event-stream",
                  "cache-control": "no-cache",
                  connection: "close",
                });
                log(`stream ${hit.service} -> ${resp.statusCode}`);
              },
              onResponseChunk: (chunk) => {
                if (chunk.data?.length) res.write(Buffer.from(chunk.data));
              },
            });
            if (!res.headersSent) {
              // Peer refused before the stream opened: keep the answer, next peer.
              if (r?.statusCode) lastResp = r;
              lastErr = new Error("peer refused stream (402/payment?)");
              continue;
            }
            if (!res.writableEnded) {
              res.write("data: [DONE]\n\n");
              res.end();
            }
            return;
          }

          const r = await node.sendRequest(peer, wire);
          if ((r?.statusCode || 500) >= 400 && peer !== queue[queue.length - 1]) {
            // Payment-demanding / dead peer: remember it, move to the next.
            lastResp = r;
            log(`${hit.service} -> ${r?.statusCode} via ${peer.peerId.slice(0, 10)}, trying next`);
            continue;
          }
          const raw = r?.body?.length ? Buffer.from(r.body) : Buffer.alloc(0);
          log(`${hit.service} -> ${r?.statusCode} via ${peer.peerId.slice(0, 10)} (${hit.peers} peers)`);
          const ctype = r?.headers?.["content-type"] || "application/json";
          res.writeHead(r?.statusCode || 502, { "content-type": ctype, connection: "close" });
          res.end(raw);
          return;
        } catch (e) {
          lastErr = e;
          log(`peer ${peer.peerId.slice(0, 10)} failed: ${e.message}`);
        }
      }
      if (lastResp) {
        // No seller would serve it without payment — pass their answer through.
        const raw = lastResp.body?.length ? Buffer.from(lastResp.body) : Buffer.alloc(0);
        res.writeHead(lastResp.statusCode, {
          "content-type": lastResp.headers?.["content-type"] || "application/json",
          connection: "close",
        });
        return res.end(raw);
      }
      json(res, 502, {
        error: {
          message: `all ${queue.length} sellers for "${body.model}" failed: ${lastErr?.message || "unknown"}`,
          type: "upstream_error",
        },
      });
      return;
    }

    json(res, 404, { error: { message: `no route for ${req.method} ${path}`, type: "invalid_request_error" } });
  } catch (e) {
    if (!res.headersSent) json(res, 502, { error: { message: "buyer error: " + e.message } });
    else if (!res.writableEnded) res.end();
  }
});

server.listen(PORT, "127.0.0.1", () => log(`buyer HTTP on http://127.0.0.1:${PORT}`));
await node.start();
log("node started peerId=" + node.peerId?.slice(0, 12) + " dht=" + node.dhtNodeCount);
await refresh();
setInterval(() => refresh(), 60_000).unref();
for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, async () => {
    try {
      await node.stop();
    } catch {}
    process.exit(0);
  });
}
