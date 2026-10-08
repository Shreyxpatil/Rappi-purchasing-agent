.PHONY: setup test demo run docker-up docker-down clean

CASE ?= s1_overstock
PROVIDER ?= scripted

setup:            ## install Python 3.12 deps with uv
	uv sync

test:             ## run the full test suite (no API key needed)
	uv run pytest -o addopts=""

demo:             ## run one scenario end to end and print the trace: make demo CASE=x_supplier_rejects
	cd backend && uv run python -m app.cli $(CASE) --provider $(PROVIDER)

run:              ## start the API on http://localhost:8000 (docs at /docs)
	cd backend && uv run uvicorn app.main:create_app --factory --reload --port 8000

docker-up:        ## build and start the API in Docker
	docker compose up --build

docker-down:
	docker compose down

clean:            ## remove the local database
	rm -rf data
