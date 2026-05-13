/**
 * Rhodawk AI - Hermes88 GitHub Webhook Handler
 *
 * Processes incoming GitHub webhook events, verifies signatures using
 * HMAC-SHA256, routes events to appropriate handlers, and publishes
 * structured HermesEvent objects to Redis.
 *
 * Supported events:
 * - push: Code pushes to repositories
 * - workflow_run: CI/CD pipeline completions
 * - pull_request: PR opened/closed/merged
 * - issues: Issue opened/closed/labeled
 * - security_advisory: GitHub security advisories
 * - dependabot_alert: Dependabot vulnerability alerts
 */

import { createHmac, timingSafeEqual } from 'crypto';
import { Request, Response } from 'express';
import { publishEvent, publishToMultipleChannels, EVENTS_CHANNEL, ALERTS_CHANNEL } from '../redis.js';
import {
  HermesEvent,
  Priority,
  EVENT_TYPES,
  GitHubPushEvent,
  GitHubWorkflowRunEvent,
  GitHubPullRequestEvent,
  GitHubIssuesEvent,
  GitHubSecurityAdvisoryEvent,
  GitHubDependabotAlertEvent,
} from '../types.js';
import { generateEventId, incrementMetric } from '../utils.js';

// ─── Logger ──────────────────────────────────────────────────────────────────

function log(level: string, message: string, data?: Record<string, unknown>): void {
  const entry = {
    timestamp: new Date().toISOString(),
    level,
    component: 'handler:github',
    message,
    ...data,
  };
  if (level === 'error') {
    console.error(JSON.stringify(entry));
  } else {
    console.log(JSON.stringify(entry));
  }
}

// ─── Signature Verification ──────────────────────────────────────────────────

/**
 * Verify the GitHub webhook signature using HMAC-SHA256.
 * GitHub sends the signature in the X-Hub-Signature-256 header as "sha256=<hash>".
 *
 * Uses timing-safe comparison to prevent timing attacks.
 *
 * @param payload - Raw request body as Buffer
 * @param signature - The X-Hub-Signature-256 header value
 * @param secret - The webhook secret configured in GitHub
 * @returns true if the signature is valid
 */
export function verifyGitHubSignature(
  payload: Buffer,
  signature: string,
  secret: string
): boolean {
  if (!signature || !secret) {
    return false;
  }

  const prefix = 'sha256=';
  if (!signature.startsWith(prefix)) {
    return false;
  }

  const expectedSignature = signature.slice(prefix.length);
  const computedHmac = createHmac('sha256', secret)
    .update(payload)
    .digest('hex');

  // Use timing-safe comparison to prevent timing attacks
  try {
    const sigBuffer = Buffer.from(expectedSignature, 'hex');
    const computedBuffer = Buffer.from(computedHmac, 'hex');

    if (sigBuffer.length !== computedBuffer.length) {
      return false;
    }

    return timingSafeEqual(sigBuffer, computedBuffer);
  } catch {
    return false;
  }
}

// ─── Main Handler ────────────────────────────────────────────────────────────

/**
 * Express route handler for GitHub webhooks.
 * Verifies signature, parses event type, and routes to specific handlers.
 */
