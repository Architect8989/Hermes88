/**
 * Rhodawk AI - Hermes88 Webhook Server Type Definitions
 *
 * Core interfaces and types for the webhook event processing system.
 * All events flow through the webhook server and are published to Redis
 * for consumption by the Python gateway and other services.
 */

// ─── Priority Enum ───────────────────────────────────────────────────────────

export enum Priority {
  CRITICAL = 'CRITICAL',
  HIGH = 'HIGH',
  NORMAL = 'NORMAL',
  LOW = 'LOW',
  BACKGROUND = 'BACKGROUND',
}

// ─── Event Type Constants ────────────────────────────────────────────────────

export const EVENT_TYPES = {
  // GitHub events
  GITHUB_PUSH: 'github.push',
  GITHUB_PULL_REQUEST: 'github.pull_request',
  GITHUB_WORKFLOW_RUN: 'github.workflow_run',
  GITHUB_ISSUES: 'github.issues',
  GITHUB_SECURITY_ADVISORY: 'github.security_advisory',
  GITHUB_DEPENDABOT_ALERT: 'github.dependabot_alert',

  // Stripe events
  STRIPE_PAYMENT_FAILED: 'stripe.invoice.payment_failed',
  STRIPE_PAYMENT_SUCCEEDED: 'stripe.invoice.payment_succeeded',
  STRIPE_SUBSCRIPTION_UPDATED: 'stripe.customer.subscription.updated',
  STRIPE_DISPUTE_CREATED: 'stripe.charge.dispute.created',

  // System events
  SYSTEM_HEALTH_ALERT: 'system.health_alert',
  SYSTEM_DISK_WARNING: 'system.disk_warning',
  SYSTEM_MEMORY_WARNING: 'system.memory_warning',
  SYSTEM_PROCESS_CRASH: 'system.process_crash',
} as const;

export type EventType = (typeof EVENT_TYPES)[keyof typeof EVENT_TYPES];

// ─── Core Event Interface ────────────────────────────────────────────────────

export interface HermesEvent {
  /** Unique event ID: timestamp-random hex */
  id: string;
  /** Event type from EVENT_TYPES */
  type: EventType;
  /** ISO 8601 timestamp of when the event was received */
  timestamp: string;
  /** Event priority level */
  priority: Priority;
  /** Source system that generated the event */
  source: 'github' | 'stripe' | 'system';
  /** Structured event payload */
  payload: WebhookPayload;
  /** Optional metadata */
  metadata?: Record<string, string>;
}

// ─── Webhook Payload Union Type ──────────────────────────────────────────────

export type WebhookPayload = GitHubEvent | StripeEvent | SystemEvent;

// ─── GitHub Event Types ──────────────────────────────────────────────────────

export interface GitHubEventBase {
  source: 'github';
  repository: string;
  sender: string;
  delivery_id?: string;
}

export interface GitHubPushEvent extends GitHubEventBase {
  event_type: 'push';
  ref: string;
  before: string;
  after: string;
  commits: Array<{
    id: string;
    message: string;
    author: string;
    timestamp: string;
    added: string[];
    modified: string[];
    removed: string[];
  }>;
  pusher: string;
  forced: boolean;
}

export interface GitHubWorkflowRunEvent extends GitHubEventBase {
  event_type: 'workflow_run';
  workflow_name: string;
  workflow_id: number;
  run_id: number;
  run_number: number;
  status: string;
  conclusion: string | null;
  branch: string;
  head_sha: string;
  run_url: string;
}

export interface GitHubPullRequestEvent extends GitHubEventBase {
  event_type: 'pull_request';
  action: string;
  number: number;
  title: string;
  body: string;
  state: string;
  head_branch: string;
  base_branch: string;
  merged: boolean;
  draft: boolean;
  url: string;
}

export interface GitHubIssuesEvent extends GitHubEventBase {
  event_type: 'issues';
  action: string;
  number: number;
  title: string;
  body: string;
  state: string;
  labels: string[];
  assignees: string[];
  url: string;
}

export interface GitHubSecurityAdvisoryEvent extends GitHubEventBase {
  event_type: 'security_advisory';
  action: string;
  severity: string;
  summary: string;
  description: string;
  cve_id: string | null;
  affected_packages: Array<{
    ecosystem: string;
    name: string;
  }>;
}

