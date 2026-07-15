#!/usr/bin/env python3
"""Sync the Backblaze B2 data vault into data/.

Credentials come from .env via os.environ only. Never prints secrets.
Usage:
    python scripts/sync_data.py --list            # list objects + sizes only
    python scripts/sync_data.py                   # sync everything missing
    python scripts/sync_data.py --prefix X        # sync only keys under prefix X
    python scripts/sync_data.py --workers 8       # parallel downloads
"""
import argparse
import concurrent.futures as cf
import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

load_dotenv(ROOT / ".env")


def client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["DS_ENDPOINT"],
        region_name=os.environ["DS_REGION"],
        aws_access_key_id=os.environ["DS_KEY"],
        aws_secret_access_key=os.environ["DS_SECRET"],
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"}),
    )


def list_objects(prefix=""):
    s3 = client()
    bucket = os.environ["DS_BUCKET"]
    paginator = s3.get_paginator("list_objects_v2")
    out = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            out.append((obj["Key"], obj["Size"]))
    return out


def download_one(key_size):
    key, size = key_size
    dest = DATA / key
    if dest.exists() and dest.stat().st_size == size:
        return ("skip", key, size)
    dest.parent.mkdir(parents=True, exist_ok=True)
    s3 = client()
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        s3.download_file(os.environ["DS_BUCKET"], key, str(tmp))
        tmp.rename(dest)
        return ("ok", key, size)
    except Exception as e:  # noqa: BLE001
        if tmp.exists():
            tmp.unlink()
        return ("err", key, f"{type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--prefix", default="")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    objs = list_objects(args.prefix)
    total = sum(s for _, s in objs)
    print(f"{len(objs)} objects, {total/1e9:.2f} GB under prefix '{args.prefix}'")

    if args.list:
        for k, s in objs:
            print(f"{s:>14,}  {k}")
        return

    done = 0
    errs = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for status, key, info in ex.map(download_one, objs):
            done += 1
            if status == "err":
                errs.append((key, info))
                print(f"[{done}/{len(objs)}] ERROR {key}: {info}", flush=True)
            elif done % 50 == 0 or done == len(objs):
                print(f"[{done}/{len(objs)}] latest: {status} {key}", flush=True)

    if errs:
        print(f"\n{len(errs)} errors; rerun to retry.")
        sys.exit(1)
    print("sync complete")


if __name__ == "__main__":
    main()
