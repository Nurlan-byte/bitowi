"""Run the mini LMS: python -m e2e.fake_lms [--port 8765]"""

import argparse

import uvicorn

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8765)
args = parser.parse_args()
uvicorn.run("e2e.fake_lms.app:app", host=args.host, port=args.port)
