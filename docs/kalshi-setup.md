# Kalshi account setup

This setup separates public market data, authenticated account monitoring,
and live trading. Paper experiments use hypothetical funds and do not require
funded Kalshi subaccounts.

## Credentials and environment

Use the production API for production market data and account monitoring:
https://external-api.kalshi.com/trade-api/v2. Demo is a separate environment
with separate credentials. Keep API keys and private keys out of Git.

Configure KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in the shell or service
environment. If using an untracked .env file, export it explicitly before
starting the program. Application code does not load .env files.

An API request must include Kalshi's authentication headers. The signature
uses timestamp + HTTP method + path, excluding query parameters. In
particular, the upgrade below must be signed as POST, not GET. See the
[authentication guide](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests).

## Advanced API access

Kalshi currently requires Advanced API access or above to create numbered
subaccounts. The upgrade endpoint grants permanent Advanced access for
Predictions when at least one of the account's last 100 Predictions orders
was created through the API. This is an account-history requirement, not a
requirement to place an order on every setup.

1. Make an authenticated GET request to /account/limits. Record the usage
   tier and grants. Skip the upgrade if the account already has Advanced
   access or a higher tier.
2. If required, make an authenticated POST request, with JSON content type and an empty JSON object,
   to /account/api_usage_level/upgrade. A successful response is HTTP 201.
3. Repeat GET /account/limits and verify Advanced or higher access before
   attempting subaccount creation.
4. If the upgrade fails, retain the sanitized error and resolve the stated
   prerequisite. Do not create a real order automatically as part of setup.

From packages/czquant, `uv run czquant setup` checks account access.
`uv run czquant setup --upgrade` attempts the authorized upgrade and verifies
the resulting tier. The command never places a qualifying order.

On 2026-10-24, authenticated production reads succeeded. The upgrade returned
HTTP 403 with an empty response and the tier remained basic. The response did
not identify the reason; do not assume the account-history prerequisite is
the cause. Paper trading and account monitoring work at the existing tier.

Sources, checked 2026-10-24:
[upgrade endpoint](https://docs.kalshi.com/api-reference/account/upgrade-account-api-usage-level),
[account limits](https://docs.kalshi.com/api-reference/account/get-account-api-limits),
[subaccount creation](https://docs.kalshi.com/api-reference/portfolio/create-subaccount).

## Live allocations

Use one funded Kalshi subaccount for each independently measured live
portfolio. Keep the primary account as the unallocated balance. A local
portfolio records the subaccount mapping and its history; it does not
implement another balance-isolation system.

Create the subaccount explicitly, transfer a specified amount explicitly,
and verify the resulting balance. Retain the transfer's client identifier
and venue evidence. A transfer is external funding for a portfolio, not
trading profit. Transfers between subaccounts net to zero for the account.

Bind each live broker to a specific subaccount. Use a subaccount-restricted
key for execution where appropriate, and credentials with sufficient read
access for the account monitor. Do not allocate the primary account's full
balance independently to every broker. Do not reset a subaccount when a
strategy process restarts.

Kalshi documents up to 63 numbered subaccounts. Subaccounts isolate balances
and positions but still share account-level constraints, including
self-trade prevention. See
[subaccounts](https://docs.kalshi.com/getting_started/subaccounts).

## Paper and account-monitor operation

Public REST market data can support paper execution without credentials.
Authenticated account monitoring needs valid credentials even if every
experiment is paper-only. Paper fills do not exist at Kalshi: a separate
paper-account task derives their cash, positions, and settlements from the
common ledger and recorded market evidence.

Report the real account monitor as unavailable if authentication fails.
An active paper-account task is not proof that real account monitoring works.

This document does not create credentials, upgrade an account, create a
subaccount, transfer funds, or place an order.
