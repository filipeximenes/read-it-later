.PHONY: install uninstall

install:
	uv tool install --reinstall .

uninstall:
	uv tool uninstall read-it-later

.PHONY: test lint fmt
test: ## Run the test suite
	uv run pytest -q

# Rules only. The repository has never been formatted end to end, so
# `ruff format --check` is left out until `make fmt` is run over it once.
lint: ## Check lint rules
	uv run ruff check ril tests

fmt: ## Apply formatting
	uv run ruff format ril tests
	uv run ruff check --fix ril tests
