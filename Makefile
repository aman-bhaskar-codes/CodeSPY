.PHONY: install services bootstrap-local stop checks verify tools
install:
	uv sync --all-packages --all-groups --frozen
	npm ci --ignore-scripts
	fnm exec --using 22 npm --prefix frontend ci --ignore-scripts
services:
	docker compose up -d --wait
bootstrap-local:
	bash scripts/bootstrap_local.sh
stop:
	docker compose stop
checks:
	uv lock --check
	uv pip check
	uv run pre-commit run --all-files
	npm run format:check
	gitleaks git . --redact --no-banner
verify:
	bash scripts/verify_phase0.sh --scope local
tools:
	UV_TOOL_DIR="$(CURDIR)/.tools" UV_TOOL_BIN_DIR="$(CURDIR)/.tools/bin" uv tool install semgrep==1.181.0 --python 3.12
