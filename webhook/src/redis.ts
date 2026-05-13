/**
 * Rhodawk AI - Hermes88 Redis Client Wrapper
 *
 * Manages the Redis connection for the webhook server.
 * Provides event publishing helpers and channel constants.
 * Used by all webhook handlers to publish structured events.
 */

import Redis from 'ioredis';
import { HermesEvent } from './types.js';

// ─── Channel Constants ───────────────────────────────────────────────────────

/** Main events channel - all webhook events published here */
export const EVENTS_CHANNEL = 'hermes:events';

/** High-priority alerts channel - system alerts and critical events */
export const ALERTS_CHANNEL = 'hermes:alerts';

/** Task queue channel - events that require action */
export const TASKS_CHANNEL = 'hermes:tasks';

// ─── Logger ──────────────────────────────────────────────────────────────────

function log(level: string, message: string, data?: Record<string, unknown>): void {
  const entry = {
    timestamp: new Date().toISOString(),
    level,
    component: 'redis',
    message,
    ...data,
  };
  if (level === 'error') {
    console.error(JSON.stringify(entry));
  } else {
    console.log(JSON.stringify(entry));
  }
}

// ─── Redis Client Singleton ──────────────────────────────────────────────────

let redisClient: Redis | null = null;
let isConnected = false;

/**
 * Get or create the Redis client instance.
 * Uses environment variables for connection configuration:
 * - REDIS_URL: Full Redis URL (redis://host:port)
 * - REDIS_HOST: Redis hostname (default: localhost)
 * - REDIS_PORT: Redis port (default: 6379)
 * - REDIS_PASSWORD: Optional password
 * - REDIS_DB: Database number (default: 0)
 */
export function getRedisClient(): Redis {
  if (redisClient) {
    return redisClient;
  }

  const redisUrl = process.env.REDIS_URL;

  if (redisUrl) {
    redisClient = new Redis(redisUrl, {
      maxRetriesPerRequest: 3,
      retryStrategy(times: number): number | null {
        if (times > 10) {
          log('error', 'Redis connection failed after 10 retries, giving up');
          return null;
        }
        const delay = Math.min(times * 200, 5000);
        log('warn', `Redis retry attempt ${times}, waiting ${delay}ms`);
        return delay;
      },
      lazyConnect: false,
      enableReadyCheck: true,
      connectTimeout: 10000,
    });
  } else {
    const host = process.env.REDIS_HOST || 'localhost';
    const port = parseInt(process.env.REDIS_PORT || '6379', 10);
    const password = process.env.REDIS_PASSWORD || undefined;
    const db = parseInt(process.env.REDIS_DB || '0', 10);

    redisClient = new Redis({
      host,
      port,
      password,
      db,
      maxRetriesPerRequest: 3,
      retryStrategy(times: number): number | null {
        if (times > 10) {
          log('error', 'Redis connection failed after 10 retries, giving up');
          return null;
        }
        const delay = Math.min(times * 200, 5000);
        log('warn', `Redis retry attempt ${times}, waiting ${delay}ms`);
        return delay;
      },
      lazyConnect: false,
      enableReadyCheck: true,
      connectTimeout: 10000,
    });
  }

  // Connection event handlers
  redisClient.on('connect', () => {
    isConnected = true;
    log('info', 'Redis connection established');
  });

  redisClient.on('ready', () => {
    isConnected = true;
    log('info', 'Redis client ready');
  });

  redisClient.on('error', (err: Error) => {
    isConnected = false;
    log('error', 'Redis connection error', { error: err.message });
  });

  redisClient.on('close', () => {
    isConnected = false;
    log('warn', 'Redis connection closed');
  });

  redisClient.on('reconnecting', () => {
    log('info', 'Redis reconnecting...');
  });

  return redisClient;
}

// ─── Connection Health ───────────────────────────────────────────────────────

/**
 * Check if Redis is currently connected and responsive.
 * Performs a PING command to verify the connection is alive.
 */
export async function isRedisHealthy(): Promise<boolean> {
  if (!redisClient || !isConnected) {
    return false;
  }

  try {
    const result = await redisClient.ping();
    return result === 'PONG';
  } catch {
    return false;
  }
}

/**
 * Get the current connection status without performing a health check.
 */
export function isRedisConnected(): boolean {
  return isConnected;
}

// ─── Event Publishing ────────────────────────────────────────────────────────

/**
 * Publish a structured HermesEvent to a Redis channel.
 * The event is serialized to JSON before publishing.
 *
 * @param channel - Redis pub/sub channel name
 * @param event - Structured HermesEvent object
 * @returns Number of subscribers that received the message
 */
export async function publishEvent(channel: string, event: HermesEvent): Promise<number> {
  const client = getRedisClient();

  try {
    const serialized = JSON.stringify(event);
    const subscribers = await client.publish(channel, serialized);

    log('info', `Event published to ${channel}`, {
      event_id: event.id,
      event_type: event.type,
      priority: event.priority,
      subscribers,
    });

    // Also store in a Redis list for persistence (last 1000 events)
    const listKey = `${channel}:history`;
    await client.lpush(listKey, serialized);
    await client.ltrim(listKey, 0, 999);

    return subscribers;
  } catch (err) {
    const error = err instanceof Error ? err : new Error(String(err));
    log('error', `Failed to publish event to ${channel}`, {
      event_id: event.id,
      event_type: event.type,
      error: error.message,
    });
    throw error;
  }
}

/**
 * Publish an event to multiple channels simultaneously.
 * Useful for events that need to appear on both EVENTS and ALERTS channels.
 */
export async function publishToMultipleChannels(
  channels: string[],
  event: HermesEvent
): Promise<void> {
  const results = await Promise.allSettled(
    channels.map((channel) => publishEvent(channel, event))
  );

  const failures = results.filter((r) => r.status === 'rejected');
  if (failures.length > 0) {
    log('error', `Failed to publish to ${failures.length}/${channels.length} channels`, {
      event_id: event.id,
    });
  }
}

// ─── Graceful Disconnect ─────────────────────────────────────────────────────

/**
 * Gracefully disconnect the Redis client.
 * Waits for pending commands to complete before closing.
 */
export async function disconnectRedis(): Promise<void> {
  if (!redisClient) {
    return;
  }

  try {
    log('info', 'Disconnecting Redis client...');
    await redisClient.quit();
    isConnected = false;
    redisClient = null;
    log('info', 'Redis client disconnected gracefully');
  } catch (err) {
    const error = err instanceof Error ? err : new Error(String(err));
    log('error', 'Error during Redis disconnect', { error: error.message });
    // Force disconnect if graceful quit fails
    if (redisClient) {
      redisClient.disconnect();
      redisClient = null;
      isConnected = false;
    }
  }
}
