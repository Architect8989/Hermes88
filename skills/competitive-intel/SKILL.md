# Competitive Intelligence Skill — Rhodawk

## Trigger
User asks about competitor (XBOW, CrowdStrike Falcon, GitHub Advanced Security, Semgrep).

## Protocol
1. Fetch competitor's pricing page, feature page, recent press releases
2. Map capabilities against Rhodawk's current state
3. Identify gaps and advantages
4. Output: Competitor Card (model, pricing, moat, weakness, Rhodawk counter)

## Output format
COMPETITOR: [name]
TIMESTAMP: [date]
PRICING: [tiers and prices from their page]
KEY_FEATURES: [top 5 capabilities]
MOAT: [what makes them sticky]
WEAKNESS: [gaps or complaints from reviews]
RHODAWK_COUNTER: [how Rhodawk wins or differentiates]

## Storage
Save to /data/.hermes/research/competitive_[name]_YYYYMMDD.md
