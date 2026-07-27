"""A1 Part 2(e) cont.: measure the on-chain aggregator's real cadence + precision by walking back
historical rounds, and by live-polling. Compare to the captured-series behaviour from Part 1(c)."""
import json, os, time
os.environ.setdefault("REQUESTS_CA_BUNDLE", "/root/.ccr/ca-bundle.crt")
import numpy as np
from web3 import Web3

ABI = json.loads(open("/home/user/S4/scripts/a1/agg_abi.json").read())
POLY = "0xc907e116054Ad103354f2D350FD2514433D57F6f"
RPC = "https://polygon-bor-rpc.publicnode.com"

w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 15}))
c = w3.eth.contract(address=Web3.to_checksum_address(POLY), abi=ABI)
dec = c.functions.decimals().call()
rid, ans, st, up, air = c.functions.latestRoundData().call()
print(f"latest round {rid} ${ans/10**dec:,.8f} updatedAt={up} age={time.time()-up:.1f}s")

N = 220
rows = []
for k in range(N):
    try:
        r = c.functions.getRoundData(rid - k).call()
        rows.append((r[0], r[1] / 10**dec, r[3]))
    except Exception as e:
        print("stop at k=", k, type(e).__name__)
        break
rows = sorted(rows, key=lambda x: x[2])
print(f"\nwalked back {len(rows)} rounds, span {rows[0][2]} .. {rows[-1][2]} "
      f"= {(rows[-1][2]-rows[0][2])/3600:.2f} h")
t = np.array([r[2] for r in rows], dtype=float)
p = np.array([r[1] for r in rows], dtype=float)
iv = np.diff(t)
print(f"\n=== ON-CHAIN POLYGON BTC/USD UPDATE CADENCE (VERIFIED, n={len(iv)} intervals) ===")
for q in [0, 10, 25, 50, 75, 90, 99, 100]:
    print(f"  p{q}: {np.percentile(iv,q):.0f}s")
print(f"  mean={iv.mean():.1f}s  ==1s: {(iv==1).mean():.2%}  <=2s: {(iv<=2).mean():.2%}  "
      f">=20s: {(iv>=20).mean():.2%}  updates/hour ~= {3600/iv.mean():.1f}")
dp = np.abs(np.diff(p))
print(f"  |Δanswer| between rounds: median=${np.median(dp):.2f} mean=${dp.mean():.2f} max=${dp.max():.2f}")
rel = dp / p[:-1] * 100
print(f"  |Δ%| median={np.median(rel):.4f}%  p90={np.percentile(rel,90):.4f}%  max={np.max(rel):.4f}%")
cents = np.all(np.abs(p * 100 - np.round(p * 100)) < 1e-6)
print(f"  every answer quantised to whole cents? {cents}  (sample: {p[:5]})")
subcent = np.mean(np.abs(p * 100 - np.round(p * 100)) > 1e-9)
print(f"  fraction of answers with sub-cent digits: {subcent:.2%}")

# live poll to confirm freshness behaviour
print("\n=== LIVE POLL of latestRoundData (60 s) ===")
seen = {}
t0 = time.time()
lat = []
while time.time() - t0 < 60:
    s = time.time()
    r = c.functions.latestRoundData().call()
    lat.append(time.time() - s)
    seen[r[0]] = (r[1] / 10**dec, r[3])
    time.sleep(2)
ks = sorted(seen)
print(f"  distinct rounds observed in 60s: {len(ks)}")
for k in ks:
    print(f"    round {k} ${seen[k][0]:,.8f} updatedAt={seen[k][1]}")
print(f"  RPC call latency: median={np.median(lat)*1000:.0f}ms p90={np.percentile(lat,90)*1000:.0f}ms "
      f"max={np.max(lat)*1000:.0f}ms")
json.dump({"rounds": [[str(a), b, c_] for a, b, c_ in rows], "rpc_latency_ms": list(np.array(lat)*1000)},
          open("/home/user/S4/scripts/a1/onchain_cadence.json", "w"), indent=1)
