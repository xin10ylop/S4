"""A1 Part 2(e): actually read the on-chain Chainlink BTC/USD aggregator over public RPCs."""
import json, os, time, sys
os.environ.setdefault("REQUESTS_CA_BUNDLE", "/root/.ccr/ca-bundle.crt")
os.environ.setdefault("SSL_CERT_FILE", "/root/.ccr/ca-bundle.crt")
import requests
from web3 import Web3

AGG_ABI = json.loads("""[
 {"inputs":[],"name":"latestRoundData","outputs":[
   {"internalType":"uint80","name":"roundId","type":"uint80"},
   {"internalType":"int256","name":"answer","type":"int256"},
   {"internalType":"uint256","name":"startedAt","type":"uint256"},
   {"internalType":"uint256","name":"updatedAt","type":"uint256"},
   {"internalType":"uint80","name":"answeredInRound","type":"uint80"}],
  "stateMutability":"view","type":"function"},
 {"inputs":[],"name":"decimals","outputs":[{"internalType":"uint8","name":"","type":"uint8"}],
  "stateMutability":"view","type":"function"},
 {"inputs":[],"name":"description","outputs":[{"internalType":"string","name":"","type":"string"}],
  "stateMutability":"view","type":"function"},
 {"inputs":[],"name":"version","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],
  "stateMutability":"view","type":"function"},
 {"inputs":[],"name":"aggregator","outputs":[{"internalType":"address","name":"","type":"address"}],
  "stateMutability":"view","type":"function"},
 {"inputs":[{"internalType":"uint80","name":"_roundId","type":"uint80"}],"name":"getRoundData","outputs":[
   {"internalType":"uint80","name":"roundId","type":"uint80"},
   {"internalType":"int256","name":"answer","type":"int256"},
   {"internalType":"uint256","name":"startedAt","type":"uint256"},
   {"internalType":"uint256","name":"updatedAt","type":"uint256"},
   {"internalType":"uint80","name":"answeredInRound","type":"uint80"}],
  "stateMutability":"view","type":"function"}
]""")

TARGETS = [
    ("Polygon",  "0xc907e116054Ad103354f2D350FD2514433D57F6f",
     ["https://polygon-rpc.com", "https://polygon-bor-rpc.publicnode.com",
      "https://polygon.llamarpc.com", "https://rpc.ankr.com/polygon",
      "https://1rpc.io/matic", "https://polygon.drpc.org"]),
    ("Ethereum", "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c",
     ["https://eth.llamarpc.com", "https://ethereum-rpc.publicnode.com",
      "https://rpc.ankr.com/eth", "https://eth.drpc.org", "https://cloudflare-eth.com"]),
]

results = {}
for chain, addr, rpcs in TARGETS:
    for rpc in rpcs:
        try:
            t0 = time.time()
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 12}))
            if not w3.is_connected():
                print(f"[{chain}] {rpc}: not connected"); continue
            blk = w3.eth.block_number
            c = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=AGG_ABI)
            desc = c.functions.description().call()
            dec = c.functions.decimals().call()
            ver = c.functions.version().call()
            try:
                inner = c.functions.aggregator().call()
            except Exception:
                inner = "n/a"
            rid, ans, started, updated, air = c.functions.latestRoundData().call()
            dt = time.time() - t0
            now = time.time()
            print(f"\n[{chain}] RPC={rpc}  ok in {dt:.2f}s  block={blk}")
            print(f"  proxy={addr}  description={desc!r} decimals={dec} version={ver} inner_aggregator={inner}")
            print(f"  roundId={rid} answer={ans} -> ${ans/10**dec:,.8f}")
            print(f"  startedAt={started} updatedAt={updated} answeredInRound={air}")
            print(f"  age of answer = {now-updated:.1f}s (wall now={now:.0f})")
            results[chain] = dict(rpc=rpc, addr=addr, desc=desc, dec=dec, ver=ver, inner=str(inner),
                                  roundId=str(rid), price=ans/10**dec, updatedAt=updated,
                                  age_s=now-updated, rtt_s=dt, block=blk)
            break
        except Exception as e:
            print(f"[{chain}] {rpc}: FAIL {type(e).__name__}: {str(e)[:160]}")

# live Binance for comparison
try:
    r = requests.get("https://data-api.binance.vision/api/v3/ticker/price",
                     params={"symbol": "BTCUSDT"}, timeout=10,
                     verify="/root/.ccr/ca-bundle.crt")
    binance = float(r.json()["price"])
    print(f"\n[binance] BTCUSDT spot now = ${binance:,.2f}")
except Exception as e:
    binance = None
    print(f"[binance] FAIL {e}")

for chain, d in results.items():
    if binance:
        print(f"  {chain} onchain ${d['price']:,.2f} - binance ${binance:,.2f} = "
              f"{d['price']-binance:+.2f} ({(d['price']-binance)/binance*1e4:+.1f} bp), answer age {d['age_s']:.0f}s")

json.dump({"onchain": results, "binance": binance}, open("/home/user/S4/scripts/a1/onchain_snapshot.json", "w"), indent=2, default=str)
