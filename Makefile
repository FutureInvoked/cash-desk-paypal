PY ?= python3.11
VENV := .venv
BIN := $(VENV)/bin

.PHONY: install test demo run audit-verify eval clean

$(BIN)/python:
	@if command -v uv >/dev/null 2>&1; then uv venv -q -p $(PY) $(VENV); else $(PY) -m venv $(VENV); fi

install: $(BIN)/python
	@if command -v uv >/dev/null 2>&1; then uv pip install -q -p $(BIN)/python -e '.[dev]'; else $(BIN)/pip install -q -e '.[dev]'; fi

test: install
	$(BIN)/python -m pytest -q

run: install
	$(BIN)/python -m uvicorn cashdesk.app:app --host 127.0.0.1 --port 8000

demo: install
	@(sleep 2; open http://localhost:8000 2>/dev/null || xdg-open http://localhost:8000 2>/dev/null || true) &
	$(BIN)/python -m uvicorn cashdesk.app:app --host 127.0.0.1 --port 8000

audit-verify: install
	@echo "audit-verify: not implemented yet (backlog E6)"; exit 1

clean:
	rm -rf $(VENV) var/*.db var/*.jsonl

eval: install
	$(BIN)/python tools/run_eval.py