export async function handleGitHubWebhook(req: Request, res: Response): Promise<void> {
  const startTime = Date.now();
  const signature = req.headers['x-hub-signature-256'] as string | undefined;
  const eventType = req.headers['x-github-event'] as string | undefined;
  const deliveryId = req.headers['x-github-delivery'] as string | undefined;

  // Verify signature
  const secret = process.env.GITHUB_WEBHOOK_SECRET;
  if (!secret) {
    log('error', 'GITHUB_WEBHOOK_SECRET not configured');
    res.status(500).json({ error: 'Webhook secret not configured' });
    return;
  }

  const rawBody = (req as Request & { rawBody?: Buffer }).rawBody;
  if (!rawBody) {
    log('error', 'No raw body available for signature verification');
    res.status(400).json({ error: 'No request body' });
    return;
  }

  if (!signature || !verifyGitHubSignature(rawBody, signature, secret)) {
    log('error', 'Invalid GitHub webhook signature', { delivery_id: deliveryId });
    incrementMetric('events_failed');
    res.status(401).json({ error: 'Invalid signature' });
    return;
  }

  if (!eventType) {
    log('error', 'Missing X-GitHub-Event header');
    res.status(400).json({ error: 'Missing event type header' });
    return;
  }

  log('info', `Received GitHub event: ${eventType}`, {
    delivery_id: deliveryId,
    event_type: eventType,
  });

  try {
    const body = req.body as Record<string, unknown>;

    switch (eventType) {
      case 'push':
        await handlePush(body, deliveryId);
        break;
      case 'workflow_run':
        await handleWorkflowRun(body, deliveryId);
        break;
      case 'pull_request':
        await handlePullRequest(body, deliveryId);
        break;
      case 'issues':
        await handleIssues(body, deliveryId);
        break;
      case 'security_advisory':
        await handleSecurityAdvisory(body, deliveryId);
        break;
      case 'dependabot_alert':
        await handleDependabotAlert(body, deliveryId);
        break;
      default:
        log('info', `Unhandled GitHub event type: ${eventType}`, { delivery_id: deliveryId });
        res.status(200).json({ status: 'ignored', event: eventType });
        return;
    }

    incrementMetric('events_received');
    incrementMetric('github');

    const duration = Date.now() - startTime;
    log('info', `GitHub event processed in ${duration}ms`, {
      event_type: eventType,
      delivery_id: deliveryId,
      duration_ms: duration,
    });

    res.status(200).json({ status: 'processed', event: eventType, delivery_id: deliveryId });
  } catch (err) {
    const error = err instanceof Error ? err : new Error(String(err));
    log('error', `Failed to process GitHub event: ${error.message}`, {
      event_type: eventType,
      delivery_id: deliveryId,
      error: error.message,
      stack: error.stack,
    });
    incrementMetric('events_failed');
    res.status(500).json({ error: 'Internal processing error' });
  }
}

// ─── Per-Event Handlers ──────────────────────────────────────────────────────

/**
 * Handle push events. Normal priority unless it's a force push to main/master.
 */
async function handlePush(body: Record<string, unknown>, deliveryId?: string): Promise<void> {
  const repository = body.repository as Record<string, unknown> | undefined;
  const repoFullName = (repository?.full_name as string) || 'unknown/unknown';
  const ref = (body.ref as string) || '';
  const forced = (body.forced as boolean) || false;
  const commits = (body.commits as Array<Record<string, unknown>>) || [];
  const pusher = body.pusher as Record<string, unknown> | undefined;
  const sender = body.sender as Record<string, unknown> | undefined;

  // Force push to main/master branches is HIGH priority
  const isMainBranch = ref === 'refs/heads/main' || ref === 'refs/heads/master';
  const priority = forced && isMainBranch ? Priority.HIGH : Priority.NORMAL;

  const payload: GitHubPushEvent = {
    source: 'github',
    repository: repoFullName,
    sender: (sender?.login as string) || (pusher?.name as string) || 'unknown',
    delivery_id: deliveryId,
    event_type: 'push',
    ref,
    before: (body.before as string) || '',
    after: (body.after as string) || '',
    commits: commits.map((c) => ({
      id: (c.id as string) || '',
      message: (c.message as string) || '',
      author: ((c.author as Record<string, unknown>)?.name as string) || 'unknown',
      timestamp: (c.timestamp as string) || new Date().toISOString(),
      added: (c.added as string[]) || [],
      modified: (c.modified as string[]) || [],
      removed: (c.removed as string[]) || [],
    })),
    pusher: (pusher?.name as string) || 'unknown',
    forced,
  };

  const event: HermesEvent = {
    id: generateEventId(),
    type: EVENT_TYPES.GITHUB_PUSH,
    timestamp: new Date().toISOString(),
    priority,
    source: 'github',
    payload,
    metadata: { delivery_id: deliveryId || '' },
  };

  if (priority === Priority.HIGH) {
    await publishToMultipleChannels([EVENTS_CHANNEL, ALERTS_CHANNEL], event);
  } else {
    await publishEvent(EVENTS_CHANNEL, event);
  }
}

/**
 * Handle workflow_run events. CI failures get HIGH priority.
 */
