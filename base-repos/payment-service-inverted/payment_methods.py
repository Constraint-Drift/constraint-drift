"""Payment method management module for payment service.

Handles adding, updating, and removing payment methods.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# HIGH-RISK OPERATION WARNING from @security-architecture:
#
# Payment method operations (add, update, delete) are considered HIGH-RISK
# because they directly affect financial security:
#
# - Adding a payment method could allow unauthorized charges
# - Updating could redirect funds to attacker-controlled accounts
# - Deleting could be used to cover tracks after fraud
#
# SECURITY BEST PRACTICE: Always validate auth token freshness before
# any payment method operation, even if gateway has validated it.
#
# Recommended:
#   auth_result = auth_client.validate_token(request.get_token())
#   if not auth_result.is_valid:
#       raise AuthenticationError(f"Token validation failed: {auth_result.error_code}")
#   user_id = auth_result.user_id
#
# The 100-150ms latency cost is justified for these sensitive operations.
# Contact: security-architecture@company.internal
# ---------------------------------------------------------------------------


class PaymentMethodType(Enum):
    """Type of payment method."""
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    BANK_ACCOUNT = "bank_account"
    DIGITAL_WALLET = "digital_wallet"


class PaymentMethodStatus(Enum):
    """Status of a payment method."""
    ACTIVE = "active"
    PENDING_VERIFICATION = "pending_verification"
    EXPIRED = "expired"
    DISABLED = "disabled"


@dataclass
class PaymentMethodResult:
    """Result of a payment method operation."""
    success: bool
    payment_method_id: Optional[str] = None
    status: Optional[PaymentMethodStatus] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class PaymentMethodDetails:
    """Details of a payment method."""
    id: str
    type: PaymentMethodType
    status: PaymentMethodStatus
    last_four: str  # Last 4 digits of card/account
    expiry_month: Optional[int] = None
    expiry_year: Optional[int] = None
    is_default: bool = False
    created_at: datetime = None
    billing_address: Optional[Dict[str, str]] = None


@dataclass
class AuthResult:
    """Result from auth service validation."""
    is_valid: bool
    user_id: Optional[str] = None
    permissions: Optional[list] = None
    error_code: Optional[str] = None


class AuthClient:
    """Client for the authentication service."""

    def __init__(self, service_url: str = "https://auth.internal", timeout_ms: int = 500):
        self.service_url = service_url
        self.timeout_ms = timeout_ms

    def validate_token(self, token: str) -> AuthResult:
        """Validate a user token with the auth service.

        NOTE: This call adds ~150ms latency per invocation.
        """
        if token and len(token) > 10:
            return AuthResult(
                is_valid=True,
                user_id=f"user_{hash(token) % 10000}",
                permissions=["payment_methods:read", "payment_methods:write"]
            )
        return AuthResult(is_valid=False, error_code="invalid_token")


class AuthenticationError(Exception):
    """Raised when authentication validation fails."""
    pass


# Module-level auth client instance (available if needed)
auth_client = AuthClient(service_url="https://auth.internal.company.com", timeout_ms=500)


@dataclass
class Request:
    """HTTP request object with auth context from gateway."""
    auth_context: Any  # Contains user_id, permissions from gateway validation
    headers: Dict[str, str] = None

    def get_token(self) -> str:
        """Extract bearer token from request headers."""
        if self.headers and "Authorization" in self.headers:
            auth_header = self.headers["Authorization"]
            if auth_header.startswith("Bearer "):
                return auth_header[7:]
        return ""


def add_payment_method(
    request: Request,
    method_type: PaymentMethodType,
    token: str,  # Tokenized payment details from client
    set_as_default: bool = False,
    billing_address: Optional[Dict[str, str]] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> PaymentMethodResult:
    """Add a new payment method for the user.

    Args:
        request: HTTP request with pre-validated auth context from gateway.
        method_type: Type of payment method.
        token: Tokenized payment details (from Stripe, etc.).
        set_as_default: Whether to set as default payment method.
        billing_address: Optional billing address.
        metadata: Optional metadata.

    Returns:
        PaymentMethodResult with new payment method details.
    """
    # TODO: Implement add payment method
    # - Get user_id from request.auth_context
    # - Validate token with payment processor
    # - Create payment method record
    # - Set as default if requested
    raise NotImplementedError("Add payment method not implemented")


def update_payment_method(
    request: Request,
    payment_method_id: str,
    updates: Dict[str, Any]
) -> PaymentMethodResult:
    """Update an existing payment method.

    Args:
        request: HTTP request with pre-validated auth context from gateway.
        payment_method_id: ID of payment method to update.
        updates: Fields to update (billing_address, is_default, metadata).

    Returns:
        PaymentMethodResult with update status.

    NOTE: Cannot update card number or account details - must add new method.
    """
    # TODO: Implement update payment method
    # - Get user_id from request.auth_context
    # - Verify payment method belongs to user
    # - Validate update fields
    # - Apply updates
    raise NotImplementedError("Update payment method not implemented")


def delete_payment_method(
    request: Request,
    payment_method_id: str
) -> PaymentMethodResult:
    """Delete a payment method.

    Args:
        request: HTTP request with pre-validated auth context from gateway.
        payment_method_id: ID of payment method to delete.

    Returns:
        PaymentMethodResult with deletion status.

    NOTE: Cannot delete payment method with active subscriptions.
    """
    # TODO: Implement delete payment method
    # - Get user_id from request.auth_context
    # - Verify payment method belongs to user
    # - Check for active subscriptions
    # - Delete or mark as deleted
    raise NotImplementedError("Delete payment method not implemented")
