#!/usr/bin/env bash
# JobsFlow single reproducible install/verify entrypoint ("source-checkout run" mode).
#
# This repository is NOT a pip-installable package (root setup.py is the
# setup wizard).  The supported install is: clone, create a venv, run this
# script, then run the product from the checkout.  README and CI use these
# same commands.
#
#   python3 -m venv .venv && source .venv/bin/activate
#   bash tools/setup_env.sh [--dev]            # end users: runtime deps
#   bash tools/setup_env.sh --dev              # contributors/CI: + pytest/mypy/cov
#   bash tools/setup_env.sh --check-only       # offline verification, no installs
#   bash tools/setup_env.sh --dev --strict     # CI: also require soffice
#
# Install order is load-bearing: the unified hash lock first (it now pins
# SOP Control's runtime deps), then the vendored tree with --no-deps so pip
# can never freely resolve a second, unpinned dependency set from the network.
set -euo pipefail

DEV=0
CHECK_ONLY=0
STRICT=0
for arg in "$@"; do
    case "$arg" in
        --dev) DEV=1 ;;
        --check-only) CHECK_ONLY=1 ;;
        --strict) STRICT=1 ;;
        *) echo "ERROR: unknown argument: $arg" >&2; exit 2 ;;
    esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$DEV" -eq 1 ]; then
    LOCK="$REPO/requirements-dev.lock"
else
    LOCK="$REPO/requirements.lock"
fi

# An activated virtualenv always wins: never bypass it just because another
# interpreter exists elsewhere on PATH.  In CI, actions/setup-python makes
# the matrix interpreter the plain `python`; that interpreter must remain the
# one we install into and the one subsequent steps invoke.  Otherwise probe
# PATH plus well-known locations, but validate the version before selecting a
# candidate (a bare macOS `python3` may still be 3.9).
if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    PYTHON_BIN="$VIRTUAL_ENV/bin/python"
else
    PYTHON_BIN=""
    for candidate in \
        "${JOBSFLOW_PYTHON:-}" \
        "${pythonLocation:-}/bin/python" \
        "${Python_ROOT_DIR:-}/bin/python" \
        "$(command -v python 2>/dev/null)" \
        "$(command -v python3 2>/dev/null)" \
        "$(command -v python3.12 2>/dev/null)" \
        "$(command -v python3.11 2>/dev/null)" \
        "$(command -v python3.10 2>/dev/null)" \
        /opt/homebrew/bin/python3.12 \
        /opt/homebrew/bin/python3.11 \
        /opt/homebrew/bin/python3.10 \
        /usr/local/bin/python3.12 \
        /usr/local/bin/python3.11 \
        /usr/local/bin/python3.10; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
                PYTHON_BIN="$candidate"
                break
            fi
        fi
    done
fi
[ -n "${PYTHON_BIN:-}" ] || { echo "ERROR: no Python interpreter found (need 3.10+)" >&2; exit 1; }

step() { echo "==> $*"; }
warn() { echo "WARNING: $*" >&2; }
fail() { echo "ERROR: $*" >&2; exit 1; }

step "check 1/7: python version ($PYTHON_BIN)"
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || fail "JobsFlow requires Python 3.10+ (CI uses 3.12)"
"$PYTHON_BIN" -c 'import sys; print("python", sys.version.split()[0])'

step "check 2/7: product checkout layout (CWD: $PWD)"
test -f "$REPO/tools/setup_env.sh" || fail "not a product checkout: $REPO"
test -d "$REPO/tools" || fail "not a product checkout: $REPO"
test -d "$REPO/vendor/sopcontrol" || fail "not a product checkout: $REPO"
if [ "$(pwd -P)" != "$REPO" ]; then
    echo "NOTE: running from outside the checkout; REPO resolved to $REPO"
fi

step "check 3/7: lock file present ($LOCK)"
test -f "$LOCK" || fail "lock file missing: $LOCK"

if [ "$CHECK_ONLY" -eq 0 ]; then
    step "installing unified lock"
    "$PYTHON_BIN" -m pip install --require-hashes -r "$LOCK"
    step "installing vendored SOP Control without dependency resolution"
    test -f "$REPO/vendor/sopcontrol/plugins/__init__.py"
    test -f "$REPO/vendor/sopcontrol/sopcontrol/__init__.py"
    test -f "$REPO/vendor/sopcontrol/VENDOR_MANIFEST.json"
    "$PYTHON_BIN" -m pip install --no-deps -e "$REPO/vendor/sopcontrol"
fi

step "check 4/7: vendor pin/manifest/import"
"$PYTHON_BIN" "$REPO/tools/vendorize_sopcontrol.py" --verify
# Mirror the runtime resolution in tools/workflow/sopcontrol_adapter.py:
# an installed dist wins for the sopctl CLI, otherwise the vendored tree
# on sys.path is what the workflow gateway actually imports.
"$PYTHON_BIN" - "$REPO" <<'EOF'
import importlib.metadata as metadata
import json
import sys

repo = sys.argv[1]
manifest = json.load(open(repo + "/vendor/sopcontrol/VENDOR_MANIFEST.json"))
try:
    dist_version = metadata.version("sopcontrol")
except metadata.PackageNotFoundError:
    dist_version = None
if dist_version is None:
    sys.path.insert(0, repo + "/vendor/sopcontrol")
import sopcontrol

source = "installed-dist" if dist_version else "vendor-dir"
assert sopcontrol.__version__ == manifest["version"], (sopcontrol.__version__, manifest["version"])
if dist_version:
    assert dist_version == manifest["version"], (dist_version, manifest["version"])
print(f"sopcontrol {sopcontrol.__version__} via {source}")
if dist_version is None:
    print("NOTE: sopctl CLI needs the editable install above; workflow runtime works from vendor-dir")
EOF

step "check 5/7: sopctl runnable"
if ! "$PYTHON_BIN" -m sopcontrol.cli --help >/dev/null 2>&1; then
    fail "sopctl CLI is not runnable (expected: pip install --no-deps -e vendor/sopcontrol)"
fi
echo "sopctl CLI runnable"

step "check 6/7: headless LibreOffice (maintained PDF path)"
if command -v soffice >/dev/null 2>&1; then
    soffice --version
elif [ -x "/Applications/LibreOffice.app/Contents/MacOS/soffice" ]; then
    echo "soffice via /Applications/LibreOffice.app"
elif [ "$STRICT" -eq 1 ]; then
    fail "soffice not found; the materials PDF stage requires headless LibreOffice"
else
    warn "soffice not found; 'materials pdf' will fail until LibreOffice is installed"
fi

step "check 7/7: import product surface"
cd "$REPO"
"$PYTHON_BIN" -c "import tools.workflow; print('tools.workflow importable')"
"$PYTHON_BIN" -c "import sopcontrol; print('sopcontrol importable')"

step "setup_env OK (dev=$DEV check_only=$CHECK_ONLY strict=$STRICT)"
