"""Tests never read the developer's real repo/.env (keys there must not change test results)."""

import os

os.environ["CASHDESK_NO_DOTENV"] = "1"
os.environ["CASHDESK_TESTING"] = "1"  # lets Starlette's TestClient host ("testserver") through
