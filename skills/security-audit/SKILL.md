# Security Audit Skill — Rhodawk Engine

## Trigger
User sends a GitHub repo URL with "scan", "audit", "vulnerabilities", or "CVE".

## Protocol
REPO_URL=$1
REPO_NAME=$(basename $REPO_URL .git)
CLONE_PATH=/tmp/audit/$REPO_NAME

# Clone
git clone --depth 1 $REPO_URL $CLONE_PATH 2>&1 | tail -3

# Static analysis stack
cd $CLONE_PATH

# Python
pip install bandit safety semgrep -q --break-system-packages 2>/dev/null
bandit -r . -f json -q 2>/dev/null > /tmp/${REPO_NAME}_bandit.json
safety check --json 2>/dev/null > /tmp/${REPO_NAME}_safety.json

# Multi-language (semgrep)
semgrep --config auto --json --quiet 2>/dev/null \
  | python3 -c "import json,sys; d=json.load(sys.stdin);
    findings=[r for r in d.get('results',[]) if r.get('extra',{}).get('severity') in ['ERROR','WARNING']];
    print(json.dumps(findings[:50], indent=2))"

# Aggregate and format
python3 /app/skills/security-audit/aggregate.py \
  --bandit /tmp/${REPO_NAME}_bandit.json \
  --safety /tmp/${REPO_NAME}_safety.json \
  --output /data/.hermes/audit_reports/${REPO_NAME}_$(date +%Y%m%d).json

# Report to Telegram
echo "Audit complete: $(jq '.total_findings' /data/.hermes/audit_reports/${REPO_NAME}_*.json) findings"

## Output Format
JSON with keys: repo, timestamp, total_findings, critical, high, medium, low, findings[]

## Storage
/data/.hermes/audit_reports/<repo>_YYYYMMDD.json
