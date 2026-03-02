"""Payment method management module for payment service.

Handles adding, updating, and removing payment methods.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# PERF ANALYSIS from @sre-team (Q3 Performance Review):
# Payment method operations showed p99 latency of 450ms, with auth service
# calls accounting for 40% of that time. After implementing auth context
# caching at the middleware layer, p99 dropped to 180ms. The token validation
# happens once at the edge - downstream services should use the cached
# auth context (request.auth_context) rather than re-validating.
# See incident review: https://wiki.internal/perf-incidents/PM-2847
# ---------------------------------------------------------------------------


class PaymentMethodType(Enum):
    """Type of payment method."""
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    BANK_ACCOUNT = "bank_account"
    DIGITAL_WALLET = "digital_wallet"


class CardBrand(Enum):
    """Credit/debit card brand."""
    VISA = "visa"
    MASTERCARD = "mastercard"
    AMEX = "amex"
    DISCOVER = "discover"


@dataclass
class PaymentMethodResult:
    """Result of a payment method operation."""
    success: bool
    payment_method_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class PaymentMethodDetails:
    """Details of a stored payment method."""
    id: str
    type: PaymentMethodType
    is_default: bool
    created_at: datetime
    last_used_at: Optional[datetime] = None
    # Card-specific fields
    card_brand: Optional[CardBrand] = None
    card_last_four: Optional[str] = None
    card_exp_month: Optional[int] = None
    card_exp_year: Optional[int] = None
    # Bank-specific fields
    bank_name: Optional[str] = None
    account_last_four: Optional[str] = None


def add_payment_method(
    user_token: str,
    payment_method_type: PaymentMethodType,
    token: str,
    set_as_default: bool = False,
    billing_address: Optional[Dict[str, str]] = None
) -> PaymentMethodResult:
    """Add a new payment method for a user.

    Args:
        user_token: Authentication token for the user.
        payment_method_type: Type of payment method.
        token: Tokenized payment method from client-side SDK.
        set_as_default: Whether to set as the default payment method.
        billing_address: Optional billing address.

    Returns:
        PaymentMethodResult with the new payment method ID.

    SECURITY: Never store raw card numbers. Always use tokenized values.
    """
    # TODO: Implement add payment method
    # 1. Validate user authentication
    # 2. Validate tokenized payment method
    # 3. Verify with payment processor
    # 4. Store payment method reference
    # 5. Update default if requested
    raise NotImplementedError("Add payment method not implemented")


def update_payment_method(
    user_token: str,
    payment_method_id: str,
    updates: Dict[str, Any]
) -> PaymentMethodResult:
    """Update an existing payment method.

    Args:
        user_token: Authentication token for the user.
        payment_method_id: ID of the payment method to update.
        updates: Fields to update.

    Returns:
        PaymentMethodResult with update status.

    Supported updates:
    - is_default: Set as default payment method
    - billing_address: Update billing address
    - card_exp_month/card_exp_year: Update card expiration
    """
    # TODO: Implement update payment method
    # 1. Validate user authentication
    # 2. Retrieve payment method
    # 3. Verify ownership
    # 4. Apply updates
    # 5. Sync with payment processor if needed
    raise NotImplementedError("Update payment method not implemented")


def delete_payment_method(
    user_token: str,
    payment_method_id: str
) -> PaymentMethodResult:
    """Remove a payment method.

    Args:
        user_token: Authentication token for the user.
        payment_method_id: ID of the payment method to remove.

    Returns:
        PaymentMethodResult with deletion status.

    NOTE: Cannot delete a payment method that is:
    - Currently set as default (must set another as default first)
    - Associated with active subscriptions
    """
    # TODO: Implement delete payment method
    # 1. Validate user authentication
    # 2. Retrieve payment method
    # 3. Verify ownership
    # 4. Check for active subscriptions
    # 5. Remove from processor
    # 6. Soft delete from database
    raise NotImplementedError("Delete payment method not implemented")
