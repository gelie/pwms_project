# Local entry points that mirror CI. Run `make help` for the list.
#
# CI (.github/workflows/ci.yml) is the source of truth; these targets exist so a
# developer can reproduce a CI failure locally without reading the workflow file.

.PHONY: help install lint lint-fix format migrate-check test coverage check clean

UV ?= uv
MANAGE := $(UV) run python manage.py test

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies (incl. dev) from the lockfile
	$(UV) sync

lint: ## Run ruff check + format check (matches the CI lint job)
	$(UV) run ruff check src/pwms
	$(UV) run ruff format --check src/pwms

lint-fix: ## Apply ruff's safe fixes and formatting
	$(UV) run ruff check --fix src/pwms
	$(UV) run ruff format src/pwms

format: ## Format the codebase
	$(UV) run ruff format src/pwms

migrate-check: ## Fail if a model change has no migration
	$(UV) run python manage.py makemigrations --check --dry-run

test: ## Run the full suite (needs PostgreSQL, see README quickstart)
	$(MANAGE) \
		pwms.tests \
		pwms.tests_api \
		pwms.tests_attachments \
		pwms.tests_authentication \
		pwms.tests_detail \
		pwms.tests_detail_actions \
		pwms.tests_executive \
		pwms.tests_messages \
		pwms.tests_ninja \
		pwms.tests_notifications \
		pwms.tests_referral_access \
		pwms.tests_referrals \
		pwms.tests_reports \
		pwms.tests_transitions

coverage: ## Run the suite under coverage and print a report
	$(UV) run coverage run --source=pwms manage.py test \
		pwms.tests \
		pwms.tests_api \
		pwms.tests_attachments \
		pwms.tests_authentication \
		pwms.tests_detail \
		pwms.tests_detail_actions \
		pwms.tests_executive \
		pwms.tests_messages \
		pwms.tests_ninja \
		pwms.tests_notifications \
		pwms.tests_referral_access \
		pwms.tests_referrals \
		pwms.tests_reports \
		pwms.tests_transitions
	$(UV) run coverage report --show-missing

check: lint migrate-check test ## Everything CI runs, in order

clean: ## Remove caches and coverage data
	rm -rf .ruff_cache .coverage htmlcov
	find src -type d -name __pycache__ -prune -exec rm -rf {} +