export interface GitHubDependabotAlertEvent extends GitHubEventBase {
  event_type: 'dependabot_alert';
  action: string;
  alert_number: number;
  severity: string;
  package_name: string;
  vulnerable_version_range: string;
  patched_version: string | null;
  summary: string;
}

export type GitHubEvent =
  | GitHubPushEvent
  | GitHubWorkflowRunEvent
  | GitHubPullRequestEvent
  | GitHubIssuesEvent
  | GitHubSecurityAdvisoryEvent
  | GitHubDependabotAlertEvent;

// ─── Stripe Event Types ──────────────────────────────────────────────────────

export interface StripeEventBase {
  source: 'stripe';
  stripe_event_id: string;
  api_version: string;
  livemode: boolean;
}

export interface StripePaymentFailedEvent extends StripeEventBase {
  event_type: 'invoice.payment_failed';
  customer_id: string;
  customer_email: string;
  invoice_id: string;
  amount_due: number;
  currency: string;
  attempt_count: number;
  next_retry_at: string | null;
  failure_message: string;
}

export interface StripePaymentSucceededEvent extends StripeEventBase {
  event_type: 'invoice.payment_succeeded';
  customer_id: string;
  customer_email: string;
  invoice_id: string;
  amount_paid: number;
  currency: string;
  subscription_id: string | null;
}

export interface StripeSubscriptionUpdatedEvent extends StripeEventBase {
  event_type: 'customer.subscription.updated';
  customer_id: string;
  subscription_id: string;
  status: string;
  plan_id: string;
  current_period_start: string;
  current_period_end: string;
  cancel_at_period_end: boolean;
  previous_attributes: Record<string, unknown>;
}

export interface StripeDisputeCreatedEvent extends StripeEventBase {
  event_type: 'charge.dispute.created';
  dispute_id: string;
  charge_id: string;
  amount: number;
  currency: string;
  reason: string;
  status: string;
  customer_id: string;
  evidence_due_by: string;
}

export type StripeEvent =
  | StripePaymentFailedEvent
  | StripePaymentSucceededEvent
  | StripeSubscriptionUpdatedEvent
  | StripeDisputeCreatedEvent;

// ─── System Event Types ──────────────────────────────────────────────────────

export interface SystemEventBase {
  source: 'system';
  hostname: string;
  reported_at: string;
}

export interface SystemHealthAlertEvent extends SystemEventBase {
  event_type: 'health_alert';
  service: string;
  status: 'degraded' | 'down' | 'unreachable';
  message: string;
  check_url?: string;
  last_healthy: string;
  consecutive_failures: number;
}

export interface SystemDiskWarningEvent extends SystemEventBase {
  event_type: 'disk_warning';
  mount_point: string;
  usage_percent: number;
  available_bytes: number;
  total_bytes: number;
  threshold_percent: number;
}

export interface SystemMemoryWarningEvent extends SystemEventBase {
  event_type: 'memory_warning';
  usage_percent: number;
  available_mb: number;
  total_mb: number;
  swap_usage_percent: number;
  top_processes: Array<{
    pid: number;
    name: string;
    memory_mb: number;
  }>;
}

export interface SystemProcessCrashEvent extends SystemEventBase {
  event_type: 'process_crash';
  process_name: string;
  pid: number;
  exit_code: number;
  signal: string | null;
  uptime_seconds: number;
  restart_count: number;
  last_log_lines: string[];
}

export type SystemEvent =
  | SystemHealthAlertEvent
  | SystemDiskWarningEvent
  | SystemMemoryWarningEvent
  | SystemProcessCrashEvent;

// ─── Server Types ────────────────────────────────────────────────────────────

export interface ServerMetrics {
  started_at: string;
  events_received: number;
  events_published: number;
  events_failed: number;
  events_by_source: {
    github: number;
    stripe: number;
    system: number;
  };
  last_event_at: string | null;
  redis_connected: boolean;
  uptime_seconds: number;
}

export interface HealthStatus {
  status: 'healthy' | 'degraded' | 'unhealthy';
  version: string;
  uptime_seconds: number;
  redis_connected: boolean;
  timestamp: string;
}
