"""python -m mongoarc.view [--port 8787] [--host 127.0.0.1]"""
import argparse

import uvicorn

from mongoarc import db as D


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m mongoarc.view", description="mongoARC live view")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    D.load_env()
    print(f"mongoARC view on http://{args.host}:{args.port}/", flush=True)
    uvicorn.run("mongoarc.view.server:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
