/**
 * Rhodawk AI - Hermes88 System Event Handler
 *
 * Processes system monitoring events from internal services.
 * These events come from watchdog processes, health checks,
 * and system monitoring agents. All system events are HIGH priority
 * since they indicate infrastructure issues.
 *
 * Supported events:
 * - health_alert: Service health degradation/failure
 * - disk_warning: Disk space running low
 * - memory_warning: Memory usage critical
 * - process_crash: Supervised process crashed
 */

import { Request, Response } from 'express';
import { timingSafeEqual } from 'crypto';
import { publishEvent, ALERTS_CHANNEL } from '../redis.js';
import {
  HermesEvent,
  Priority,
  EVENT_TYPES,
  SystemHealthAlertEvent,
  SystemDiskWarningEvent,
  SystemMemoryWarningEvent,
  SystemProcessCrashEvent,
} from '../types.js';
import { generateEventId, incrementMetric } from '../utils.js';

// ─── Logger ──────────────────────────────────────────────────────────────────

function log(level: string, message: string, data?: Record<string, unknown>): void {
  const entry = {
    timestamp: new Date().toISOString(),
    level,
    component: 'handler:system',
    message,
    ...data,
  };
  if (level === 'error') {
    console.error(JSON.stringify(entry));
  } else {
    console.log(JSON.stringify(entry));
  }
}

// ─── Authentication ──────────────────────────────────────────────────────────

/**
 * Verify the system event sender using a bearer token.
 * The token is passed in the Authorization header as "Bearer <token>".
 *
 * @param req - Express request object
 * @returns true if the bearer token matches the configured secret
 */
export function verifySystemSecret(req: Request): boolean {
  const secret = process.env.SYSTEM_WEBHOOK_SECRET;
  if (!secret) {
    log('error', 'SYSTEM_WEBHOOK_SECRET not configured');
    return false;
  }

  const authHeader = req.headers.authorization;
  if (!authHeader) {
    return false;
  }

  const prefix = 'Bearer ';
  if (!authHeader.startsWith(prefix)) {
    return false;
  }

  const token = authHeader.slice(prefix.length);

  // Timing-safe comparison
  try {
    const tokenBuffer = Buffer.from(token, 'utf8');
    const secretBuffer = Buffer.from(secret, 'utf8');

    if (tokenBuffer.length !== secretBuffer.length) {
      return false;
    }

    return timingSafeEqual(tokenBuffer, secretBuffer);
  } catch {
    return false;
  }
}

// ─── Main Handler ────────────────────────────────────────────────────────────

/**
 * Express route handler for system events.
 * Verifies bearer token, parses event type, and routes to specific handlers.
 */
export async function handleSystemEvent(req: Request, res: Response): Promise<void> {
  const startTime = Date.now();

  // Verify bearer token
  if (!verifySystemSecret(req)) {
    log('error', 'Invalid or missing system webhook authentication');
    incrementMetric('events_failed');
    res.status(401).json({ error: 'Unauthorized' });
    return;
  }

  const body = req.body as Record<string, unknown>;
  const eventType = (body.event_type as string) || (body.type as string) || 'unknown';
  const hostname = (body.hostname as string) || 'unknown';

  log('info', `Received system event: ${eventType}`, {
    event_type: eventType,
    hostname,
  });

  try {
    switch (eventType) {
      case 'health_alert':
        await handleHealthAlert(body);
        break;
      case 'disk_warning':
        await handleDiskWarning(body);
        break;
      case 'memory_warning':
        await handleMemoryWarning(body);
        break;
      case 'process_crash':
        await handleProcessCrash(body);
        break;
      default:
        log('info', `Unhandled system event type: ${eventType}`, { hostname });
        res.status(200).json({ status: 'ignored', event: eventType });
        return;
    }

    incrementMetric('events_received');
    incrementMetric('system');

    const duration = Date.now() - startTime;
    log('info', `System event processed in ${duration}ms`, {
      event_type: eventType,
      hostname,
      duration_ms: duration,
    });

    res.status(200).json({ status: 'processed', event: eventType });
  } catch (err) {
    const error = err instanceof Error ? err : new Error(String(err));
    log('error', `Failed to process system event: ${error.message}`, {
      event_type: eventType,
      hostname,
      error: error.message,
      stack: error.stack,
    });
    incrementMetric('events_failed');
    res.status(500).json({ error: 'Internal processing error' });
  }
}

// ─── Per-Event Handlers ──────────────────────────────────────────────────────

/**
 * Handle health alert events. HIGH priority.
 * Indicates a monitored service is degraded or down.
 */
