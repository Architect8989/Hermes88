# Skill: deployment-orchestration (Peak v1.0)

## Purpose
Manage deployments to HuggingFace Spaces, DigitalOcean Droplets,
and other infrastructure targets.

## When This Skill Applies
- "Deploy Hermes to production"
- "Update the HuggingFace Space"
- "Roll back to previous version"
- Proactive: after successful CI on main branch

## Targets
- HuggingFace Spaces (git push to HF remote)
- DigitalOcean Droplet (docker compose via SSH)
- GitHub Pages (static site deployments)

## Deploy to HuggingFace
python3 /app/skills/deployment-orchestration/deploy.py hf \
  --repo "Architect8999/Hermes" \
  --branch main \
  --token $HF_TOKEN

## Deploy to DO Droplet
python3 /app/skills/deployment-orchestration/deploy.py do \
  --host $DO_DROPLET_IP \
  --compose-file docker-compose.yml \
  --pull-latest

## Rollback
python3 /app/skills/deployment-orchestration/deploy.py rollback \
  --target hf \
  --commits 1

## Protocol
1. Run tests locally (must pass before deploy)
2. Create deployment record in memory
3. Execute deployment command
4. Verify health check passes on target
5. Report success/failure to operator
6. If failure: auto-rollback and alert

## Health Check
After deploy, verify:
- HTTP health endpoint returns 200
- Telegram bot responds to /ping
- Supervisord shows all processes RUNNING

## Environment Variables
- HF_TOKEN: HuggingFace access token
- DO_DROPLET_IP: DigitalOcean droplet IP
- GITHUB_PAT: GitHub personal access token
- DEPLOY_SSH_KEY: Path to SSH key for DO deploys

## Error Handling
- If tests fail pre-deploy: abort and report
- If health check fails post-deploy: auto-rollback
- If rollback fails: alert operator immediately
