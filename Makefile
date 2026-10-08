.PHONY: setup test demo eval eval-real run docker-up docker-down clean

CASE ?= s1_overstock
PROVIDER ?= scripted

setup:            ## install Python 3.12 deps with uv
	uv sync

test:             ## run the full test suite (no API key needed)
	uv run pytest -o addopts=""

demo:             ## run one scenario end to end and print the trace: make demo CASE=x_supplier_rejects
	cd backend && uv run python -m app.cli $(CASE) --provider $(PROVIDER)

eval:             ## scripted evals: all 17 cases x 3 runs, writes evals/report.md (~10 s)
	uv run python -m evals.run_evals

eval-real:        ## real-model evals: default subset x 1 run on Groq and Gemini, Gemini as judge (~30-40 min on free tiers)
	uv run python -m evals.run_evals --provider openai_compat gemini --judge gemini

run:              ## start the API on http://localhost:8000 (docs at /docs)
	cd backend && uv run uvicorn app.main:create_app --factory --reload --port 8000

docker-up:        ## build and start the API in Docker
	docker compose up --build

docker-down:
	docker compose down

clean:            ## remove the local database
	rm -rf data