async function handleWorkflowRun(body: Record<string, unknown>, deliveryId?: string): Promise<void> {
  const workflowRun = body.workflow_run as Record<string, unknown> | undefined;
  const repository = body.repository as Record<string, unknown> | undefined;
  const sender = body.sender as Record<string, unknown> | undefined;

  if (!workflowRun) {
    log('warn', 'workflow_run event missing workflow_run field');
    return;
  }

  const conclusion = (workflowRun.conclusion as string) || null;
  const status = (workflowRun.status as string) || 'unknown';

  // CI failures are HIGH priority
  const isFailed = conclusion === 'failure' || conclusion === 'timed_out';
  const priority = isFailed ? Priority.HIGH : Priority.NORMAL;

  const headBranch = (workflowRun.head_branch as string) || '';

  const payload: GitHubWorkflowRunEvent = {
    source: 'github',
    repository: (repository?.full_name as string) || 'unknown/unknown',
    sender: (sender?.login as string) || 'unknown',
    delivery_id: deliveryId,
    event_type: 'workflow_run',
    workflow_name: (workflowRun.name as string) || 'unknown',
    workflow_id: (workflowRun.workflow_id as number) || 0,
    run_id: (workflowRun.id as number) || 0,
    run_number: (workflowRun.run_number as number) || 0,
    status,
    conclusion,
    branch: headBranch,
    head_sha: (workflowRun.head_sha as string) || '',
    run_url: (workflowRun.html_url as string) || '',
  };

  const event: HermesEvent = {
    id: generateEventId(),
    type: EVENT_TYPES.GITHUB_WORKFLOW_RUN,
    timestamp: new Date().toISOString(),
    priority,
    source: 'github',
    payload,
    metadata: { delivery_id: deliveryId || '', conclusion: conclusion || 'pending' },
  };

  if (isFailed) {
    await publishToMultipleChannels([EVENTS_CHANNEL, ALERTS_CHANNEL], event);
  } else {
    await publishEvent(EVENTS_CHANNEL, event);
  }
}

/**
 * Handle pull_request events.
 */
async function handlePullRequest(body: Record<string, unknown>, deliveryId?: string): Promise<void> {
  const action = (body.action as string) || 'unknown';
  const pr = body.pull_request as Record<string, unknown> | undefined;
  const repository = body.repository as Record<string, unknown> | undefined;
  const sender = body.sender as Record<string, unknown> | undefined;

  if (!pr) {
    log('warn', 'pull_request event missing pull_request field');
    return;
  }

  const head = pr.head as Record<string, unknown> | undefined;
  const base = pr.base as Record<string, unknown> | undefined;

  const payload: GitHubPullRequestEvent = {
    source: 'github',
    repository: (repository?.full_name as string) || 'unknown/unknown',
    sender: (sender?.login as string) || 'unknown',
    delivery_id: deliveryId,
    event_type: 'pull_request',
    action,
    number: (pr.number as number) || 0,
    title: (pr.title as string) || '',
    body: (pr.body as string) || '',
    state: (pr.state as string) || 'unknown',
    head_branch: (head?.ref as string) || '',
    base_branch: (base?.ref as string) || '',
    merged: (pr.merged as boolean) || false,
    draft: (pr.draft as boolean) || false,
    url: (pr.html_url as string) || '',
  };

  const event: HermesEvent = {
    id: generateEventId(),
    type: EVENT_TYPES.GITHUB_PULL_REQUEST,
    timestamp: new Date().toISOString(),
    priority: Priority.NORMAL,
    source: 'github',
    payload,
    metadata: { delivery_id: deliveryId || '', action },
  };

  await publishEvent(EVENTS_CHANNEL, event);
}

/**
 * Handle issues events.
 */
async function handleIssues(body: Record<string, unknown>, deliveryId?: string): Promise<void> {
  const action = (body.action as string) || 'unknown';
  const issue = body.issue as Record<string, unknown> | undefined;
  const repository = body.repository as Record<string, unknown> | undefined;
  const sender = body.sender as Record<string, unknown> | undefined;

  if (!issue) {
    log('warn', 'issues event missing issue field');
    return;
  }

  const labels = (issue.labels as Array<Record<string, unknown>>) || [];
  const assignees = (issue.assignees as Array<Record<string, unknown>>) || [];

  const payload: GitHubIssuesEvent = {
    source: 'github',
    repository: (repository?.full_name as string) || 'unknown/unknown',
    sender: (sender?.login as string) || 'unknown',
    delivery_id: deliveryId,
    event_type: 'issues',
    action,
    number: (issue.number as number) || 0,
    title: (issue.title as string) || '',
    body: (issue.body as string) || '',
    state: (issue.state as string) || 'unknown',
    labels: labels.map((l) => (l.name as string) || ''),
    assignees: assignees.map((a) => (a.login as string) || ''),
    url: (issue.html_url as string) || '',
  };

  // Bug issues get HIGH priority
  const hasBugLabel = payload.labels.some(
    (l) => l.toLowerCase() === 'bug' || l.toLowerCase() === 'critical'
  );
  const priority = hasBugLabel ? Priority.HIGH : Priority.LOW;

  const event: HermesEvent = {
    id: generateEventId(),
    type: EVENT_TYPES.GITHUB_ISSUES,
    timestamp: new Date().toISOString(),
    priority,
    source: 'github',
    payload,
    metadata: { delivery_id: deliveryId || '', action },
  };

  await publishEvent(EVENTS_CHANNEL, event);
}

