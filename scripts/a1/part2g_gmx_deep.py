"""A1 Part 2(f) deep: characterise the GMX oracle-keeper mirror of Chainlink Data Streams BTC/USD.
1) measure per-second coverage + end-to-end latency of the signed report
2) pull GMX historical candles from the SAME feed and compare them to our captured 1s series
   over the capture window -> independent confirmation the capture IS the Data Streams feed."""
import json, os, time, statistics as st
os.environ.setdefault("REQUESTS_CA_BUNDLE", "/root/.ccr/ca-bundle.crt")
import numpy as np
import requests
from eth_abi import decode as abi_decode

S = requests.Session(); S.verify = "/root/.ccr/ca-bundle.crt"
BTC_USD_STREAM = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
HOSTS = ["https://arbitrum-api.gmxinfra.io", "https://arbitrum-api.gmxinfra2.io"]

for h in HOSTS:
    try:
        t0 = time.time(); r = S.get(h + "/prices/tickers", timeout=10); dt = time.time() - t0
        print(f"[host] {h}: HTTP {r.status_code} in {dt*1000:.0f}ms  n={len(r.json())}")
    except Exception as e:
        print(f"[host] {h}: FAIL {e}")

print("\n=== A) 120 s live capture of the signed BTC/USD Data Streams report via GMX ===")
recs = {}
lat = []
t0 = time.time()
errs = 0
while time.time() - t0 < 120:
    try:
        r = S.get(HOSTS[0] + "/signed_prices/latest", timeout=6)
        rx = time.time()
        for x in r.json()["signedPrices"]:
            if x["tokenSymbol"] != "BTC":
                continue
            raw = bytes.fromhex(x["blob"][2:])
            ctx, rpt, rs, ss, rv = abi_decode(["bytes32[3]", "bytes", "bytes32[]", "bytes32[]", "bytes32"], raw)
            fid, vft, obs, nf, lf, exp, price, bid, ask = abi_decode(
                ["bytes32", "uint32", "uint32", "uint192", "uint192", "uint32", "int192", "int192", "int192"], rpt)
            assert ("0x" + fid.hex()).lower() == BTC_USD_STREAM
            if obs not in recs:
                recs[obs] = (price / 1e18, bid / 1e18, ask / 1e18, rx, vft)
                lat.append(rx - obs)
    except Exception:
        errs += 1
    time.sleep(0.35)
ks = sorted(recs)
span = ks[-1] - ks[0] + 1
print(f"  observationsTimestamps captured: {len(ks)} distinct over a {span}s span "
      f"= {len(ks)/span:.1%} per-second coverage (poll every 0.35 s, errs={errs})")
print(f"  all obs timestamps are whole seconds: {all(isinstance(k,int) for k in ks)}")
pr = np.array([recs[k][0] for k in ks])
d = np.diff(pr)
print(f"  consecutive captured reports with IDENTICAL price: {(d==0).mean():.2%}")
print(f"  |Δprice| between consecutive captured seconds: median=${np.median(np.abs(d)):.3f} "
      f"mean=${np.abs(d).mean():.3f} max=${np.abs(d).max():.2f}")
sub = np.mean(np.abs(pr*100 - np.round(pr*100)) > 1e-6)
print(f"  fraction of prices with sub-cent digits: {sub:.2%}")
print(f"  END-TO-END LATENCY (local receipt - observationsTimestamp), n={len(lat)}:")
for q in [5, 25, 50, 75, 90, 99]:
    print(f"    p{q}: {np.percentile(lat,q):.3f}s")
print(f"    min={min(lat):.3f}s max={max(lat):.3f}s mean={np.mean(lat):.3f}s")
vf = np.array([recs[k][4] for k in ks]); obsa = np.array(ks)
print(f"  validFrom vs observations: obs-validFrom median={np.median(obsa-vf):.0f}s "
      f"(report covers [validFrom, observations])")
sp = np.array([recs[k][2] - recs[k][1] for k in ks])
print(f"  ask-bid spread inside the report: median=${np.median(sp):.3f} p90=${np.percentile(sp,90):.3f}")

print("\n=== B) GMX historical candles from the SAME feed vs our captured 1s series ===")
for period in ["1m", "5m"]:
    try:
        r = S.get(HOSTS[0] + "/prices/candles", params={"tokenSymbol": "BTC", "period": period, "limit": 5000}, timeout=25)
        print(f"  /prices/candles?period={period} -> HTTP {r.status_code}")
        if r.status_code == 200:
            j = r.json()
            c = j.get("candles", j)
            print(f"    n={len(c)} first={c[-1] if c else None} last={c[0] if c else None}")
            json.dump(j, open(f"/home/user/S4/scripts/a1/gmx_candles_{period}.json", "w"))
    except Exception as e:
        print(f"  candles {period} FAIL {e}")
