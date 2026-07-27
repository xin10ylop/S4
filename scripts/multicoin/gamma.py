"""Shared gamma/clob helpers for multi-coin hourly recon."""
import json, ssl, urllib.parse, urllib.request, time

CA = "/root/.ccr/ca-bundle.crt"
CTX = ssl.create_default_context(cafile=CA)
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

def get(url, params=None, tries=4, timeout=30):
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "s4-recon/1.0"})
            with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(1.0 + i)
    raise last

def gamma_markets(**params):
    return get(GAMMA + "/markets", params)

def gamma_events(**params):
    return get(GAMMA + "/events", params)

def clob_book(token_id):
    return get(CLOB + "/book", {"token_id": token_id})

def clob_books(token_ids):
    """POST /books for multiple."""
    body = json.dumps([{"token_id": t} for t in token_ids]).encode()
    req = urllib.request.Request(CLOB + "/books", data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "s4-recon/1.0"})
    with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
        return json.loads(r.read().decode())
