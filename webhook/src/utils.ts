/**
 * Rhodawk AI - Hermes88 Webhook Server Utilities
 *
 * Shared utility functions used by handlers and the main server.
 * Extracted to avoid circular dependencies between server.ts and handlers.
 */

import { randomBytes } from 'crypto';

// ─── Event ID Generator ──────────────────────────────────────────────────────

/**
 * Generate a unique event ID: timestamp + random hex.
 * Format: <unix_ms>-<8 random hex chars>
 */
export function generateEventId(): string {
  const timestamp = Date.now();
  const random = randomBytes(4).toString('hex');
  return `${timestamp}-${random}`;
}

// ─── Metrics Store ───────────────────────────────────────────────────────────
// NOTE: Metrics are in-memory only and reset on server restart.
// For persistent metrics across restarts, consider writing to Redis
// (e.g., HINCRBY hermes:webhook:metrics events_received 1).

const metrics = {
  events_received: 0,
  events_published: 0,
  events_failed: 0,
  events_by_source: {
    github: 0,
    stripe: 0,
    system: 0,
  },
  last_event_at: null as string | null,
};

export type MetricKey = 'events_received' | 'events_published' | 'events_failed' | 'github' | 'stripe' | 'system';

/**
 * Increment a metric counter.
 */
export function incrementMetric(metric: MetricKey): void {
  switch (metric) {
    case 'events_received':
      metrics.events_received++;
      metrics.last_event_at = new Date().toISOString();
      break;
    case 'events_published':
      metrics.events_published++;
      break;
    case 'events_failed':
      metrics.events_failed++;
      break;
    case 'github':
      metrics.events_by_source.github++;
      break;
    case 'stripe':
      metrics.events_by_source.stripe++;
      break;
    case 'system':
      metrics.events_by_source.system++;
      break;
  }
}

/**
 * Get current metrics snapshot.
 */
export function getMetrics(): {
  events_received: number;
  events_published: number;
  events_failed: number;
  events_by_source: { github: number; stripe: number; system: number };
  last_event_at: string | null;
} {
  return {
    events_received: metrics.events_received,
    events_published: metrics.events_published,
    events_failed: metrics.events_failed,
    events_by_source: { ...metrics.events_by_source },
    last_event_at: metrics.last_event_at,
  };
}

// ─── Logger ──────────────────────────────────────────────────────────────────

/**
 * Structured JSON logger.
 */
export function log(level: string, message: string, data?: Record<string, unknown>): void {
  const entry = {
    timestamp: new Date().toISOString(),
    level,
    component: 'server',
    message,
    ...data,
  };
  if (level === 'error') {
    console.error(JSON.stringify(entry));
  } else {
    console.log(JSON.stringify(entry));
  }
}
