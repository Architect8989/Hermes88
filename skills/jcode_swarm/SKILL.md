# jcode-swarm
Spawns parallel jcode workers across multiple repos or modules simultaneously.
jcode's swarm server (already running via supervisord) auto-resolves file conflicts,
notifies sibling agents of file changes, and manages completion status.

## When to use
- Batch: running Rhodawk against 10+ target repos
- Parallel healing: fixing multiple failing test modules at once
- Multi-repo PR generation sprint

## Usage
```bash
python3 /app/skills/jcode_swarm/spawn.py \
  --repos /tmp/target_repos.json \
  --task "Run Rhodawk scan, fix all failing tests, push PR" \
  --workers 5
```