async function handleHealthAlert(body: Record<string, unknown>): Promise<void> {
  const hostname = (body.hostname as string) || 'unknown';
  const service = (body.service as string) || 'unknown';
  const status = (body.status as string) || 'unknown';

  const validStatuses: Array<'degraded' | 'down' | 'unreachable'> = ['degraded', 'down', 'unreachable'];
  const normalizedStatus = validStatuses.includes(status as 'degraded' | 'down' | 'unreachable')
    ? (status as 'degraded' | 'down' | 'unreachable')
    : 'down';

  const payload: SystemHealthAlertEvent = {
    source: 'system',
    hostname,
    reported_at: (body.reported_at as string) || new Date().toISOString(),
    event_type: 'health_alert',
    service,
    status: normalizedStatus,
    message: (body.message as string) || `Service ${service} is ${normalizedStatus}`,
    check_url: (body.check_url as string) || undefined,
    last_healthy: (body.last_healthy as string) || '',
    consecutive_failures: (body.consecutive_failures as number) || 1,
  };

  // Down/unreachable services get CRITICAL, degraded gets HIGH
  const priority = normalizedStatus === 'degraded' ? Priority.HIGH : Priority.CRITICAL;

  const event: HermesEvent = {
    id: generateEventId(),
    type: EVENT_TYPES.SYSTEM_HEALTH_ALERT,
    timestamp: new Date().toISOString(),
    priority,
    source: 'system',
    payload,
    metadata: {
      hostname,
      service,
      status: normalizedStatus,
    },
  };

  await publishEvent(ALERTS_CHANNEL, event);
}

/**
 * Handle disk warning events. HIGH priority.
 * Indicates disk space on a mount point is running low.
 */
async function handleDiskWarning(body: Record<string, unknown>): Promise<void> {
  const hostname = (body.hostname as string) || 'unknown';
  const usagePercent = (body.usage_percent as number) || 0;

  const payload: SystemDiskWarningEvent = {
    source: 'system',
    hostname,
    reported_at: (body.reported_at as string) || new Date().toISOString(),
    event_type: 'disk_warning',
    mount_point: (body.mount_point as string) || '/',
    usage_percent: usagePercent,
    available_bytes: (body.available_bytes as number) || 0,
    total_bytes: (body.total_bytes as number) || 0,
    threshold_percent: (body.threshold_percent as number) || 90,
  };

  // Above 95% is CRITICAL, otherwise HIGH
  const priority = usagePercent >= 95 ? Priority.CRITICAL : Priority.HIGH;

  const event: HermesEvent = {
    id: generateEventId(),
    type: EVENT_TYPES.SYSTEM_DISK_WARNING,
    timestamp: new Date().toISOString(),
    priority,
    source: 'system',
    payload,
    metadata: {
      hostname,
      mount_point: payload.mount_point,
      usage_percent: String(usagePercent),
    },
  };

  await publishEvent(ALERTS_CHANNEL, event);
}

/**
 * Handle memory warning events. HIGH priority.
 * Indicates system memory usage is critically high.
 */
async function handleMemoryWarning(body: Record<string, unknown>): Promise<void> {
  const hostname = (body.hostname as string) || 'unknown';
  const usagePercent = (body.usage_percent as number) || 0;
  const topProcesses = (body.top_processes as Array<Record<string, unknown>>) || [];

  const payload: SystemMemoryWarningEvent = {
    source: 'system',
    hostname,
    reported_at: (body.reported_at as string) || new Date().toISOString(),
    event_type: 'memory_warning',
    usage_percent: usagePercent,
    available_mb: (body.available_mb as number) || 0,
    total_mb: (body.total_mb as number) || 0,
    swap_usage_percent: (body.swap_usage_percent as number) || 0,
    top_processes: topProcesses.map((p) => ({
      pid: (p.pid as number) || 0,
      name: (p.name as string) || 'unknown',
      memory_mb: (p.memory_mb as number) || 0,
    })),
  };

  // Above 95% is CRITICAL, otherwise HIGH
  const priority = usagePercent >= 95 ? Priority.CRITICAL : Priority.HIGH;

  const event: HermesEvent = {
    id: generateEventId(),
    type: EVENT_TYPES.SYSTEM_MEMORY_WARNING,
    timestamp: new Date().toISOString(),
    priority,
    source: 'system',
    payload,
    metadata: {
      hostname,
      usage_percent: String(usagePercent),
      top_process: topProcesses.length > 0 ? (topProcesses[0].name as string) || 'unknown' : 'none',
    },
  };

  await publishEvent(ALERTS_CHANNEL, event);
}

/**
 * Handle process crash events. HIGH priority.
 * Indicates a supervised process has crashed and may need intervention.
 */
async function handleProcessCrash(body: Record<string, unknown>): Promise<void> {
  const hostname = (body.hostname as string) || 'unknown';
  const processName = (body.process_name as string) || 'unknown';
  const restartCount = (body.restart_count as number) || 0;
  const lastLogLines = (body.last_log_lines as string[]) || [];

  const payload: SystemProcessCrashEvent = {
    source: 'system',
    hostname,
    reported_at: (body.reported_at as string) || new Date().toISOString(),
    event_type: 'process_crash',
    process_name: processName,
    pid: (body.pid as number) || 0,
    exit_code: (body.exit_code as number) || 1,
    signal: (body.signal as string) || null,
    uptime_seconds: (body.uptime_seconds as number) || 0,
    restart_count: restartCount,
    last_log_lines: lastLogLines,
  };

  // Multiple restarts indicates a persistent issue - CRITICAL
  const priority = restartCount >= 3 ? Priority.CRITICAL : Priority.HIGH;

  const event: HermesEvent = {
    id: generateEventId(),
    type: EVENT_TYPES.SYSTEM_PROCESS_CRASH,
    timestamp: new Date().toISOString(),
    priority,
    source: 'system',
    payload,
    metadata: {
      hostname,
      process_name: processName,
      restart_count: String(restartCount),
      exit_code: String(payload.exit_code),
    },
  };

  await publishEvent(ALERTS_CHANNEL, event);
}
