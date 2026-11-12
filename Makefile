# Local shortcuts. CI runs the same commands from .github/workflows/ci.yml.
PYTHON ?= python3
export PATH := $(HOME)/.foundry/bin:$(PATH)

.PHONY: test test-python test-contracts validate phase0-help

validate:
	$(PYTHON) scripts/validate_config.py

test-python:
	$(PYTHON) -m pytest tests -q

test-contracts:
	cd contracts && forge test -vv

test: validate test-python test-contracts

phase0-help:
	@echo "cp .env.example .env   # fill keys; never commit .env"
	@echo "docker compose up -d   # LiteLLM on :4000"
	@echo "$(PYTHON) -m muse      # phase 0 service on :4100"
