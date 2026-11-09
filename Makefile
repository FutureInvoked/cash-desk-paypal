# first Python >= 3.11 on PATH (override with `make PY=/path/to/python3.x ...`)
PY ?= $(shell for p in python3.13 python3.12 python3.11 python3; do \
         command -v $$p >/dev/null 2>&1 && $$p -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null && { echo $$p; break; }; done)
VENV := .venv
BIN := $(VENV)/bin
WEBHOOK_PORT ?= 8001
PORT ?= 8000

.PHONY: install test demo run webhooks audit-verify eval lock secret-scan clean

$(BIN)/python:
	@test -n "$(PY)" || { echo "Cash Desk needs Python 3.11 or newer on PATH (or run: make PY=/path/to/python3.11 demo)"; exit 1; }
	@if command -v uv >/dev/null 2>&1; then uv venv -q -p $(PY) $(VENV); else $(PY) -m venv $(VENV); fi

# exact versions of every dependency come from requirements.lock (uv pip compile pyproject.toml --extra dev)
install: $(BIN)/python
	@if command -v uv >/dev/null 2>&1; then \
	  uv pip install -q -p $(BIN)/python -r requirements.lock && uv pip install -q -p $(BIN)/python --no-deps -e .; \
	else \
	  $(BIN)/pip install -q -r requirements.lock && $(BIN)/pip install -q --no-deps -e .; \
	fi

test: install
	$(BIN)/python -m pytest -q

run: install
	$(BIN)/python -m uvicorn cashdesk.app:app --host 127.0.0.1 --port $(PORT)

demo: install
	@(sleep 2; open http://localhost:$(PORT) 2>/dev/null || xdg-open http://localhost:$(PORT) 2>/dev/null || true) &
	$(BIN)/python -m uvicorn cashdesk.app:app --host 127.0.0.1 --port $(PORT)

webhooks: install   ## webhook-only server for PayPal events (point a tunnel at this port, never at the board)
	$(BIN)/python -m uvicorn cashdesk.webhook_app:app --host 127.0.0.1 --port $(WEBHOOK_PORT)

audit-verify: install
	$(BIN)/python -m cashdesk.audit verify

clean:
	rm -rf $(VENV) var/*.db var/*.jsonl

eval: install
	$(BIN)/python tools/run_eval.py

lock:
	uv pip compile pyproject.toml --extra dev -q -o requirements.lock --python-version 3.11

secret-scan:
	@bash tools/secret_scan.sh
