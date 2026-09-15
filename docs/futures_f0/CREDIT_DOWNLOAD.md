# Historical download credit gate

The continuation authorizes at most USD 10 of authentic vendor list-price quotes,
also bounded by actual usable historical credits and the remaining current-month
limit. Cash authorization is USD 0. This changes no frozen trading rule. It does
not buy or change any service, data plan, monthly limit, or account setting.

Use one `BudgetGate` and one data directory for the entire authorized continuation:

```python
from src.futures_f0.budget import BudgetGate
from src.futures_f0.data import MetadataClient

gate = BudgetGate(config.data_dir, evidence_manifest,
                  deadline="2026-09-16T00:43:08.002893+00:00")
client = MetadataClient(config, guard=resource_guard)
receipt = client.download_with_credit(request, gate, exact_contracts_verified=True)
```

The existing `download_zero_quote` interface remains zero-only by default. Its
optional `budget=gate` delegates to the same credit implementation and ignores
caller-provided estimates. A positive quote is never relabelled as zero. Exact
contract identity qualification remains a separate prerequisite; this module
does not turn a caller's identity assertion into qualified contract definitions.

## Evidence captured before a charged endpoint

The manifest format is `futures-f0-databento-visible-dom-v1`. Each source points to
an archived visible DOM text or an official policy excerpt, with its source URL,
capture time, and SHA-256. Only HTTPS `databento.com` account portal pages and
official `/docs/` policy pages are accepted. Account captures must share the
actual browser session identifier and link to a visible unique account ID from
the account/API-key page. If another page visibly identifies an account, its
account selector must match that ID. Do not infer identity from initials.
The manifest also requires `capture_provenance: {path, sha256}` pointing to the
collector's original JSON record. That record supplies `browser_session_id`,
`captured_at`, `account_source` (the file name), and `sources` entries containing
`file` and `url`. The gate checks those associations and the record's hash.

Selectors contain a regular expression with exactly one nonempty named `value`
capture. They select the archived text; they cannot supply substitute amounts,
scopes, dates, or `verified` flags. Missing and ambiguous selections block the
download. Amounts use the source's explicit decimal separator, so `$125,00` is
USD 125.00 when `decimal_separator` is `,`. Negative, Boolean, NaN and infinite
quotes are rejected. A missing use amount is never treated as zero.

Required selected fields:

| Field | Required source fact |
| --- | --- |
| `api_key_masked` or `api_key_dom_sha256` | Visible API key on the linked account's key page, matched in memory to the configured key |
| `credit_balance_usd` | Amount remaining in the applicable historical credit entry |
| `credit_expires_at` | That entry's explicit expiration |
| `credit_expiry_summary` (optional) | A second displayed expiration; the earlier date is used and disagreement is recorded |
| `credit_scope` | Applicable service literally `Historical` |
| `usage_based_access` | Actual account page value `Enabled` |
| `billing_month` | Current `YYYY-MM`, or visible `Current billing cycle` tied to the source's current-month capture |
| `monthly_usage_usd` | Actual current-cycle total usage in USD |
| `monthly_limit_usd` | Actual enabled current-month limit in USD |
| `credit_application_terms` | Official text stating credits are automatically applied before charges |

The supported account case is an applicable historical credit entry, with a
balance and expiration tied to that entry. Do not select an aggregate containing
other service credits or combine the amount from one entry with another entry's
expiration. The request itself must be `GLBX.MDP3` and one of `definition`,
`ohlcv-1h`, `ohlcv-1m`, `statistics`, or `status`. The receipt records the actual
service/dataset/schema relationship. This does not establish unrelated live-data
or subscription rights.

For a masked key, at least eight actual key characters after `db-` must match;
an optional visible suffix must match too. If the page reveals a complete key,
the capture must hash the **DOM-extracted key** in memory, redact all key
occurrences, and preserve these provenance lines in the archived text:

```text
DOM key SHA256: <64 hexadecimal characters computed from the DOM key>
Original DOM SHA256 (not retained): <SHA-256 of the original visible DOM>
```

