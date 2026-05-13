/**
 * Rhodawk AI - Hermes88 Webhook Server
 *
 * Express-based webhook receiver for GitHub, Stripe, and system events.
 * Publishes structured events to Redis for the Python gateway to consume.
 *
 * Features:
 * - HMAC-SHA256 signature verification for GitHub/Stripe webhooks
 * - Bearer token authentication for system events
 * - Raw body preservation for signature verification
 * - Graceful shutdown on SIGTERM/SIGINT
 * - Health check and metrics endpoints
 * - Structured JSON logging
 * - Request/response middleware
 *
 * Environment Variables:
 * - WEBHOOK_PORT: Server port (default: 9000)
 * - REDIS_URL: Redis connection URL
 * - REDIS_HOST/REDIS_PORT/REDIS_PASSWORD/REDIS_DB: Individual Redis config
 * - GITHUB_WEBHOOK_SECRET: GitHub webhook HMAC secret
 * - STRIPE_WEBHOOK_SECRET: Stripe webhook signing secret
 * - SYSTEM_WEBHOOK_SECRET: Bearer token for system events
 */

import express, { Request, Response, NextFunction } from 'express';
import { randomBytes } from 'crypto';
import * as dotenv from 'dotenv';
import { getRedisClient, isRedisHealthy, disconnectRedis } from './redis.js';
import { handleGitHubWebhook } from './handlers/github.js';
import { handleStripeWebhook } from './handlers/stripe.js';
import { handleSystemEvent } from './handlers/system.js';
import { ServerMetrics, HealthStatus } from './types.js';
import { generateEventId, incrementMetric, getMetrics, log } from './utils.js';

// Re-export for backward compatibility with handler imports
export { generateEventId, incrementMetric };

// ─── Load Environment ────────────────────────────────────────────────────────

dotenv.config();

// ─── Constants ───────────────────────────────────────────────────────────────

const VERSION = '1.0.0';
const PORT = parseInt(process.env.WEBHOOK_PORT || '9000', 10);
const STARTED_AT = new Date().toISOString();

// ─── Express App Setup ───────────────────────────────────────────────────────

const app = express();

// Raw body parsing middleware - preserves raw body for signature verification
// Must be before express.json() since we need the raw Buffer
app.use(
  express.json({
    verify: (req: Request, _res: Response, buf: Buffer) => {
      // Store raw body on request for signature verification
      (req as Request & { rawBody?: Buffer }).rawBody = buf;
    },
    limit: '5mb',
  })
);

// ─── Request Logging Middleware ──────────────────────────────────────────────

app.use((req: Request, res: Response, next: NextFunction) => {
  const startTime = Date.now();
  const requestId = randomBytes(4).toString('hex');

  // Log incoming request
  log('info', `${req.method} ${req.path}`, {
    request_id: requestId,
    method: req.method,
    path: req.path,
    ip: req.ip || req.socket.remoteAddress || 'unknown',
    user_agent: req.headers['user-agent'] || 'unknown',
  });

  // Log response when finished
  res.on('finish', () => {
    const duration = Date.now() - startTime;
    log('info', `${req.method} ${req.path} -> ${res.statusCode} (${duration}ms)`, {
      request_id: requestId,
      method: req.method,
      path: req.path,
      status_code: res.statusCode,
      duration_ms: duration,
    });
  });

  next();
});

// ─── Health Check Endpoint ───────────────────────────────────────────────────

app.get('/health', async (_req: Request, res: Response) => {
  const redisHealthy = await isRedisHealthy();
  const uptimeSeconds = Math.floor((Date.now() - new Date(STARTED_AT).getTime()) / 1000);

  const health: HealthStatus = {
    status: redisHealthy ? 'healthy' : 'degraded',
    version: VERSION,
    uptime_seconds: uptimeSeconds,
    redis_connected: redisHealthy,
    timestamp: new Date().toISOString(),
  };

  const statusCode = redisHealthy ? 200 : 503;
  res.status(statusCode).json(health);
});

// ─── Metrics Endpoint ────────────────────────────────────────────────────────

app.get('/metrics', async (_req: Request, res: Response) => {
  const redisHealthy = await isRedisHealthy();
  const uptimeSeconds = Math.floor((Date.now() - new Date(STARTED_AT).getTime()) / 1000);
  const currentMetrics = getMetrics();

  const serverMetrics: ServerMetrics = {
    started_at: STARTED_AT,
    events_received: currentMetrics.events_received,
    events_published: currentMetrics.events_published,
    events_failed: currentMetrics.events_failed,
    events_by_source: currentMetrics.events_by_source,
    last_event_at: currentMetrics.last_event_at,
    redis_connected: redisHealthy,
    uptime_seconds: uptimeSeconds,
  };

  res.status(200).json(serverMetrics);
});

// ─── Webhook Routes ──────────────────────────────────────────────────────────

app.post('/webhooks/github', (req: Request, res: Response) => {
  handleGitHubWebhook(req, res).catch((err: unknown) => {
    const error = err instanceof Error ? err : new Error(String(err));
    log('error', 'Unhandled error in GitHub webhook handler', { error: error.message });
    if (!res.headersSent) {
      res.status(500).json({ error: 'Internal server error' });
    }
  });
});

