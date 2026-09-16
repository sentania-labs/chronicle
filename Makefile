# One definition, two callers: a developer and CI invoke exactly these
# targets. CI never hand-copies a command; if a gate changes, it changes here.
SHELL := /bin/bash
.PHONY: setup lint typecheck test prose-check check \
        build build-api build-builder build-preview \
        compose-up compose-down

HUGO_VERSION ?= 0.164.0

setup:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy chronicle tests

test:
	uv run pytest -q

prose-check:
	bash ci/prose-check.sh

# Everything a pull request must pass before an image is built.
check: lint typecheck test prose-check

build: build-api build-builder build-preview

build-api:
	docker build --target api -t chronicle/api:local .

build-builder:
	docker build --target builder --build-arg HUGO_VERSION=$(HUGO_VERSION) \
		-t chronicle/builder:local .

build-preview:
	docker build --target preview -t chronicle/preview:local .

# Local development only; the lab's real deployment is examples/k8s/, not
# compose.
compose-up:
	docker compose up -d

compose-down:
	docker compose down
