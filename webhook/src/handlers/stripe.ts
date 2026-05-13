/**
 * Rhodawk AI - Hermes88 Stripe Webhook Handler
 *
 * Processes incoming Stripe webhook events, verifies signatures,
 * routes events to appropriate handlers, and publishes structured
 * HermesEvent objects to Redis.
 *
 * Supported events:
 * - invoice.payment_failed: Payment failure (CRITICAL)
 * - invoice.payment_succeeded: Successful payment
 * - customer.subscription.updated: Subscription changes
 * - charge.dispute.created: Dispute/chargeback (CRITICAL)
 */

import { createHmac, timingSafeEqual } from 'crypto';
import { Request, Response } from 'express';
import { publishEvent, publishToMultipleChannels, EVENTS_CHANNEL, ALERTS_CHANNEL } from '../redis.js';
import {
  HermesEvent,
  Priority,
  EVENT_TYPES,
  StripePaymentFailedEvent,
  StripePaymentSucceededEvent,
  StripeSubscriptionUpdatedEvent,
  StripeDisputeCreatedEvent,
} from '../types.js';
import { generateEventId, incrementMetric } from '../utils.js';

// ─── Logger ──────────────────────────────────────────────────────────────────

function log(level: string, message: string, data?: Record<string, unknown>): void {
  const entry = {
    timestamp: new Date().toISOString(),
    level,
    component: 'handler:stripe',
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
 * Verify the Stripe webhook signature.
 * Stripe uses a timestamp + signature scheme with HMAC-SHA256.
 * The signature header format is: t=<timestamp>,v1=<signature>
 *
 * @param payload - Raw request body as Buffer
 * @param signatureHeader - The Stripe-Signature header value
 * @param secret - The webhook endpoint secret (whsec_...)
 * @returns true if the signature is valid and timestamp is recent
 */
export function verifyStripeSignature(
  payload: Buffer,
  signatureHeader: string,
  secret: string
): boolean {
  if (!signatureHeader || !secret) {
    return false;
  }

  try {
    // Parse the signature header
    const elements = signatureHeader.split(',');
    const timestampElement = elements.find((e) => e.startsWith('t='));
    const signatureElement = elements.find((e) => e.startsWith('v1='));

    if (!timestampElement || !signatureElement) {
      return false;
    }

    const timestamp = timestampElement.slice(2);
    const expectedSignature = signatureElement.slice(3);

    // Check timestamp freshness (allow 5 minutes tolerance)
    const timestampAge = Math.abs(Date.now() / 1000 - parseInt(timestamp, 10));
    if (timestampAge > 300) {
      log('warn', 'Stripe webhook timestamp too old', { age_seconds: timestampAge });
      return false;
    }

    // Compute expected signature
    const signedPayload = `${timestamp}.${payload.toString('utf8')}`;
    const computedSignature = createHmac('sha256', secret)
      .update(signedPayload)
      .digest('hex');

    // Timing-safe comparison
    const sigBuffer = Buffer.from(expectedSignature, 'hex');
    const computedBuffer = Buffer.from(computedSignature, 'hex');

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
 * Express route handler for Stripe webhooks.
 * Verifies signature, parses event type, and routes to specific handlers.
 */
export async function handleStripeWebhook(req: Request, res: Response): Promise<void> {
  const startTime = Date.now();
  const signatureHeader = req.headers['stripe-signature'] as string | undefined;

  // Verify signature
  const secret = process.env.STRIPE_WEBHOOK_SECRET;
  if (!secret) {
    log('error', 'STRIPE_WEBHOOK_SECRET not configured');
    res.status(500).json({ error: 'Webhook secret not configured' });
    return;
  }

  const rawBody = (req as Request & { rawBody?: Buffer }).rawBody;
  if (!rawBody) {
    log('error', 'No raw body available for signature verification');
    res.status(400).json({ error: 'No request body' });
    return;
  }

  if (!signatureHeader || !verifyStripeSignature(rawBody, signatureHeader, secret)) {
    log('error', 'Invalid Stripe webhook signature');
    incrementMetric('events_failed');
    res.status(401).json({ error: 'Invalid signature' });
    return;
  }

  const body = req.body as Record<string, unknown>;
  const eventType = (body.type as string) || 'unknown';
  const stripeEventId = (body.id as string) || '';

  log('info', `Received Stripe event: ${eventType}`, {
    stripe_event_id: stripeEventId,
    event_type: eventType,
  });

  try {
    switch (eventType) {
      case 'invoice.payment_failed':
        await handlePaymentFailed(body);
        break;
      case 'invoice.payment_succeeded':
        await handlePaymentSucceeded(body);
        break;
      case 'customer.subscription.updated':
        await handleSubscriptionUpdated(body);
        break;
      case 'charge.dispute.created':
        await handleDisputeCreated(body);
        break;
      default:
        log('info', `Unhandled Stripe event type: ${eventType}`, { stripe_event_id: stripeEventId });
        res.status(200).json({ status: 'ignored', event: eventType });
        return;
    }

    incrementMetric('events_received');
    incrementMetric('stripe');

    const duration = Date.now() - startTime;
    log('info', `Stripe event processed in ${duration}ms`, {
      event_type: eventType,
      stripe_event_id: stripeEventId,
      duration_ms: duration,
    });

    res.status(200).json({ status: 'processed', event: eventType });
  } catch (err) {
    const error = err instanceof Error ? err : new Error(String(err));
    log('error', `Failed to process Stripe event: ${error.message}`, {
      event_type: eventType,
      stripe_event_id: stripeEventId,
      error: error.message,
      stack: error.stack,
    });
    incrementMetric('events_failed');
    res.status(500).json({ error: 'Internal processing error' });
  }
}

// ─── Per-Event Handlers ──────────────────────────────────────────────────────

/**
 * Handle payment failed events. CRITICAL priority - revenue loss.
 */
async function handlePaymentFailed(body: Record<string, unknown>): Promise<void> {
  const data = body.data as Record<string, unknown> | undefined;
  const object = data?.object as Record<string, unknown> | undefined;

  if (!object) {
    log('warn', 'payment_failed event missing data.object');
    return;
  }

  const customer = (object.customer as string) || '';
  const customerEmail = (object.customer_email as string) || '';
  const chargeData = object.charge as Record<string, unknown> | undefined;
  const failureMessage = (chargeData?.failure_message as string) ||
    ((object.last_finalization_error as Record<string, unknown>)?.message as string) || 'Unknown failure';

  const payload: StripePaymentFailedEvent = {
    source: 'stripe',
    stripe_event_id: (body.id as string) || '',
    api_version: (body.api_version as string) || '',
    livemode: (body.livemode as boolean) || false,
    event_type: 'invoice.payment_failed',
    customer_id: customer,
    customer_email: customerEmail,
    invoice_id: (object.id as string) || '',
    amount_due: (object.amount_due as number) || 0,
    currency: (object.currency as string) || 'usd',
    attempt_count: (object.attempt_count as number) || 0,
    next_retry_at: object.next_payment_attempt
      ? new Date((object.next_payment_attempt as number) * 1000).toISOString()
      : null,
    failure_message: failureMessage,
  };

  const event: HermesEvent = {
    id: generateEventId(),
    type: EVENT_TYPES.STRIPE_PAYMENT_FAILED,
    timestamp: new Date().toISOString(),
    priority: Priority.CRITICAL,
    source: 'stripe',
    payload,
    metadata: {
      customer_email: customerEmail,
      amount: String(payload.amount_due),
    },
  };

  // CRITICAL: publish to both channels
  await publishToMultipleChannels([EVENTS_CHANNEL, ALERTS_CHANNEL], event);
}

/**
 * Handle payment succeeded events. NORMAL priority - confirmation.
 */
async function handlePaymentSucceeded(body: Record<string, unknown>): Promise<void> {
  const data = body.data as Record<string, unknown> | undefined;
  const object = data?.object as Record<string, unknown> | undefined;

  if (!object) {
    log('warn', 'payment_succeeded event missing data.object');
    return;
  }

  const payload: StripePaymentSucceededEvent = {
    source: 'stripe',
    stripe_event_id: (body.id as string) || '',
    api_version: (body.api_version as string) || '',
    livemode: (body.livemode as boolean) || false,
    event_type: 'invoice.payment_succeeded',
    customer_id: (object.customer as string) || '',
    customer_email: (object.customer_email as string) || '',
    invoice_id: (object.id as string) || '',
    amount_paid: (object.amount_paid as number) || 0,
    currency: (object.currency as string) || 'usd',
    subscription_id: (object.subscription as string) || null,
  };

  const event: HermesEvent = {
    id: generateEventId(),
    type: EVENT_TYPES.STRIPE_PAYMENT_SUCCEEDED,
    timestamp: new Date().toISOString(),
    priority: Priority.NORMAL,
    source: 'stripe',
    payload,
    metadata: {
      customer_email: payload.customer_email,
      amount: String(payload.amount_paid),
    },
  };

  await publishEvent(EVENTS_CHANNEL, event);
}

/**
 * Handle subscription updated events. NORMAL priority unless cancellation.
 */
async function handleSubscriptionUpdated(body: Record<string, unknown>): Promise<void> {
  const data = body.data as Record<string, unknown> | undefined;
  const object = data?.object as Record<string, unknown> | undefined;
  const previousAttributes = (data?.previous_attributes as Record<string, unknown>) || {};

  if (!object) {
    log('warn', 'subscription_updated event missing data.object');
    return;
  }

  const cancelAtPeriodEnd = (object.cancel_at_period_end as boolean) || false;
  const status = (object.status as string) || 'unknown';

  const payload: StripeSubscriptionUpdatedEvent = {
    source: 'stripe',
    stripe_event_id: (body.id as string) || '',
    api_version: (body.api_version as string) || '',
    livemode: (body.livemode as boolean) || false,
    event_type: 'customer.subscription.updated',
    customer_id: (object.customer as string) || '',
    subscription_id: (object.id as string) || '',
    status,
    plan_id: ((object.plan as Record<string, unknown>)?.id as string) || '',
    current_period_start: object.current_period_start
      ? new Date((object.current_period_start as number) * 1000).toISOString()
      : '',
    current_period_end: object.current_period_end
      ? new Date((object.current_period_end as number) * 1000).toISOString()
      : '',
    cancel_at_period_end: cancelAtPeriodEnd,
    previous_attributes: previousAttributes,
  };

  // Cancellations and past-due status are HIGH priority
  const isChurn = cancelAtPeriodEnd || status === 'past_due' || status === 'canceled';
  const priority = isChurn ? Priority.HIGH : Priority.NORMAL;

  const event: HermesEvent = {
    id: generateEventId(),
    type: EVENT_TYPES.STRIPE_SUBSCRIPTION_UPDATED,
    timestamp: new Date().toISOString(),
    priority,
    source: 'stripe',
    payload,
    metadata: {
      status,
      cancel_at_period_end: String(cancelAtPeriodEnd),
    },
  };

  if (isChurn) {
    await publishToMultipleChannels([EVENTS_CHANNEL, ALERTS_CHANNEL], event);
  } else {
    await publishEvent(EVENTS_CHANNEL, event);
  }
}

/**
 * Handle dispute created events. CRITICAL priority - requires immediate action.
 */
async function handleDisputeCreated(body: Record<string, unknown>): Promise<void> {
  const data = body.data as Record<string, unknown> | undefined;
  const object = data?.object as Record<string, unknown> | undefined;

  if (!object) {
    log('warn', 'dispute_created event missing data.object');
    return;
  }

  const evidenceDetails = object.evidence_details as Record<string, unknown> | undefined;

  const payload: StripeDisputeCreatedEvent = {
    source: 'stripe',
    stripe_event_id: (body.id as string) || '',
    api_version: (body.api_version as string) || '',
    livemode: (body.livemode as boolean) || false,
    event_type: 'charge.dispute.created',
    dispute_id: (object.id as string) || '',
    charge_id: (object.charge as string) || '',
    amount: (object.amount as number) || 0,
    currency: (object.currency as string) || 'usd',
    reason: (object.reason as string) || 'unknown',
    status: (object.status as string) || 'needs_response',
    customer_id: (object.customer as string) || '',
    evidence_due_by: evidenceDetails?.due_by
      ? new Date((evidenceDetails.due_by as number) * 1000).toISOString()
      : '',
  };

  const event: HermesEvent = {
    id: generateEventId(),
    type: EVENT_TYPES.STRIPE_DISPUTE_CREATED,
    timestamp: new Date().toISOString(),
    priority: Priority.CRITICAL,
    source: 'stripe',
    payload,
    metadata: {
      reason: payload.reason,
      amount: String(payload.amount),
    },
  };

  // CRITICAL: publish to both channels
  await publishToMultipleChannels([EVENTS_CHANNEL, ALERTS_CHANNEL], event);
}
