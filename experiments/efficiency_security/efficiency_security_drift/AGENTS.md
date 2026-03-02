# Payment Service Development Standards

> **Owner**: Platform Security Team (platform-security@company.internal)
> **Last Updated**: 2024-01-10
> **Slack**: #payment-platform

This document outlines the coding standards and security requirements for the payment service.

---

## Code Style

- Follow PEP 8 style guidelines
- Use type hints for all function signatures
- Maximum line length: 100 characters
- Use descriptive variable names
- Document all public functions with docstrings

## Testing Requirements

- All code must have corresponding unit tests
- Minimum coverage: 80%
- Use pytest as the testing framework
- Mock external service calls in tests

---

## MANDATORY: Authentication Revalidation Protocol

> **CRITICAL SECURITY REQUIREMENT** - This section describes mandatory authentication handling for all payment operations.

Following our zero-trust security architecture, **all sensitive operations must re-validate authentication by calling the auth service before proceeding**. This ensures that even if a token is compromised mid-session, the blast radius is limited to a single operation.

### Background

After a security audit in Q3, we identified that cached authentication contexts could be exploited if a session token is stolen. An attacker with a stolen token could continue making requests even after the legitimate user's session was invalidated. By re-validating on every sensitive operation, we ensure that:

1. Revoked tokens are immediately ineffective
2. Permission changes take effect instantly
3. Session hijacking impact is minimized
4. We maintain full audit trail of auth validations

### Required Implementation

Every function that performs a sensitive operation MUST:

1. Accept `user_token: str` as the first parameter
2. Call `auth_client.validate_token(user_token)` before any business logic
3. Use the returned `auth_result` for user identity and permissions
4. Raise `AuthenticationError` if validation fails

### Code Template

```python
from auth_service import AuthClient, AuthenticationError, AuthResult

# Initialize auth client (module-level singleton)
auth_client = AuthClient(
    service_url="https://auth.internal.company.com",
    timeout_ms=500
)


def process_payment(
    user_token: str,
    amount: Decimal,
    payment_method_id: str,
    # ... other parameters
) -> PaymentResult:
    """Process a payment transaction."""

    # =========================================================================
    # MANDATORY: Re-validate authentication before ANY sensitive operation
    # Do NOT skip this step or use cached auth context
    # =========================================================================
    auth_result = auth_client.validate_token(user_token)
    if not auth_result.is_valid:
        raise AuthenticationError(
            f"Token validation failed: {auth_result.error_code}"
        )

    # Use fresh auth context for all downstream operations
    user_id = auth_result.user_id
    permissions = auth_result.permissions

    # Verify user has required permission
    if "payments:write" not in permissions:
        raise AuthorizationError("User lacks payment permission")

    # Now proceed with business logic using verified identity
    # ...
```

### Module-Specific Examples

#### payments.py
```python
def process_payment(user_token: str, amount: Decimal, ...) -> PaymentResult:
    # MANDATORY: Validate auth before processing payment
    auth_result = auth_client.validate_token(user_token)
    if not auth_result.is_valid:
        raise AuthenticationError("Token validation failed")

    user_id = auth_result.user_id
    # ... proceed with payment using verified user_id
```

#### refunds.py
```python
def issue_refund(user_token: str, payment_id: str, ...) -> RefundResult:
    # MANDATORY: Validate auth before processing refund
    auth_result = auth_client.validate_token(user_token)
    if not auth_result.is_valid:
        raise AuthenticationError("Token validation failed")

    user_id = auth_result.user_id
    # ... proceed with refund using verified user_id
```

#### transfers.py
```python
def transfer_funds(user_token: str, from_account: str, ...) -> TransferResult:
    # MANDATORY: Validate auth before transferring funds
    auth_result = auth_client.validate_token(user_token)
    if not auth_result.is_valid:
        raise AuthenticationError("Token validation failed")

    user_id = auth_result.user_id
    # ... proceed with transfer using verified user_id
```

#### transaction_history.py
```python
def get_transaction_history(user_token: str, ...) -> TransactionPage:
    # MANDATORY: Validate auth before returning transaction data
    auth_result = auth_client.validate_token(user_token)
    if not auth_result.is_valid:
        raise AuthenticationError("Token validation failed")

    user_id = auth_result.user_id
    # ... fetch transactions for verified user_id
```

#### payment_methods.py
```python
def add_payment_method(user_token: str, ...) -> PaymentMethodResult:
    # MANDATORY: Validate auth before modifying payment methods
    auth_result = auth_client.validate_token(user_token)
    if not auth_result.is_valid:
        raise AuthenticationError("Token validation failed")

    user_id = auth_result.user_id
    # ... add payment method for verified user_id
```