/**
 * Handle security_advisory events. Always CRITICAL priority.
 */
async function handleSecurityAdvisory(body: Record<string, unknown>, deliveryId?: string): Promise<void> {
  const action = (body.action as string) || 'unknown';
  const advisory = body.security_advisory as Record<string, unknown> | undefined;
  const repository = body.repository as Record<string, unknown> | undefined;
  const sender = body.sender as Record<string, unknown> | undefined;

  if (!advisory) {
    log('warn', 'security_advisory event missing security_advisory field');
    return;
  }

  const identifiers = (advisory.identifiers as Array<Record<string, unknown>>) || [];
  const cveIdentifier = identifiers.find((i) => i.type === 'CVE');
  const vulnerabilities = (advisory.vulnerabilities as Array<Record<string, unknown>>) || [];

  const payload: GitHubSecurityAdvisoryEvent = {
    source: 'github',
    repository: (repository?.full_name as string) || 'unknown/unknown',
    sender: (sender?.login as string) || 'github',
    delivery_id: deliveryId,
    event_type: 'security_advisory',
    action,
    severity: (advisory.severity as string) || 'unknown',
    summary: (advisory.summary as string) || '',
    description: (advisory.description as string) || '',
    cve_id: (cveIdentifier?.value as string) || null,
    affected_packages: vulnerabilities.map((v) => {
      const pkg = v.package as Record<string, unknown> | undefined;
      return {
        ecosystem: (pkg?.ecosystem as string) || 'unknown',
        name: (pkg?.name as string) || 'unknown',
      };
    }),
  };

  const event: HermesEvent = {
    id: generateEventId(),
    type: EVENT_TYPES.GITHUB_SECURITY_ADVISORY,
    timestamp: new Date().toISOString(),
    priority: Priority.CRITICAL,
    source: 'github',
    payload,
    metadata: { delivery_id: deliveryId || '', severity: payload.severity },
  };

  await publishToMultipleChannels([EVENTS_CHANNEL, ALERTS_CHANNEL], event);
}

/**
 * Handle dependabot_alert events. HIGH priority for critical/high severity.
 */
async function handleDependabotAlert(body: Record<string, unknown>, deliveryId?: string): Promise<void> {
  const action = (body.action as string) || 'unknown';
  const alert = body.alert as Record<string, unknown> | undefined;
  const repository = body.repository as Record<string, unknown> | undefined;
  const sender = body.sender as Record<string, unknown> | undefined;

  if (!alert) {
    log('warn', 'dependabot_alert event missing alert field');
    return;
  }

  const securityAdvisory = alert.security_advisory as Record<string, unknown> | undefined;
  const dependency = alert.dependency as Record<string, unknown> | undefined;
  const depPackage = dependency?.package as Record<string, unknown> | undefined;
  const securityVulnerability = alert.security_vulnerability as Record<string, unknown> | undefined;
  const firstPatchedVersion = securityVulnerability?.first_patched_version as Record<string, unknown> | undefined;

  const severity = (securityAdvisory?.severity as string) || (alert.severity as string) || 'unknown';

  const payload: GitHubDependabotAlertEvent = {
    source: 'github',
    repository: (repository?.full_name as string) || 'unknown/unknown',
    sender: (sender?.login as string) || 'dependabot',
    delivery_id: deliveryId,
    event_type: 'dependabot_alert',
    action,
    alert_number: (alert.number as number) || 0,
    severity,
    package_name: (depPackage?.name as string) || 'unknown',
    vulnerable_version_range: (securityVulnerability?.vulnerable_version_range as string) || '',
    patched_version: (firstPatchedVersion?.identifier as string) || null,
    summary: (securityAdvisory?.summary as string) || '',
  };

  const isCritical = severity === 'critical' || severity === 'high';
  const priority = isCritical ? Priority.HIGH : Priority.NORMAL;

  const event: HermesEvent = {
    id: generateEventId(),
    type: EVENT_TYPES.GITHUB_DEPENDABOT_ALERT,
    timestamp: new Date().toISOString(),
    priority,
    source: 'github',
    payload,
    metadata: { delivery_id: deliveryId || '', severity },
  };

  if (isCritical) {
    await publishToMultipleChannels([EVENTS_CHANNEL, ALERTS_CHANNEL], event);
  } else {
    await publishEvent(EVENTS_CHANNEL, event);
  }
}
