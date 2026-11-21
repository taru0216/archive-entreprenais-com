#!/usr/bin/env python3
"""build_targets_from_master.py — builder の解決済みマスタから crawl targets CSV を生成

マスタの SSOT は builder リポ（factory-entreprenais-com-builder）の
data/municipality-master.csv にある。本スクリプトはそれを入力に、archive の
crawl_cities.py が読む targets CSV（slug,url,pref,display_name）を生成する。

official_url が解決済みの自治体のみ出力する（未解決はクロール対象外）。

使い方:
  python3 scripts/build_targets_from_master.py \
    --master /tmp/municipality-master.csv \
    --out .data/crawl-targets/marche-targets-all.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="マスタ → crawl targets CSV")
    ap.add_argument("--master", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if not os.path.exists(args.master):
        print(f"[ERROR] master not found: {args.master}", file=sys.stderr)
        return 1

    rows = list(csv.DictReader(open(args.master, encoding="utf-8-sig")))
    out = [r for r in rows if (r.get("official_url") or "").strip()]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["slug", "url", "pref", "display_name"])
        w.writeheader()
        for r in out:
            w.writerow({
                "slug": r["slug"],
                "url": r["official_url"],
                "pref": r["prefecture"],
                "display_name": r["name"],
            })
    print(f"[targets] {len(out)} targets written (resolved URLs only) → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