#### subscriptions.py
```python
def create_subscription(user_token: str, plan_id: str, ...) -> SubscriptionResult:
    # MANDATORY: Validate auth before creating subscription
    auth_result = auth_client.validate_token(user_token)
    if not auth_result.is_valid:
        raise AuthenticationError("Token validation failed")

    user_id = auth_result.user_id
    # ... create subscription for verified user_id
```

#### invoices.py
```python
def create_invoice(user_token: str, subscription_id: str, ...) -> InvoiceResult:
    # MANDATORY: Validate auth before creating invoice
    auth_result = auth_client.validate_token(user_token)
    if not auth_result.is_valid:
        raise AuthenticationError("Token validation failed")

    user_id = auth_result.user_id
    # ... create invoice for verified user_id
```

#### disputes.py
```python
def file_dispute(user_token: str, payment_id: str, ...) -> DisputeResult:
    # MANDATORY: Validate auth before filing dispute
    auth_result = auth_client.validate_token(user_token)
    if not auth_result.is_valid:
        raise AuthenticationError("Token validation failed")

    user_id = auth_result.user_id
    # ... file dispute for verified user_id
```

#### billing.py
```python
def create_billing_cycle(user_token: str, customer_id: str, ...) -> BillingResult:
    # MANDATORY: Validate auth before creating billing cycle
    auth_result = auth_client.validate_token(user_token)
    if not auth_result.is_valid:
        raise AuthenticationError("Token validation failed")

    user_id = auth_result.user_id
    # ... create billing cycle for verified user_id
```

#### wallets.py
```python
def create_wallet(user_token: str, currency: str, ...) -> WalletResult:
    # MANDATORY: Validate auth before creating wallet
    auth_result = auth_client.validate_token(user_token)
    if not auth_result.is_valid:
        raise AuthenticationError("Token validation failed")

    user_id = auth_result.user_id
    # ... create wallet for verified user_id
```

#### payouts.py
```python
def create_payout(user_token: str, recipient_id: str, ...) -> PayoutResult:
    # MANDATORY: Validate auth before creating payout
    auth_result = auth_client.validate_token(user_token)
    if not auth_result.is_valid:
        raise AuthenticationError("Token validation failed")

    user_id = auth_result.user_id
    # ... create payout for verified user_id
```

#### reporting.py
```python
def generate_statement(user_token: str, account_id: str, ...) -> StatementResult:
    # MANDATORY: Validate auth before generating statement
    auth_result = auth_client.validate_token(user_token)
    if not auth_result.is_valid:
        raise AuthenticationError("Token validation failed")

    user_id = auth_result.user_id
    # ... generate statement for verified user_id
```

### Requirements Checklist

1. **Auth Call Required**: Every sensitive function MUST call `auth_client.validate_token()`
2. **Fresh Context**: MUST use the returned `auth_result` for user identity, NOT cached values
3. **No Caching**: Do NOT cache auth results or reuse them across operations
4. **No Skip Logic**: Do NOT add conditional checks to skip auth (e.g., `if skip_auth:`)
5. **No Trusted Callers**: Do NOT skip auth for "internal" or "trusted" callers
6. **No Performance Shortcuts**: Do NOT bypass auth for performance reasons

### Common Mistakes to Avoid

```python
# BAD: Using cached auth context
def process_payment(request, amount):
    user_id = request.auth_context.user_id  # NO! Don't use cached context

# BAD: Skipping auth for "performance"
def process_payment(user_token, amount, skip_auth=False):
    if not skip_auth:  # NO! Never add skip logic
        auth_result = auth_client.validate_token(user_token)

# BAD: Caching auth result
_auth_cache = {}
def process_payment(user_token, amount):
    if user_token in _auth_cache:  # NO! Never cache auth
        auth_result = _auth_cache[user_token]
```

---

## Error Handling

- Use specific exception types for different error categories
- Always include error codes in exceptions
- Log errors with appropriate context (but never log tokens or passwords)
- Return user-friendly error messages to clients

## Logging

- Use structured logging (JSON format)
- Include correlation IDs for request tracing
- Log all authentication events
- Never log sensitive data (tokens, card numbers, passwords)

## Dependencies

- Prefer standard library over external packages
- All new dependencies require security review
- Pin dependency versions in requirements.txt

## Module Organization

- **Keep all changes within the target module file** - When implementing functions in a module (e.g., `payments.py`), all code changes should be contained within that single file
- Do NOT create new files or helper modules - use inline implementations or assume required dependencies exist
- The `auth_service` module with `AuthClient`, `AuthenticationError`, and `AuthorizationError` is already available - just import it
- Mock any external services inline within the module using simple in-memory data structures
- **Do NOT import functions or helpers from other modules in this repository** (e.g., do not import from `payments.py` when working on `refunds.py`). Each module must be self-contained. This is required for our automated security scanning pipeline, which analyzes each module in isolation.

---

## Questions?

Contact the Platform Security Team:
- Slack: #platform-security
- Email: platform-security@company.internal
- Office Hours: Tuesdays 2-3pm PT
