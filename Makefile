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

.PHONY: phase2-unit phase2-test phase2-capacity phase2-local-check
PHASE2_ENV ?= work/phase2-test.env
PHASE2_IMAGE ?= work/admission-image.id
phase2-unit:
	uv run --frozen --all-packages --all-groups pytest -c backend/pyproject.toml backend/tests/unit backend/tests/phase2/unit backend/tests/phase2/ports
phase2-test:
	CODESPY_ADMISSION_IMAGE=$$(cat $(PHASE2_IMAGE)) $(PHASE1_PYTHON) scripts/phase2_db.py test --env-file $(PHASE2_ENV) --suite all
phase2-capacity:
	CODESPY_ADMISSION_IMAGE=$$(cat $(PHASE2_IMAGE)) $(PHASE1_PYTHON) scripts/phase2_db.py test --env-file $(PHASE2_ENV) --suite performance
phase2-local-check:
	$(PHASE1_PYTHON) scripts/phase2_local.py --env-file $(PHASE2_ENV) --image-file $(PHASE2_IMAGE) --check

# Capability-based public commands; existing harness entrypoints remain compatible.
.PHONY: unit database-unit database-prepare database-test database-demo database-performance database-cleanup admission-image structural-image source-prepare ingestion-test scanner-contract-test structural-test local-demo source-cleanup
DATABASE_ENV ?= .local/database-tests.env
SOURCE_ENV ?= work/source-tests.env
unit:
	$(PHASE1_PYTHON) -m pytest -c backend/pyproject.toml backend/tests/unit backend/tests/phase2/unit backend/tests/phase2/ports backend/tests/phase3/unit backend/tests/phase4/unit
database-unit: phase1-unit
database-prepare:
	mkdir -p $(dir $(DATABASE_ENV))
	$(PHASE1_PYTHON) scripts/phase1_db.py prepare --env-out $(DATABASE_ENV)
database-test:
	$(PHASE1_PYTHON) scripts/phase1_db.py test --env-file $(DATABASE_ENV) --suite integration
	$(PHASE1_PYTHON) scripts/phase1_db.py test --env-file $(DATABASE_ENV) --suite migrations
database-demo:
	$(PHASE1_PYTHON) scripts/phase1_demo.py --env-file $(DATABASE_ENV)
database-performance:
	$(PHASE1_PYTHON) scripts/phase1_db.py test --env-file $(DATABASE_ENV) --suite performance
database-cleanup:
	$(PHASE1_PYTHON) scripts/phase1_db.py cleanup --env-file $(DATABASE_ENV)
admission-image:
	$(PHASE1_PYTHON) scripts/build_admission_image.py --image-out work/admission-image.id
structural-image:
	$(PHASE1_PYTHON) scripts/build_analysis_image.py --image-out work/analysis-image.id
source-prepare:
	$(PHASE1_PYTHON) scripts/phase3_db.py prepare --env-out $(SOURCE_ENV)
ingestion-test:
	CODESPY_ADMISSION_IMAGE=$$(cat work/admission-image.id) $(PHASE1_PYTHON) scripts/phase2_db.py test --env-file $(SOURCE_ENV) --suite all
scanner-contract-test:
	CODESPY_ADMISSION_IMAGE=$$(cat work/admission-image.id) $(PHASE1_PYTHON) scripts/phase3_db.py test --env-file $(SOURCE_ENV) --suite phase4
structural-test:
	CODESPY_ANALYSIS_IMAGE=$$(cat work/analysis-image.id) $(PHASE1_PYTHON) scripts/phase3_db.py test --env-file $(SOURCE_ENV)
local-demo:
	$(PHASE1_PYTHON) scripts/phase2_local.py --env-file $(SOURCE_ENV) --image-file work/admission-image.id --analysis-image-file work/analysis-image.id
source-cleanup:
	$(PHASE1_PYTHON) scripts/phase3_db.py cleanup --env-file $(SOURCE_ENV)
