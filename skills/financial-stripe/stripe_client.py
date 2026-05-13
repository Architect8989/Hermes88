#!/usr/bin/env python3
"""
Stripe Financial Client Skill for Hermes88.
Monitors Stripe for payment events, subscription changes, revenue metrics.
Provides MRR calculation, failed payment tracking, and financial reporting.

Rhodawk AI -- Peak Architecture v10.0
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional


@dataclass
class SubscriptionInfo:
    """Represents a Stripe subscription."""
    id: str = ""
    customer_id: str = ""
    customer_email: str = ""
    plan_name: str = ""
    amount: int = 0  # in cents
    currency: str = "usd"
    status: str = ""
    interval: str = "month"
    created_at: str = ""
    current_period_end: str = ""

    def monthly_amount(self) -> float:
        """Get monthly amount in dollars."""
        amount = self.amount / 100.0
        if self.interval == "year":
            return amount / 12.0
        return amount


@dataclass
class PaymentInfo:
    """Represents a payment or charge."""
    id: str = ""
    amount: int = 0
    currency: str = "usd"
    status: str = ""
    customer_email: str = ""
    description: str = ""
    created_at: str = ""
    failure_message: str = ""

    def amount_dollars(self) -> float:
        """Amount in dollars."""
        return self.amount / 100.0


@dataclass
class RevenueReport:
    """Revenue summary report."""
    period_days: int = 30
    total_revenue: int = 0
    mrr: int = 0
    active_subscriptions: int = 0
    churned_subscriptions: int = 0
    failed_payments: int = 0
    failed_amount: int = 0
    new_subscriptions: int = 0

    def to_text(self) -> str:
        """Format as text report."""
        lines = [
            f"MRR: ${self.mrr / 100:.2f}",
            f"Active subscriptions: {self.active_subscriptions}",
            f"Revenue ({self.period_days}d): ${self.total_revenue / 100:.2f}",
            f"New subs ({self.period_days}d): {self.new_subscriptions}",
            f"Churned ({self.period_days}d): {self.churned_subscriptions}",
        ]
        if self.failed_payments > 0:
            lines.append(
                f"Failed payments ({self.period_days}d): "
                f"{self.failed_payments} (${self.failed_amount / 100:.2f} at risk)"
            )
        return "\n".join(lines)


class StripeClient:
    """
    Stripe API client for financial monitoring.
    Handles authentication, pagination, and error handling for
    Stripe API v1 calls.
    """

    BASE_URL = "https://api.stripe.com/v1"

    def __init__(self, api_key: str = ""):
        """
        Initialize Stripe client.

        Args:
            api_key: Stripe secret key (sk_live_... or sk_test_...).
        """
        self.api_key = api_key or os.environ.get("STRIPE_API_KEY", "")
        if not self.api_key:
            print("[stripe] WARNING: No API key configured", flush=True)

    def get_mrr(self) -> int:
        """
        Calculate Monthly Recurring Revenue from active subscriptions.

        Returns:
            MRR in cents.
        """
        subscriptions = self.list_subscriptions(status="active")
        mrr = 0
        for sub in subscriptions:
            mrr += int(sub.monthly_amount() * 100)
        return mrr

    def list_subscriptions(self, status: str = "active",
                           limit: int = 100) -> list:
        """
        List subscriptions with optional status filter.

        Args:
            status: Filter by status (active, past_due, canceled, all).
            limit: Maximum subscriptions to return.

        Returns:
            List of SubscriptionInfo objects.
        """
        params = {"limit": min(limit, 100)}
        if status != "all":
            params["status"] = status

        data = self._api_get("/subscriptions", params)
        if not data:
            return []

        subscriptions = []
        for item in data.get("data", []):
            plan = item.get("plan", item.get("items", {}).get("data", [{}])[0].get("plan", {}))
            sub = SubscriptionInfo(
                id=item.get("id", ""),
                customer_id=item.get("customer", ""),
                plan_name=plan.get("nickname", plan.get("id", "unknown")),
                amount=plan.get("amount", 0),
                currency=plan.get("currency", "usd"),
                status=item.get("status", ""),
                interval=plan.get("interval", "month"),
                created_at=self._format_timestamp(item.get("created", 0)),
                current_period_end=self._format_timestamp(
                    item.get("current_period_end", 0)
                ),
            )
            subscriptions.append(sub)

        return subscriptions

    def get_revenue(self, period_days: int = 30) -> RevenueReport:
        """
        Generate a revenue report for the specified period.

        Args:
            period_days: Number of days to look back.

        Returns:
            RevenueReport with aggregated metrics.
        """
        report = RevenueReport(period_days=period_days)

        # Get active subscriptions for MRR
        active_subs = self.list_subscriptions(status="active")
        report.active_subscriptions = len(active_subs)
        report.mrr = sum(int(s.monthly_amount() * 100) for s in active_subs)

        # Get charges for revenue
        since = int(time.time()) - (period_days * 86400)
        charges = self._list_charges(since=since, status="succeeded")
        report.total_revenue = sum(c.amount for c in charges)

        # Get failed payments
        failed = self._list_charges(since=since, status="failed")
        report.failed_payments = len(failed)
        report.failed_amount = sum(c.amount for c in failed)

        # Get new subscriptions
        new_subs = self._list_subscriptions_created_since(since)
        report.new_subscriptions = len(new_subs)

        # Get churned (canceled) subscriptions
        canceled = self.list_subscriptions(status="canceled")
        report.churned_subscriptions = sum(
            1 for s in canceled
            if self._parse_timestamp(s.created_at) > since
        )

        return report

    def get_failed_payments(self, period_days: int = 7) -> list:
        """
        Get failed payments in the specified period.

        Args:
            period_days: Number of days to look back.

        Returns:
            List of PaymentInfo objects for failed charges.
        """
        since = int(time.time()) - (period_days * 86400)
        return self._list_charges(since=since, status="failed")

    def get_customer_email(self, customer_id: str) -> str:
        """Get customer email by ID."""
        data = self._api_get(f"/customers/{customer_id}")
        if data:
            return data.get("email", "")
        return ""

    def _list_charges(self, since: int = 0, status: str = "",
                      limit: int = 100) -> list:
        """List charges with filters."""
        params = {"limit": min(limit, 100)}
        if since:
            params["created[gte]"] = since

        data = self._api_get("/charges", params)
        if not data:
            return []

        charges = []
        for item in data.get("data", []):
            if status and item.get("status") != status:
                continue
            if not status or item.get("status") == status:
                charge = PaymentInfo(
                    id=item.get("id", ""),
                    amount=item.get("amount", 0),
                    currency=item.get("currency", "usd"),
                    status=item.get("status", ""),
                    customer_email=item.get("receipt_email", ""),
                    description=item.get("description", ""),
                    created_at=self._format_timestamp(item.get("created", 0)),
                    failure_message=item.get("failure_message", ""),
                )
                charges.append(charge)

        return charges

    def _list_subscriptions_created_since(self, since: int) -> list:
        """List subscriptions created since a timestamp."""
        params = {
            "limit": 100,
            "created[gte]": since,
        }
        data = self._api_get("/subscriptions", params)
        if not data:
            return []
        return data.get("data", [])

    def _api_get(self, endpoint: str, params: Optional[dict] = None) -> Optional[dict]:
        """Make an authenticated GET request to Stripe API."""
        if not self.api_key:
            print("[stripe] No API key", flush=True)
            return None

        url = f"{self.BASE_URL}{endpoint}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    retry_after = int(e.headers.get("Retry-After", 2))
                    time.sleep(retry_after)
                    continue
                elif e.code == 401:
                    print("[stripe] Invalid API key", flush=True)
                    return None
                else:
                    error_body = e.read().decode(errors="replace")[:200]
                    print(f"[stripe] API error {e.code}: {error_body}", flush=True)
                    return None
            except Exception as e:
                print(f"[stripe] Request error: {e}", flush=True)
                return None

        return None

    def _format_timestamp(self, ts: int) -> str:
        """Format Unix timestamp to ISO string."""
        if not ts:
            return ""
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

    def _parse_timestamp(self, date_str: str) -> int:
        """Parse date string back to Unix timestamp."""
        if not date_str:
            return 0
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            return 0


# -- CLI Interface ---------------------------------------------------------------

def main():
    """CLI entry point for Stripe client."""
    parser = argparse.ArgumentParser(
        description="Stripe Financial Client -- Rhodawk AI Hermes88"
    )
    sub = parser.add_subparsers(dest="command")

    # MRR
    sub.add_parser("mrr", help="Calculate current MRR")

    # Revenue
    revenue_p = sub.add_parser("revenue", help="Revenue report")
    revenue_p.add_argument(
        "--period", default="30d",
        help="Period (e.g., 7d, 30d, 90d)"
    )

    # Failed payments
    failed_p = sub.add_parser("failed-payments", help="List failed payments")
    failed_p.add_argument("--period", default="7d")

    # Subscriptions
    subs_p = sub.add_parser("subscriptions", help="List subscriptions")
    subs_p.add_argument("--active", action="store_true")
    subs_p.add_argument("--all", action="store_true")

    # Full report
    sub.add_parser("report", help="Full financial report")

    args = parser.parse_args()
    client = StripeClient()

    if args.command == "mrr":
        mrr = client.get_mrr()
        print(f"MRR: ${mrr / 100:.2f}")

    elif args.command == "revenue":
        days = int(args.period.rstrip("d"))
        report = client.get_revenue(period_days=days)
        print(report.to_text())

    elif args.command == "failed-payments":
        days = int(args.period.rstrip("d"))
        failed = client.get_failed_payments(period_days=days)
        if failed:
            print(f"Failed payments (last {days} days): {len(failed)}")
            for p in failed:
                print(
                    f"  ${p.amount_dollars():.2f} - {p.customer_email} "
                    f"- {p.failure_message or 'unknown reason'}"
                )
        else:
            print(f"No failed payments in the last {days} days.")

    elif args.command == "subscriptions":
        status = "all" if args.all else "active"
        subs = client.list_subscriptions(status=status)
        print(f"Subscriptions ({status}): {len(subs)}")
        for s in subs:
            print(
                f"  {s.plan_name}: ${s.monthly_amount():.2f}/mo "
                f"({s.status}) - {s.customer_email or s.customer_id}"
            )

    elif args.command == "report":
        report = client.get_revenue(period_days=30)
        print("=== Rhodawk AI Financial Report ===")
        print(report.to_text())
        print(f"\nRunway estimate: {_estimate_runway(report)}")

    else:
        parser.print_help()
        sys.exit(1)


def _estimate_runway(report: RevenueReport) -> str:
    """Estimate runway based on current metrics."""
    if report.mrr <= 0:
        return "Pre-revenue (runway depends on bank balance)"
    # Rough estimate assuming ~$3k/mo burn for solo founder
    monthly_burn = 3000_00  # cents
    net = report.mrr - monthly_burn
    if net >= 0:
        return "Profitable (infinite runway at current burn)"
    months = abs(250000_00 / net)  # Assume $250k in bank
    return f"~{months:.0f} months at current burn"


if __name__ == "__main__":
    main()
