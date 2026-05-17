# Stealth Browser Skill

## When to use instead of plain curl
- Target returns "Checking your browser" or HTTP 403
- Site is a JS SPA (React/Vue) — curl returns empty body
- Site requires cookies (LinkedIn, Crunchbase, paywalls)
- Any Cloudflare-protected competitive intel target
- YouTube transcript extraction

## Preferred method — use the camofox_browse tool (handles lifecycle automatically)
camofox_browse(url="https://target.com", wait_seconds=3)

## Manual via terminal tool (when you need fine-grained control)
## Camofox runs at http://camofox:9377 in Docker Compose (NOT localhost)
CAMOFOX_BASE="http://${CAMOFOX_HOST:-camofox}:${CAMOFOX_PORT:-9377}"

## Always check health first
curl -sf "$CAMOFOX_BASE/health" || echo "CAMOFOX DOWN — use plain curl"

## Core fetch pattern
SESSION_ID="hermes-$$"
TARGET_URL="$1"

TAB_ID=$(curl -s -X POST "$CAMOFOX_BASE/sessions/$SESSION_ID/tabs" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CAMOFOX_ACCESS_KEY" \
  -d "{\"url\": \"$TARGET_URL\"}" | python3 -c "
import json,sys; d=json.load(sys.stdin); print(d.get('tabId', d.get('id','')))")

sleep 3  # page load

curl -s "$CAMOFOX_BASE/tabs/$TAB_ID/snapshot" \
  -H "Authorization: Bearer $CAMOFOX_ACCESS_KEY" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('text', d.get('content',''))[:8000])"

curl -s -X DELETE "$CAMOFOX_BASE/tabs/$TAB_ID" \
  -H "Authorization: Bearer $CAMOFOX_ACCESS_KEY" > /dev/null

## YouTube transcript
curl -s "$CAMOFOX_BASE/youtube/transcript" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CAMOFOX_ACCESS_KEY" \
  -d '{"url": "https://youtube.com/watch?v=VIDEO_ID"}' | python3 -c "
import json,sys; print(json.load(sys.stdin).get('transcript','')[:6000])"

## Cookie auth (LinkedIn, paywalls)
curl -s -X POST "$CAMOFOX_BASE/sessions/$SESSION_ID/cookies" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CAMOFOX_ACCESS_KEY" \
  --data-binary @/data/.hermes/cookies/linkedin.txt

## Session limits
Max 50 concurrent sessions. Max 10 tabs per session.
Cold start after idle: ~10-15 seconds.

## Failure fallback
If camofox is DOWN, fall back to plain curl with spoofed User-Agent:
curl -sL --max-time 15 "URL" \
  -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0"
