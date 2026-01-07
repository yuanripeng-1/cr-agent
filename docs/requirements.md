# User Loyalty & Transaction System PRD

## 1. Overview
This system handles user transactions and awards loyalty points based on business rules.

## 2. Functional Requirements
- **FR1: Transaction Processing**: Users can transfer funds. Each transaction must be logged.
- **FR2: Loyalty Points**: For every $10 spent, award 1 point. Points should be calculated using the `PointEngine`.
- **FR3: Batch Export**: Admin can export all transactions for a specific user.
- **FR4: Security**: All database queries MUST use parameterized inputs to prevent SQL injection. JWT tokens must be verified for every request.

## 3. Performance SLAs
- **PS1**: Point calculation for a single user must complete within 50ms.
- **PS2**: Batch exports must not block the main thread.

## 5. Payment Gateway (New)
- **FR5: Tax Calculation**: Sales tax is 8% for domestic orders and 0% for international. Round to 2 decimal places.
- **FR6: Security**: API keys must be loaded from environment variables. Never log full credit card numbers or secrets.
- **FR7: Webhooks**: Verify signatures for all incoming webhooks from the payment provider.
- **PS3**: Payment processing must handle timeout gracefully if the provider is slow.
