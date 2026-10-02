PY ?= python
# Recipes that cd before running Python need an absolute interpreter path, so a relative PY still works.
# A bare name such as python is looked up on PATH, which cd does not change.
PY_ABS := $(if $(findstring /,$(PY)),$(abspath $(PY)),$(PY))
# A fresh home per make run, so no test sees state from an earlier run. CI sets PROFILE itself.
RUN_ID := $(shell date +%s)
PROFILE ?= $(CURDIR)/.tmp/chaos-home-$(RUN_ID)
SCRIPTS := chaos_trader/trading/scripts

.PHONY: bootstrap test compile safety-check gitleaks-scan vendor-check history-scan dep-audit verify

bootstrap:
	$(PY) -m venv .venv
	.venv/Scripts/python -m pip install --upgrade pip 2>/dev/null || .venv/bin/python -m pip install --upgrade pip
	.venv/Scripts/python -m pip install -e . 2>/dev/null || .venv/bin/python -m pip install -e .

compile:
	mkdir -p $(PROFILE)/trading/db $(PROFILE)/trading/reports $(PROFILE)/trading/alpha
	cd $(SCRIPTS) && CHAOS_HOME=$(PROFILE) $(PY_ABS) -W error::ResourceWarning -m py_compile *.py

test:
	mkdir -p $(PROFILE)/trading/db $(PROFILE)/trading/reports $(PROFILE)/trading/alpha
	cd $(SCRIPTS) && CHAOS_HOME=$(PROFILE) $(PY_ABS) -W error::ResourceWarning -m unittest discover -s . -p 'test_*.py'
	CHAOS_HOME=$(PROFILE) $(PY) -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py'

safety-check:
	$(PY) tools/safety_check.py

gitleaks-scan:
	gitleaks detect --source . --log-opts="--all" --no-banner --redact

vendor-check:
	$(PY) -m unittest tests.test_no_vendor_names -v

history-scan:
	$(PY) tools/history_name_scan.py

# Audits what CI audits: the Requires-Dist lines of a freshly built wheel. Without the `build` package it
# audits the project directory instead; the CI wheel audit is the source of truth.
dep-audit:
	@if $(PY) -m build --version >/dev/null 2>&1; then \
		rm -rf .tmp/dep-audit && \
		$(PY) -m build --wheel --outdir .tmp/dep-audit . >/dev/null && \
		$(PY) -c "import glob, zipfile; z = zipfile.ZipFile(glob.glob('.tmp/dep-audit/*.whl')[0]); meta = next(n for n in z.namelist() if n.endswith('.dist-info/METADATA')); reqs = [l.split(':', 1)[1].strip() for l in z.read(meta).decode().splitlines() if l.startswith('Requires-Dist:')]; reqs = [r.split(';', 1)[0].strip() if 'extra ==' in r else r for r in reqs]; open('.tmp/dep-audit/requirements.txt', 'w').write(''.join(r + '\n' for r in reqs))" && \
		$(PY) -m pip_audit --strict --progress-spinner off -r .tmp/dep-audit/requirements.txt; \
	else \
		echo "dep-audit: the build package is missing, so this audits the project directory; CI audits the wheel"; \
		$(PY) -m pip_audit --strict --progress-spinner off .; \
	fi

verify: safety-check vendor-check compile test gitleaks-scan history-scan dep-audit
