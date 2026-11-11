# Threat and Failure Note

In my assumed production model, this service is middleware between the HRIS and the Entra ID tenant, running as a scheduled Azure Function App. It reads worker records from the HRIS, creates and updates identities through Microsoft Graph, and writes the confirmed email address back to the HRIS. This note covers how it handles credentials, permissions, destructive actions, replays, monitoring and recovery.

### Credentials

- The service uses the Function App's managed identity, so there is no Graph secret at all. Locally this would be `DefaultAzureCredential`.
- The client id and secret are held in Azure Key Vault and read with the managed identity, through a Key Vault reference in the Function App settings. Nothing is stored in code or config. If the HRIS supports certificate or signed-JWT client authentication, I'd use that instead of a shared secret.
- Tokens are kept in memory until shortly before they expire, then requested again. They are never written to disk or logged. `audit()` only accepts a fixed set of fields (`employee_id`, `object_id`, `phase`, `reason` and so on), with no parameter for a token, secret or whole record, so one can't end up in a log by accident.
- All sample workers are fictional.
- `.env` is in gitignore. Would only be used for local development.
- The HRIS secret is rotated in Key Vault. The Function app will pick up without any code change

### Least privilege

The service needs two sets of permissions: Graph for Entra and Exchange, and OAuth scopes for the HRIS API. Each is limited to what the service actually does. Nothing has delete permission anywhere.

#### HRIS OAuth

The mock HRIS currently accepts calls without a token, so OAuth isn't implemented in the code. This is the intended design. I've assumed the HRIS uses **OAuth 2.0 client credentials**, which is standard for service-to-service access. The usual developer option is to create an Enterprise App in the HRIS, name it, and select the scopes it's allowed.

To authenticate, the service needs:
- the Application (client) ID
- the client secret from the HRIS Enterprise App/Developer section placed into the Azure Key Vault
- the scope it's asking for, which must be one of the scopes granted to the app in the HRIS
- Authentication endpoint to POST to to recieve token

I've assumed granular scopes: Structured as such Intent.Table.Field/All. For example `Read.Employees.All`, `Read.Employees.Email`, `Write.Employees.All` and `Write.Employees.Email`. The service is granted only two:

| Scope | Used for | Why only this |
|---|---|---|
| `Read.Employees.All` | Reading the worker feed, and reading a worker just before the write-back | The service needs every field it decides on. It never needs to write them |
| `Write.Employees.Email` | The write-back of the confirmed address | The only field the service ever writes. It can't change a name, status or date |

`Write.Employees.All` is deliberately not granted. Even if the service had a bug, it couldn't change anything in the HRIS except the email field.

How the scopes are used:
- The Enterprise App in HRIS is granted both scopes for simplicity. So one token will work for both actions
- A short expiry. I've assumed 15 minutes. Tokens are cached until about a minute before expiry. Client credentials have no refresh token, so the service simply requests a new one.
- A rejected token (401/403) is classed as an `AuthError`. During the write-back it's retried on the next run, up to 5 runs, then the joiner moves to `WritebackFailed`, so an expired or revoked token can't loop forever.
- The HRIS side also enforces it. Even with a valid write token, the service reads the worker first and only writes into an empty email field. A 409 from the HRIS is treated as a second safeguard.

Note: the write-back is a PATCH in principle, but many HRIS APIs expose updates as POSTs. The scope, not the HTTP verb, is what limits it.

#### Microsoft Graph

These are application permissions, granted to the managed identity:

| Permission | Used for |
|---|---|
| `User.ReadWrite.All` | Creating users, updating department and manager, enabling and disabling accounts, and reading `proxyAddresses` and `provisionedPlans` |
| `LicenseAssignment.ReadWrite.All` | Assigning the Exchange Online licence that triggers the mailbox |

- No `Directory.ReadWrite.All`, no `User.DeleteRestore.All`, no mail or mailbox permissions. The service never reads anyone's mail, only the address.
- If the tenant offers `User.EnableDisableAccount.All`, the disable step could use that narrower permission.
- `accountEnabled` is a sensitive property, and Entra restricts changing it on privileged admin accounts. That backs up the service's own rule of never auto-disabling a privileged account.

### Destructive actions

- Dry run by default. Nothing changes unless `--apply` is passed.
- A leaver's account is disabled, so it can be restored.
- Privileged accounts are never disabled by service. They go to ManualReview.
- Bulk-disable limit. If more than 5 disables are due in one run all of them are held.
- Only accounts this service created are re-enabled. An account someone else disabled goes to ManualReview.
- Doubtful data never grants access. An invalid newer record cancels a pending enable but keeps a pending disable.
- An HRIS email is never overwritten. The service reads first and only writes into an empty field. A different address is a `Conflict` for a person to resolve.
- A blocked leaver keeps access until a person acts. The only reminder is that run's alert and the ticket it raises.

### Replay

- Identical records are skipped. Each successfully handled record's hash is stored in `processed_inputs`.
- Problem records aren't remembered. ManualReview and Failed records are re-evaluated if HRIS sends them again as the new record could be a remidiation to this error.
- The write-back is idempotent. If HRIS already holds the same address, the result is NoChange.
- A lost create response never makes a second account. The write-ahead `actions` log records the create before it's attempted. A retry hits the UPN conflict, and the next run adopts the existing account by `employeeId` instead of creating another.

### Monitoring

- One JSON line per decision, written to stdout and the `audit_log` table, with the run's correlation id, employee id, Entra object id, phase, action, reason and result. In production these go to Application Insights.
- A run summary lists everything under *Needs attention*, with the employee id and reason.
- Exit code 2 means something needs a person. In production, an Application Insights alert on that result raises a ticket.
- `hris-sync status` shows where every joiner is up to, including the confirmed address and the last error.
- Flags are raised once. A later run doesn't repeat a manual review, so the ticket is the reminder. Work the service still holds (a scheduled change it couldn't apply, a write-back being retried) is flagged on every run until it's resolved.
- No personal data beyond the ids is logged. Names, dates and whole records never reach the log.

### Recovery

- State lives outside the run (`sync_state`, `scheduled_changes`), so the next run carries on where the last one stopped.
- The write-ahead `actions` log shows what was attempted. An account whose create succeeded but whose response was lost is adopted, not recreated.
- Only the failed stage is retried. A failed write-back retries the write-back and never recreates an identity.
- Transient errors (429, 503, timeouts) get up to 4 attempts per call, with exponential back-off, jitter and `Retry-After`.
- Permanent errors go to a person straight away, because retrying won't help.
- `MailboxTimedOut` (no mailbox after 24 hours), `WritebackFailed` (write-back gave up) and `ManualReview`. These are fixed by a person.
- The failure-injection tests in `tests/test_sync.py` make calls fail before they take effect, or succeed with the response lost, over 8 seeds. They check that no account is duplicated, no HRIS email is overwritten, no action is left Pending, every failure is audited, and repeated runs end in the same state as a run with no failures. A 100-seed run was also done by hand.
