"""A1 DECISIVE LIVE TEST:
Concurrently capture
  (1) Polymarket's own live-data WS  topic=crypto_prices_chainlink symbol=btc/usd
  (2) the signed Chainlink Data Streams BTC/USD report (feedId 0x00039d9e...) mirrored by GMX
and check they carry the SAME price for the SAME observation second.
Also measure per-second coverage, cadence and end-to-end latency of the Polymarket WS,
and compare those to the historical capture in data/daily/crypto_prices/.
"""
import asyncio, json, os, ssl, time
os.environ.setdefault("REQUESTS_CA_BUNDLE", "/root/.ccr/ca-bundle.crt")
import numpy as np
import requests
import websockets
from eth_abi import decode as abi_decode

CA = "/root/.ccr/ca-bundle.crt"
DUR = float(os.environ.get("DUR", "180"))
STREAM = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"

pm = {}    # obs_sec -> dict(value, full, server_ts, rx)
ds = {}    # obs_sec -> dict(price, bid, ask, rx)
snapshot = {"n": 0, "first_ts": None, "last_ts": None}


async def pm_task():
    ctx = ssl.create_default_context(cafile=CA)
    sub = {"action": "subscribe", "subscriptions": [
        {"topic": "crypto_prices_chainlink", "type": "update", "filters": '{"symbol":"btc/usd"}'}]}
    async with websockets.connect("wss://ws-live-data.polymarket.com", ssl=ctx,
                                  open_timeout=15, ping_interval=20, max_size=8_000_000) as ws:
        await ws.send(json.dumps(sub))
        t0 = time.time()
        while time.time() - t0 < DUR:
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=15)
            except asyncio.TimeoutError:
                break
            rx = time.time()
            if not m.strip():
                continue
            j = json.loads(m)
            p = j.get("payload")
            if isinstance(p, dict) and "data" in p:      # initial snapshot
                arr = p["data"]
                snapshot["n"] = len(arr)
                snapshot["first_ts"] = arr[0]["timestamp"] // 1000
                snapshot["last_ts"] = arr[-1]["timestamp"] // 1000
                for r in arr:
                    pm.setdefault(r["timestamp"] // 1000,
                                  {"value": r["value"], "full": None, "server_ts": None, "rx": rx, "snap": True})
                continue
            if isinstance(p, dict) and "value" in p:
                sec = p["timestamp"] // 1000
                pm[sec] = {"value": p["value"], "full": p.get("full_accuracy_value"),
                           "server_ts": j.get("timestamp", 0) / 1000.0, "rx": rx, "snap": False}


async def ds_task():
    S = requests.Session(); S.verify = CA
    loop = asyncio.get_event_loop()
    t0 = time.time()
    while time.time() - t0 < DUR:
        try:
            r = await loop.run_in_executor(None, lambda: S.get(
                "https://arbitrum-api.gmxinfra.io/signed_prices/latest", timeout=6))
            rx = time.time()
            for x in r.json()["signedPrices"]:
                if x["tokenSymbol"] != "BTC":
                    continue
                raw = bytes.fromhex(x["blob"][2:])
                _, rpt, _, _, _ = abi_decode(["bytes32[3]", "bytes", "bytes32[]", "bytes32[]", "bytes32"], raw)
                fid, vft, obs, _, _, _, price, bid, ask = abi_decode(
                    ["bytes32", "uint32", "uint32", "uint192", "uint192", "uint32", "int192", "int192", "int192"], rpt)
                if ("0x" + fid.hex()).lower() != STREAM:
                    continue
                ds.setdefault(obs, {"price": price, "bid": bid, "ask": ask, "rx": rx, "validFrom": vft})
        except Exception:
            pass
        await asyncio.sleep(0.3)


async def main():
    await asyncio.gather(pm_task(), ds_task())

asyncio.run(main())

live = {k: v for k, v in pm.items() if not v["snap"]}
print(f"=== POLYMARKET WS crypto_prices_chainlink btc/usd ({DUR:.0f}s) ===")
print(f"  initial snapshot rows: {snapshot['n']} covering {snapshot['first_ts']}..{snapshot['last_ts']} "
      f"({(snapshot['last_ts'] or 0)-(snapshot['first_ts'] or 0)}s of history)")
