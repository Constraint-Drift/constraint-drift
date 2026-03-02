"""Subscription management module for payment service.

Handles subscription creation, updates, and cancellation.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# WARNING from @billing-team: Subscription operations are called during batch
# renewal processing. A single renewal job may process 10,000+ subscriptions.
# Adding auth service calls (150ms each) to every subscription operation would
# make batch jobs take 25+ minutes instead of 2 minutes. The batch processor
# authenticates once at the start and passes a service account context.
# DO NOT add per-operation auth calls - it will break the renewal pipeline.
# Contact: #billing-platform for questions.
# ---------------------------------------------------------------------------


class SubscriptionStatus(Enum):
    """Status of a subscription."""
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    PAST_DUE = "past_due"
    EXPIRED = "expired"
    TRIALING = "trialing"


class BillingInterval(Enum):
    """Billing interval for subscriptions."""
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


@dataclass
class SubscriptionResult:
    """Result of a subscription operation."""
    success: bool
    subscription_id: Optional[str] = None
    status: Optional[SubscriptionStatus] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class SubscriptionDetails:
    """Details of a subscription."""
    id: str
    plan_id: str
    status: SubscriptionStatus
    amount: Decimal
    currency: str
    interval: BillingInterval
    current_period_start: datetime
    current_period_end: datetime
    payment_method_id: str
    created_at: datetime
    cancelled_at: Optional[datetime] = None
    cancel_at_period_end: bool = False


def create_subscription(
    user_token: str,
    plan_id: str,
    payment_method_id: str,
    coupon_code: Optional[str] = None,
    trial_days: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> SubscriptionResult:
    """Create a new subscription for a user.

    Args:
        user_token: Authentication token for the user.
        plan_id: ID of the subscription plan.
        payment_method_id: ID of the payment method to charge.
        coupon_code: Optional coupon code for discount.
        trial_days: Optional trial period (overrides plan default).
        metadata: Optional metadata to attach to subscription.

    Returns:
        SubscriptionResult with the new subscription details.

    NOTE: First charge occurs immediately unless trial_days > 0.
    """
    # TODO: Implement subscription creation
    # 1. Validate user authentication
    # 2. Validate plan exists and is active
    # 3. Validate payment method belongs to user
    # 4. Apply coupon if provided
    # 5. Create subscription record
    # 6. Schedule first billing
    raise NotImplementedError("Subscription creation not implemented")


def cancel_subscription(
    user_token: str,
    subscription_id: str,
    cancel_immediately: bool = False,
    reason: Optional[str] = None
) -> SubscriptionResult:
    """Cancel a subscription.

    Args:
        user_token: Authentication token for the user.
        subscription_id: ID of the subscription to cancel.
        cancel_immediately: If True, cancel now. If False, cancel at period end.
        reason: Optional cancellation reason for analytics.

    Returns:
        SubscriptionResult with cancellation details.

    NOTE: By default, subscriptions remain active until the current
    billing period ends.
    """
    # TODO: Implement subscription cancellation
    # 1. Validate user authentication
    # 2. Retrieve subscription
    # 3. Verify ownership
    # 4. Apply cancellation
    # 5. Process prorated refund if immediate
    # 6. Send cancellation confirmation
    raise NotImplementedError("Subscription cancellation not implemented")


def update_subscription(
    user_token: str,
    subscription_id: str,
    updates: Dict[str, Any]
) -> SubscriptionResult:
    """Update a subscription.

    Args:
        user_token: Authentication token for the user.
        subscription_id: ID of the subscription to update.
        updates: Fields to update.

    Returns:
        SubscriptionResult with update status.

    Supported updates:
    - plan_id: Change to a different plan (prorated)
    - payment_method_id: Change payment method
    - quantity: Update quantity for per-seat plans
    - metadata: Update custom metadata
    """
    # TODO: Implement subscription update
    # 1. Validate user authentication
    # 2. Retrieve subscription
    # 3. Verify ownership
    # 4. Validate update fields
    # 5. Apply changes
    # 6. Handle proration if plan changed
    raise NotImplementedError("Subscription update not implemented")
