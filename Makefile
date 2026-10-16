.PHONY: install services stop checks tools
install:
	uv sync --frozen
	npm ci --ignore-scripts
services:
	docker compose up -d
stop:
	docker compose stop
checks:
	uv lock --check
	uv pip check
	uv run pre-commit run --all-files
	npm run format:check
	gitleaks git . --redact --no-banner
tools:
	UV_TOOL_DIR="$(CURDIR)/.tools" UV_TOOL_BIN_DIR="$(CURDIR)/.tools/bin" uv tool install semgrep==1.181.0 --python 3.12
