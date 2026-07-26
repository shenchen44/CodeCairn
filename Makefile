PYTHON ?= python

.PHONY: install run-api run-worker migrate test fmt

install:
	$(PYTHON) -m pip install -e .

run-api:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

run-worker:
	$(PYTHON) -m app.workers.poller

migrate:
	alembic upgrade head

test:
	pytest app/tests -q
