"""tierlm CLI.

  python -m tierlm serve [--catalog catalog.yaml] [--scores scores.yaml] [--host 127.0.0.1] [--port 8787]
  python -m tierlm calibrate [--only id1,id2]      run the probe set, write scores.yaml
  python -m tierlm tiers                           show tiers, models, scores and which providers have keys
  python -m tierlm discover <provider>             list the provider's /models (to update the catalog)"""
import argparse
import asyncio
import os

import httpx

from .catalog import load
from .router import Router


def main():
    ap = argparse.ArgumentParser(prog="tierlm")
    ap.add_argument("cmd", choices=["serve", "calibrate", "tiers", "discover"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--catalog", default=os.environ.get("TIERLM_CATALOG", "catalog.yaml"))
    ap.add_argument("--scores", default=os.environ.get("TIERLM_SCORES", "scores.yaml"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    cat = load(a.catalog, a.scores)
    if a.cmd == "serve":
        import uvicorn
        from .server import create_app
        uvicorn.run(create_app(router=Router(cat)), host=a.host, port=a.port)
    elif a.cmd == "calibrate":
        from .calibrate import calibrate
        asyncio.run(calibrate(Router(cat), a.scores, [x for x in a.only.split(",") if x] or None))
    elif a.cmd == "tiers":
        for t, floor in cat.tiers:
            print(f"{t} (score >= {floor:g})")
            for m in cat.models_in(t, usable_only=False):
                key = "key" if cat.providers[m.provider].api_key else "no key"
                print(f"  {m.score:5.1f}{'*' if m.calibrated else ' '} {m.id:45s} {key}")
        print("* = calibrated")
    elif a.cmd == "discover":
        p = cat.providers[a.arg]
        r = httpx.get(f"{p.base_url}/models", headers={"Authorization": f"Bearer {p.api_key}", **p.headers}, timeout=30)
        for m in r.json().get("data", []):
            print(m.get("id"))


if __name__ == "__main__":
    main()
