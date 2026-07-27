"""A1: test Polymarket's own live-data websocket for the Chainlink BTC/USD price it resolves on."""
import asyncio, json, os, ssl, time
import numpy as np
import websockets

CA = "/root/.ccr/ca-bundle.crt"
ctx = ssl.create_default_context(cafile=CA)
URL = "wss://ws-live-data.polymarket.com"

SUBS = [
    {"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "update", "filters": '{"symbol":"btc/usd"}'}]},
    {"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"btc/usd"}'}]},
    {"action": "subscribe", "subscriptions": [{"topic": "crypto_prices", "type": "update", "filters": '{"symbol":"btc/usd"}'}]},
]

async def run(sub, seconds=75):
    got = []
    raw0 = None
    try:
        async with websockets.connect(URL, ssl=ctx, open_timeout=15, ping_interval=20) as ws:
            await ws.send(json.dumps(sub))
            t0 = time.time()
            while time.time() - t0 < seconds:
                try:
                    m = await asyncio.wait_for(ws.recv(), timeout=12)
                except asyncio.TimeoutError:
                    break
                rx = time.time()
                if raw0 is None:
                    raw0 = m[:600]
                try:
                    j = json.loads(m)
                except Exception:
                    continue
                got.append((rx, j))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return {"n": len(got), "first_raw": raw0, "msgs": got}


async def main():
    for i, sub in enumerate(SUBS):
        print(f"\n=== SUB {i}: {json.dumps(sub)[:160]} ===")
        r = await run(sub, 75 if i == 0 else 25)
        if "error" in r:
            print("  ERROR:", r["error"]); continue
        print(f"  messages: {r['n']}")
        print(f"  first raw: {r['first_raw']}")
        if r["n"] < 3:
            continue
        rows = []
        for rx, j in r["msgs"]:
            p = j.get("payload", j)
            if isinstance(p, list):
                p = p[0] if p else {}
            rows.append((rx, p))
        keys = sorted({k for _, p in rows if isinstance(p, dict) for k in p})
        print(f"  payload keys: {keys}")
        # try to find timestamp + value fields
        tsk = [k for k in keys if "time" in k.lower() or k.lower() in ("t", "ts")]
        vk = [k for k in keys if any(s in k.lower() for s in ("price", "value"))]
        print(f"  timestamp-ish keys={tsk}  value-ish keys={vk}")
        for rx, p in rows[:3]:
            print("   sample:", json.dumps(p)[:300])
        if tsk and vk:
            tk, pk = tsk[0], vk[0]
            ts, px, lat = [], [], []
            for rx, p in rows:
                try:
                    t = float(p[tk]); v = float(p[pk])
                except Exception:
                    continue
                # normalise units
                if t > 1e14: t /= 1e6
                elif t > 1e11: t /= 1e3
                ts.append(t); px.append(v); lat.append(rx - t)
            if len(ts) > 5:
                ts = np.array(ts); px = np.array(px); lat = np.array(lat)
                print(f"  n parsed={len(ts)}  whole-second timestamps: {(ts%1==0).mean():.1%}")
                iv = np.diff(np.unique(ts))
                print(f"  inter-message interval: median={np.median(iv):.2f}s  ==1s {(iv==1).mean():.1%}")
                print(f"  sub-cent prices: {np.mean(np.abs(px*100-np.round(px*100))>1e-6):.1%}")
                print(f"  LATENCY (local_rx - msg_timestamp): p5={np.percentile(lat,5):.3f} "
                      f"p50={np.percentile(lat,50):.3f} p90={np.percentile(lat,90):.3f} max={lat.max():.3f}")
                print(f"  sample prices: {px[:5]}")
                json.dump({"ts": ts.tolist(), "px": px.tolist(), "lat": lat.tolist()},
                          open("/home/user/S4/scripts/a1/pm_ws_capture.json", "w"))

asyncio.run(main())
