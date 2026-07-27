"""A1 Part 2(f): probe public real-time mirrors of Chainlink Data Streams.
Focus: GMX's oracle-keeper (which republishes signed Data Streams reports) - decode the blob and
check whether the feedId matches the Chainlink mainnet BTC/USD Data Stream."""
import json, os, time
os.environ.setdefault("REQUESTS_CA_BUNDLE", "/root/.ccr/ca-bundle.crt")
import requests
from eth_abi import decode as abi_decode

CA = "/root/.ccr/ca-bundle.crt"
S = requests.Session()
S.verify = CA

BTC_USD_STREAM = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"

def get(url, **kw):
    r = S.get(url, timeout=20, **kw)
    r.raise_for_status()
    return r.json()

print("=== GMX oracle keeper signed_prices/latest -> BTC entry ===")
sp = get("https://arbitrum-api.gmxinfra.io/signed_prices/latest")["signedPrices"]
btc = [x for x in sp if x["tokenSymbol"] in ("BTC", "WBTC", "BTC.b")]
for b in btc:
    print(f"  symbol={b['tokenSymbol']} oracleType={b['oracleType']} keeper={b['oracleKeeperKey']} "
          f"fetchType={b['oracleKeeperFetchType']} minBlockTimestamp={b['minBlockTimestamp']} "
          f"maxBlockTimestamp={b['maxBlockTimestamp']} createdAt={b['createdAt']}")
    print(f"    minPriceFull={b['minPriceFull']} maxPriceFull={b['maxPriceFull']}")
    blob = b.get("blob")
    if not blob:
        print("    no blob"); continue
    raw = bytes.fromhex(blob[2:])
    try:
        ctx, rpt, rs, ss, rawvs = abi_decode(["bytes32[3]", "bytes", "bytes32[]", "bytes32[]", "bytes32"], raw)
        print(f"    reportContext[0] (configDigest)=0x{ctx[0].hex()}  signers={len(rs)}")
    except Exception as e:
        print("    outer decode failed:", e); rpt = raw
    # v3 schema: feedId, validFromTimestamp, observationsTimestamp, nativeFee, linkFee, expiresAt, price, bid, ask
    try:
        fid, vft, obs, nfee, lfee, exp, price, bid, ask = abi_decode(
            ["bytes32", "uint32", "uint32", "uint192", "uint192", "uint32", "int192", "int192", "int192"], rpt)
        print(f"    feedId=0x{fid.hex()}")
        print(f"    validFrom={vft} observations={obs} expiresAt={exp} "
              f"(now={int(time.time())}, obs age={int(time.time())-obs}s)")
        print(f"    price={price/1e18:,.8f}  bid={bid/1e18:,.8f}  ask={ask/1e18:,.8f}")
        print(f"    MATCHES chainlink mainnet BTC/USD stream? {('0x'+fid.hex()).lower()==BTC_USD_STREAM.lower()}")
    except Exception as e:
        print("    v3 decode failed:", e)

print("\n=== GMX tickers cadence probe (30 s) ===")
seen = {}
t0 = time.time()
lat = []
while time.time() - t0 < 30:
    s = time.time()
    try:
        tk = get("https://arbitrum-api.gmxinfra.io/prices/tickers")
    except Exception as e:
        print("  err", e); time.sleep(1); continue
    lat.append(time.time() - s)
    for t in tk:
        if t["tokenSymbol"] == "BTC":
            seen[t["timestamp"]] = (int(t["minPrice"]), int(t["maxPrice"]), t["updatedAt"])
    time.sleep(1)
ks = sorted(seen)
print(f"  distinct BTC 'timestamp' values in 30 s: {len(ks)}  -> {ks[:5]} ... {ks[-3:]}")
if len(ks) > 1:
    import numpy as np
    print(f"  inter-update: {np.diff(ks)}")
print(f"  HTTP latency median={sorted(lat)[len(lat)//2]*1000:.0f}ms")
for k in ks[-3:]:
    mn, mx, up = seen[k]
    print(f"    ts={k} min=${mn/1e22:,.4f} max=${mx/1e22:,.4f} updatedAt={up}")

print("\n=== Pyth Hermes BTC/USD (free, no auth) ===")
PY = "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
j = get("https://hermes.pyth.network/v2/updates/price/latest", params={"ids[]": PY, "encoding": "hex"})
p = j["parsed"][0]["price"]
print(f"  price={int(p['price'])*10**int(p['expo']):,.4f} conf={int(p['conf'])*10**int(p['expo']):,.2f} "
      f"publish_time={p['publish_time']} age={int(time.time())-p['publish_time']}s")

print("\n=== live Binance for reference ===")
b = get("https://data-api.binance.vision/api/v3/ticker/price", params={"symbol": "BTCUSDT"})
print(f"  BTCUSDT ${float(b['price']):,.2f}")
