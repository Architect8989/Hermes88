# openclaw-channel
Multi-channel gateway running on port 18789 via openclaw (openclaw/openclaw).
Use to expand Hermes beyond Telegram to WhatsApp, Discord, Slack, Signal,
iMessage, and 20+ other channels — without touching hermes-gateway.

## Architecture
```
Telegram ──► hermes-gateway (Python, port N/A — Telegram polling)
WhatsApp │
Discord  ├──► openclaw-gateway (Node.js, port 18789) ──► DO Inference
Slack    │
Signal   ┘
```

## Gateway management
```bash
# Status
openclaw status
openclaw health

# Live config (hot-reload, no restart needed)
openclaw config get agent.model.primary
openclaw config set agent.model.primary "do-inference/deepseek-v4-pro"

# Add a channel (example: Discord bot token)
openclaw config set channels.discord.token "BOT_TOKEN_HERE"
openclaw config set channels.discord.dmPolicy "pairing"

# Add WhatsApp (scan QR on first connect)
openclaw config set channels.whatsapp.allowFrom '["+15555550123"]'

# Pair an unknown sender
openclaw pairing list
openclaw pairing approve telegram <code>
```

## Send a message via openclaw (from hermes-agent skill)
```bash
openclaw agent --message "Draft a reply for the pending Discord messages" \
  --model do-inference/deepseek-r1-distill-llama-70b
```

## Provider routing
All models route through DO Inference via the custom provider:
  do-inference/deepseek-r1-distill-llama-70b   ← orchestration
  do-inference/deepseek-v4-pro                  ← coding tasks

## Config location
  ~/.openclaw/openclaw.json   (hot-reload — edit live)
  /data/.openclaw/workspace   (agent workspace / scratch)

## Add a new channel
1. Find the channel docs: https://docs.openclaw.ai/channels
2. Set the token: `openclaw config set channels.<name>.token "..."`
3. Set allowFrom: `openclaw config set channels.<name>.allowFrom '["+1..."]'`
4. Restart if needed: supervisorctl restart openclaw-gateway
