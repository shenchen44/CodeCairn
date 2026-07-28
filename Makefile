PYTHON ?= uv run

.PHONY: install test check review

install:
	uv sync
	npm --prefix packages/pi-extension install

test:
	$(PYTHON) pytest -q

check:
	$(PYTHON) python -m compileall -q codecairn
	npm --prefix packages/pi-extension run check

review:
	$(PYTHON) cairn review