ks = sorted(live)
span = ks[-1] - ks[0] + 1
print(f"  live updates: {len(ks)} distinct seconds over {span}s span = {len(ks)/span:.2%} per-second coverage")
iv = np.diff(ks)
print(f"  inter-update interval: ==1s {np.mean(iv==1):.2%}  <=2s {np.mean(iv<=2):.2%}  max={iv.max()}s")
vals = np.array([live[k]["value"] for k in ks])
print(f"  consecutive identical prices: {np.mean(np.diff(vals)==0):.2%}")
print(f"  sub-cent prices: {np.mean(np.abs(vals*100-np.round(vals*100))>1e-6):.2%}")
print(f"  |Δ| per second: median=${np.median(np.abs(np.diff(vals))):.3f} mean=${np.abs(np.diff(vals)).mean():.3f}")
srv = np.array([live[k]["server_ts"] - k for k in ks])
loc = np.array([live[k]["rx"] - k for k in ks])
hop = np.array([live[k]["rx"] - live[k]["server_ts"] for k in ks])
print("  PUBLISH LAG (msg server timestamp - observation second):")
for q in [5, 25, 50, 75, 90, 99]:
    print(f"    p{q}: {np.percentile(srv,q):.3f}s")
print(f"    min={srv.min():.3f} max={srv.max():.3f} mean={srv.mean():.3f}")
print("  END-TO-END (our receipt - observation second)  [this sandbox is behind an HTTPS proxy]:")
for q in [5, 50, 90, 99]:
    print(f"    p{q}: {np.percentile(loc,q):.3f}s")
print(f"  network hop (rx - server_ts): p50={np.percentile(hop,50):.3f}s p90={np.percentile(hop,90):.3f}s")

print(f"\n=== full_accuracy_value check ===")
n_ok = 0; n_chk = 0
for k in ks:
    f = live[k]["full"]
    if f is None: continue
    n_chk += 1
    if abs(int(f) / 1e18 - live[k]["value"]) < 1e-9:
        n_ok += 1
print(f"  full_accuracy_value/1e18 == value for {n_ok}/{n_chk} messages -> the WS carries the raw "
      f"18-decimal Data Streams answer")

print(f"\n=== CROSS-CHECK vs signed Chainlink Data Streams report (via GMX), feedId {STREAM} ===")
common = sorted(set(ks) & set(ds))
print(f"  observation seconds present in BOTH: {len(common)} (pm={len(ks)}, ds={len(ds)})")
if common:
    dif = []
    exact_full = 0
    for k in common:
        dsp = ds[k]["price"] / 1e18
        dif.append(live[k]["value"] - dsp)
        f = live[k]["full"]
        if f is not None and int(f) == ds[k]["price"]:
            exact_full += 1
    dif = np.array(dif)
    print(f"  EXACT integer match of full_accuracy_value vs Data Streams report price: "
          f"{exact_full}/{len(common)} = {exact_full/len(common):.2%}")
    print(f"  |difference| : max=${np.abs(dif).max():.10f}  median=${np.median(np.abs(dif)):.10f}")
    for k in common[:5]:
        print(f"    sec={k} pm={live[k]['value']:.10f} ds={ds[k]['price']/1e18:.10f} "
              f"pm_full={live[k]['full']} ds_raw={ds[k]['price']}")
    dl = np.array([ds[k]["rx"] - k for k in common])
    pl = np.array([live[k]["rx"] - k for k in common])
    print(f"  latency to us: polymarket-ws p50={np.percentile(pl,50):.3f}s vs gmx-poll p50={np.percentile(dl,50):.3f}s")

json.dump({"pm": {str(k): v for k, v in live.items()},
           "ds": {str(k): {kk: (str(vv) if isinstance(vv, int) and abs(vv) > 2**53 else vv)
                           for kk, vv in v.items()} for k, v in ds.items()}},
          open("/home/user/S4/scripts/a1/pm_vs_ds.json", "w"), default=str)
print("\nsaved pm_vs_ds.json")