app.post('/webhooks/stripe', (req: Request, res: Response) => {
  handleStripeWebhook(req, res).catch((err: unknown) => {
    const error = err instanceof Error ? err : new Error(String(err));
    log('error', 'Unhandled error in Stripe webhook handler', { error: error.message });
    if (!res.headersSent) {
      res.status(500).json({ error: 'Internal server error' });
    }
  });
});

app.post('/webhooks/system', (req: Request, res: Response) => {
  handleSystemEvent(req, res).catch((err: unknown) => {
    const error = err instanceof Error ? err : new Error(String(err));
    log('error', 'Unhandled error in system event handler', { error: error.message });
    if (!res.headersSent) {
      res.status(500).json({ error: 'Internal server error' });
    }
  });
});

// ─── 404 Handler ─────────────────────────────────────────────────────────────

app.use((_req: Request, res: Response) => {
  res.status(404).json({
    error: 'Not found',
    message: 'The requested endpoint does not exist',
    available_endpoints: [
      'POST /webhooks/github',
      'POST /webhooks/stripe',
      'POST /webhooks/system',
      'GET /health',
      'GET /metrics',
    ],
  });
});

// ─── Error Handling Middleware ────────────────────────────────────────────────

app.use((err: Error, _req: Request, res: Response, _next: NextFunction) => {
  log('error', 'Unhandled server error', {
    error: err.message,
    stack: err.stack,
  });

  if (!res.headersSent) {
    res.status(500).json({
      error: 'Internal server error',
      message: process.env.NODE_ENV === 'development' ? err.message : 'An unexpected error occurred',
    });
  }
});

// ─── Graceful Shutdown ───────────────────────────────────────────────────────

let server: ReturnType<typeof app.listen> | null = null;
let isShuttingDown = false;

async function gracefulShutdown(signal: string): Promise<void> {
  if (isShuttingDown) {
    log('warn', 'Shutdown already in progress, ignoring signal', { signal });
    return;
  }

  isShuttingDown = true;
  log('info', `Received ${signal}, starting graceful shutdown...`);

  // Stop accepting new connections
  if (server) {
    server.close(() => {
      log('info', 'HTTP server closed');
    });
  }

  // Give in-flight requests 10 seconds to complete
  const shutdownTimeout = setTimeout(() => {
    log('error', 'Shutdown timeout exceeded, forcing exit');
    process.exit(1);
  }, 10000);

  try {
    // Disconnect Redis
    await disconnectRedis();
    log('info', 'All connections closed, exiting');
    clearTimeout(shutdownTimeout);
    process.exit(0);
  } catch (err) {
    const error = err instanceof Error ? err : new Error(String(err));
    log('error', 'Error during shutdown', { error: error.message });
    clearTimeout(shutdownTimeout);
    process.exit(1);
  }
}

process.on('SIGTERM', () => { gracefulShutdown('SIGTERM'); });
process.on('SIGINT', () => { gracefulShutdown('SIGINT'); });

// Handle uncaught exceptions
process.on('uncaughtException', (err: Error) => {
  log('error', 'Uncaught exception', { error: err.message, stack: err.stack });
  gracefulShutdown('uncaughtException');
});

process.on('unhandledRejection', (reason: unknown) => {
  const message = reason instanceof Error ? reason.message : String(reason);
  log('error', 'Unhandled rejection', { reason: message });
});

// ─── Server Startup ──────────────────────────────────────────────────────────

/**
 * Start the webhook server.
 * Initializes Redis connection and begins listening for HTTP requests.
 */
export async function startServer(): Promise<void> {
  // Print startup banner
  console.log('');
  console.log('  ╔══════════════════════════════════════════════════════╗');
  console.log('  ║          RHODAWK AI - HERMES88 WEBHOOK SERVER       ║');
  console.log('  ║                                                      ║');
  console.log('  ║   Event Receiver & Redis Publisher                   ║');
  console.log(`  ║   Version: ${VERSION.padEnd(42)}║`);
  console.log('  ╚══════════════════════════════════════════════════════╝');
  console.log('');

  // Initialize Redis connection
  try {
    const redis = getRedisClient();
    log('info', 'Redis client initialized', { status: redis.status });
  } catch (err) {
    const error = err instanceof Error ? err : new Error(String(err));
    log('error', 'Failed to initialize Redis client', { error: error.message });
    log('warn', 'Server will start but events will not be published until Redis connects');
  }

  // Start HTTP server
  server = app.listen(PORT, () => {
    log('info', `Webhook server listening on port ${PORT}`, {
      port: PORT,
      version: VERSION,
      node_version: process.version,
      environment: process.env.NODE_ENV || 'development',
    });

    console.log('');
    log('info', 'Registered routes:', {
      routes: [
        'POST /webhooks/github',
        'POST /webhooks/stripe',
        'POST /webhooks/system',
        'GET /health',
        'GET /metrics',
      ],
    });
  });
}

// Auto-start if this is the main module
const isMainModule = process.argv[1]?.endsWith('server.ts') ||
  process.argv[1]?.endsWith('server.js');

if (isMainModule) {
  startServer().catch((err: unknown) => {
    const error = err instanceof Error ? err : new Error(String(err));
    log('error', 'Failed to start server', { error: error.message });
    process.exit(1);
  });
}

export { app };
