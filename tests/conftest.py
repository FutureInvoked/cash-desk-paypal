"""Tests never read the developer's real repo/.env (keys there must not change test results)."""

import os

os.environ["CASHDESK_NO_DOTENV"] = "1"
