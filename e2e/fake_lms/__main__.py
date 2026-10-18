"""Run the mini LMS: python -m e2e.fake_lms [--port 8765]

Login and token come from the environment or .env (FAKE_LMS_USER, FAKE_LMS_PASSWORD, FAKE_LMS_TOKEN).
Without FAKE_LMS_PASSWORD nobody can log in: answers are never served to anonymous clients.
"""

import argparse
import os
import secrets

import uvicorn
from dotenv import load_dotenv

load_dotenv(".env")
parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8765)
args = parser.parse_args()
if not os.environ.get("FAKE_LMS_PASSWORD"):
    raise SystemExit("Задай FAKE_LMS_PASSWORD в .env (тот же пароль бот использует для входа)")
os.environ.setdefault("FAKE_LMS_TOKEN", secrets.token_hex(16))
uvicorn.run("e2e.fake_lms.app:app", host=args.host, port=args.port)
