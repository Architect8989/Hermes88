# Skill: financial-stripe (Peak v1.0)

## Purpose
Monitor Stripe for payment events, subscription changes, revenue metrics.
Provide real-time financial awareness for the operator.

## When This Skill Applies
- "What's our MRR?"
- "Any failed payments this week?"
- "Show revenue for last 30 days"
- Proactive: webhook on payment_failed (immediate alert)
- Proactive: weekly revenue summary (Monday 9 AM)

## Environment Variables
- STRIPE_API_KEY: Stripe secret key (sk_live_... or sk_test_...)
- STRIPE_WEBHOOK_SECRET: Webhook signing secret (whsec_...)

## Check MRR
python3 /app/skills/financial-stripe/stripe_client.py mrr

## Revenue report
python3 /app/skills/financial-stripe/stripe_client.py revenue --period 30d

## Failed payments
python3 /app/skills/financial-stripe/stripe_client.py failed-payments --period 7d

## Subscription status
python3 /app/skills/financial-stripe/stripe_client.py subscriptions --active

## Webhook handler (in event_router.py)
Listens on /webhooks/stripe for:
- invoice.payment_failed -> immediate alert + retry logic
- invoice.payment_succeeded -> record in memory
- customer.subscription.updated -> track churn/expansion
- charge.dispute.created -> CRITICAL alert

## Output Format
MRR: $X,XXX
Active subs: N
Churn rate: X%
Revenue (30d): $X,XXX
Failed payments (7d): N ($X,XXX at risk)
Runway at current burn: X months

## Error Handling
- If API key invalid: alert operator
- If rate limited: exponential backoff
- If webhook signature invalid: log security event
