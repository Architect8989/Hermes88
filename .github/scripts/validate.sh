#!/usr/bin/env bash
# =============================================================================
# .github/scripts/validate.sh
# Run the full CI suite locally without GitHub Actions.
#
# Usage:
#   bash .github/scripts/validate.sh
#
# Requires: bash, python3, docker, shellcheck (optional)
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

PASS=0; FAIL=0

pass() { echo -e "  ${GREEN}PASS${RESET}  $*"; ((PASS++)); }
fail() { echo -e "  ${RED}FAIL${RESET}  $*"; ((FAIL++)); }
info() { echo -e "\n${CYAN}${BOLD}── $* ${RESET}"; }

# ── 1. .env.example key validation ────────────────────────────────────────────
info "1. Validate .env.example"

REQUIRED=(TELEGRAM_BOT_TOKEN NVIDIA_NIM_API_KEY DO_INFERENCE_API_KEY GITHUB_PAT)
for key in "${REQUIRED[@]}"; do
    if grep -qE "^${key}=" .env.example; then
        pass "$key present in .env.example"
    else
        fail "$key MISSING from .env.example"
    fi
done

# Check for accidentally committed real secrets (tokens are typically long + non-placeholder)
if grep -E "^(TELEGRAM_BOT_TOKEN|NVIDIA_NIM_API_KEY|DO_INFERENCE_API_KEY|GITHUB_PAT|HF_TOKEN)=.{20,}" \
    .env.example 2>/dev/null | grep -qvE "_(here|key_here|token_here|your_)"; then
    fail ".env.example may contain a real secret value — replace with placeholder"
else
    pass "No real secrets detected in .env.example"
fi

# ── 2. Shell script syntax ────────────────────────────────────────────────────
info "2. Shell script syntax (bash -n)"

SHELL_FILES=(
    deploy.sh
    scripts/init_and_start.sh
    scripts/start_openclaw.sh
    .github/scripts/validate.sh
)
for f in "${SHELL_FILES[@]}"; do
    if [ -f "$f" ]; then
        if bash -n "$f" 2>/dev/null; then
            pass "bash -n $f"
        else
            fail "bash -n $f  ← syntax error"
        fi
    else
        fail "$f not found"
    fi
done

if command -v shellcheck &>/dev/null; then
    for f in deploy.sh scripts/init_and_start.sh; do
        if shellcheck -S error "$f" 2>/dev/null; then
            pass "shellcheck $f"
        else
            fail "shellcheck $f  ← see above"
        fi
    done
else
    echo -e "  ${YELLOW}SKIP${RESET}  shellcheck not installed (apt install shellcheck)"
fi

# ── 3. Python syntax ──────────────────────────────────────────────────────────
info "3. Python syntax (py_compile)"

PY_FILES=()
while IFS= read -r -d '' f; do
    PY_FILES+=("$f")
done < <(find . -name "*.py" -not -path "./.git/*" -print0)

for f in "${PY_FILES[@]}"; do
    if python3 -m py_compile "$f" 2>/dev/null; then
        pass "py_compile $f"
    else
        fail "py_compile $f  ← syntax error"
    fi
done

# ── 4. Makefile targets ───────────────────────────────────────────────────────
info "4. Makefile targets"

TARGETS=(deploy up update restart logs status backup shell stop clean destroy help)
for target in "${TARGETS[@]}"; do
    if grep -qE "^${target}:" Makefile; then
        pass "make target: $target"
    else
        fail "make target MISSING: $target"
    fi
done

if make help &>/dev/null; then
    pass "make help runs without error"
else
    fail "make help exited non-zero"
fi

# ── 5. docker-compose.yml ─────────────────────────────────────────────────────
info "5. docker-compose.yml validation"

if command -v docker &>/dev/null && docker compose version &>/dev/null; then
    # Create a dummy .env so compose can resolve all variables
    DUMMY_ENV=$(mktemp)
    while IFS= read -r line; do
        key=$(echo "$line" | cut -d= -f1)
        [[ "$key" =~ ^[A-Z_]+$ ]] && echo "${key}=dummy_value" >> "$DUMMY_ENV"
    done < .env.example

    if docker compose --env-file "$DUMMY_ENV" config --quiet 2>/dev/null; then
        pass "docker-compose.yml parses correctly"
    else
        fail "docker-compose.yml has errors — run: docker compose config"
    fi
    rm -f "$DUMMY_ENV"
else
    echo -e "  ${YELLOW}SKIP${RESET}  docker not available"
fi

# ── 6. Dockerfile.vps build (slow — skipped unless --full flag passed) ────────
info "6. Dockerfile.vps build"

if [[ "${1:-}" == "--full" ]]; then
    if command -v docker &>/dev/null; then
        echo "  Building image (this takes 3–8 minutes)..."
        if docker build -f Dockerfile.vps -t rhodawk-hermes:validate . &>/dev/null; then
            pass "Dockerfile.vps builds successfully"
            docker rmi rhodawk-hermes:validate &>/dev/null || true
        else
            fail "Dockerfile.vps build failed — run: docker build -f Dockerfile.vps ."
        fi
    else
        echo -e "  ${YELLOW}SKIP${RESET}  docker not available"
    fi
else
    echo -e "  ${YELLOW}SKIP${RESET}  Docker build skipped (pass --full to include)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}── Results ─────────────────────────────────────────────────────────${RESET}"
echo -e "  ${GREEN}Passed: ${PASS}${RESET}"
if [ "$FAIL" -gt 0 ]; then
    echo -e "  ${RED}Failed: ${FAIL}${RESET}"
    echo ""
    exit 1
else
    echo -e "  ${RED}Failed: 0${RESET}"
    echo -e "\n  ${GREEN}${BOLD}All checks passed.${RESET}"
    echo ""
fi