A fingerprint merely asserted from the configuration is not accepted. The gate
internally compares the DOM fingerprint with the configured key. It records
only a salted account/key binding in the journal, and does not emit the key or
its visible prefix in receipts. Preserve the browser capture provenance, source
hashes, and redaction details alongside the archive. Do not retain the original
unredacted key page.

Minimal manifest topology (placeholders deliberately do not pass validation):

```json
{
  "format": "futures-f0-databento-visible-dom-v1",
  "browser_session_id": "<actual capture session>",
  "account_source": "keys",
  "account_pattern": "User ID: (?P<value>[^\\n]+)",
  "capture_provenance": {
    "path": "browser_capture_provenance.json",
    "sha256": "<actual provenance-file SHA-256>"
  },
  "sources": {
    "keys": {
      "path": "keys_redacted.txt",
      "sha256": "<actual file SHA-256>",
      "source_url": "https://databento.com/portal/keys",
      "captured_at": "<actual UTC capture time>",
      "scope": "account",
      "browser_session_id": "<actual capture session>"
    }
  },
  "fields": {
    "api_key_dom_sha256": {
      "source": "keys",
      "pattern": "DOM key SHA256: (?P<value>[0-9a-f]{64})"
    }
  }
}
```

The actual collection must include all required sources and fields above. Sources
must be no more than one hour old, not from the future, and credit must remain
valid through the authorized deadline. Dates without a time are interpreted as
the start of that UTC day, conservatively. A manifest and its source hashes are
pinned on the first reservation; changing either blocks subsequent requests.
Refreshing stale evidence requires an explicit reconciliation preserving the
same journal and all reservations. There is no automatic reset or refund API.

## Reservation and failure accounting

For each uncached request, `MetadataClient` itself calls authenticated free
metadata again. The quote must be at most 60 seconds old. Before sending, the
gate saves the complete real estimate under `credit_budget/quotes/`, then
appends and flushes a hash-chained `RESERVE` record under a process file lock.
The request is blocked if total reservations exceed any of:

* USD 10 for this run;
* the historical-credit balance at the pinned account snapshot;
* `min(USD 10, actual monthly limit) - actual current-month usage`.

All reservations count, including completed requests, interrupted downloads,
timeouts, HTTP errors, and requests whose billing outcome is unknown. A process
crash after reservation but before a response still occupies the full list
price. The same request cannot be reserved again. A corrupt, truncated, missing
previously used journal, stale quote, changed evidence, wrong account/key, or
expired authorization blocks the send. No code retries a charged endpoint.

Immediately before the send, the gate checks source hashes, the saved quote,
reservation state and deadline again. The HTTP attempt creates a `.partial`
marker before opening the connection. Completed cache reuse checks the request
identity and content hash before doing any metadata or billing work. Incomplete
cache entries require review and cannot trigger an automatic rebill.

`quoted_cost_usd` and `reserved_list_price_usd` preserve the authentic list-price
estimate. `cash_payment_usd` and `credit_debit_usd` remain `null`, and billing
reconciliation stays `NOT_INDEPENDENTLY_RECONCILED` until independently observed.
The application does not claim that an estimate is the final vendor invoice or
that a USD 0 cash authorization proves a posted USD 0 payment.

## Audit limits and verification

Archived text and hashes establish what the local browser capture retained and
whether it changed. They do not cryptographically authenticate a provider's
server response. The collection must preserve genuine visible content and
account/session association; editing content and recomputing hashes would
destroy the evidentiary basis. Independent account activity after the snapshot
is not observed by this local journal. The gate is deliberately conservative,
and unknown coverage or insufficient evidence stops acquisition.

Tests are fully offline, with synthetic evidence and caches confined to pytest
temporary directories. They cover list-price preservation, cache reuse, numeric
validation, credit/month/run caps, process locks, concurrent requests, account
and key binding, source changes, expiry conflicts, missing facts, interruption,
unknown charges, and journal corruption. Use the original AI-M1 interpreter:

```text
C:/Users/benhe/OneDrive/Documentos/GITHUB/premarket-radar-ai-m1/.venv/Scripts/python.exe -B -m pytest tests/test_futures_f0_budget.py tests/test_futures_f0_data.py -q
```
