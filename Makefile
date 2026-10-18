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

.PHONY: phase1-unit phase1-test phase1-demo phase1-performance
PHASE1_PYTHON = uv run --frozen --all-packages --all-groups python
phase1-unit:
	uv run --frozen --all-packages --all-groups pytest -c backend/pyproject.toml backend/tests/unit -o 'addopts=-ra --strict-markers' -q
phase1-test:
	$(PHASE1_PYTHON) scripts/phase1_db.py test --env-file .phase1-test.env --suite integration
	$(PHASE1_PYTHON) scripts/phase1_db.py test --env-file .phase1-test.env --suite migrations
phase1-demo:
	$(PHASE1_PYTHON) scripts/phase1_demo.py --env-file .phase1-test.env
phase1-performance:
	$(PHASE1_PYTHON) scripts/phase1_db.py test --env-file .phase1-test.env --suite performance
