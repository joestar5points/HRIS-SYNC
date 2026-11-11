# HRIS Synchronisation Service

The application is a mocked representation of a production environment using an HRIS system and an
Azure Tenant which uses Entra ID (formerly Azure AD) and Exchange Online.
It is a Synchronisation Service that runs against mocks of these systems, built from the available
Microsoft Graph API documentation. It mocks only the endpoints that would be required for a system like this to work.
The CLI is a stand-in for a scheduled job. The project is built as a Python package and can be
used like a CLI Application.

It mocks all the services using Models created using SQLAlchemy, a standard ORM when creating back-end services with Python.
The models use SQLAlchemy 2's typed `Mapped[]` columns, and the code is type-hinted throughout.

The service's own state could be moved away from SQLite by changing the connection string, but it would need to be rewired
to actually communicate with the real HRIS and Microsoft Graph API for Exchange and Entra.

## Setup
Requires Python 3.11+

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1        # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
hris-sync seed   # creates the db file for SQLite and seeds the database with a realistic starting state
```

## Input Schema 
.JSON file with the following schema. Statuses assumed to be Active/Terminated/Leaver

This is here in case you want to create your own samples/inputs. More examples in Samples

```json
[
  {
    "employeeId": "E1042",
    "status": "Active",
    "firstName": "Asha",
    "lastName": "Patel",
    "department": "Quality",
    "managerEmployeeId": "E1008",
    "startDate": "2026-11-02",
    "endDate": null,
    "email": null
  }
]
```

## Running using the CLI
After installing, `hris-sync --help` should work. If it doesn't, activate the venv first. Make sure you are doing the CLI commands in the Repo Root

The hris-sync CLI application runs like most standard CLI apps: it has a variety of functions and flags that can be passed.
These different functions and flags allow us to mimic the running of a real system over periods of time without having to wait for scheduled runs or for real time to pass.

### CLI reference

```powershell
hris-sync seed                                    # reset to the base mock tenant
hris-sync sync --input samples/01-joiners.json    # dry run: shows what would change
hris-sync sync --input samples/01-joiners.json --apply
hris-sync sync --apply                            # no new input: progress scheduled changes and mailboxes
hris-sync status                                  # where each joiner is up to
```

| Flag | Purpose |
|---|---|
| `--input FILE` | HRIS delivery (JSON array). Optional |
| `--apply` | Make changes. Without it, it's a dry run |
| `--today YYYY-MM-DD` | Pretend it's this date (also moves the mailbox deadline) |
| `--max-disables N` | Hold all disables if more than N are due (default 5) |
| `--failure-rate P`, `--lost-response-rate P`, `--random-seed N` | Demo only: make the mock APIs unreliable |
| `--db FILE` | Use your own database file (default `hris_sync.db`). Goes before the command |
| `--empty` | Only works with the seed command and just creates an empty db |

**Exit codes:** 0 ok · 1 unusable input · 2 needs attention

Each decision is printed to stdout as one JSON object per line (JSON Lines), followed by a readable summary. One line per entry is deliberate: it's the format log tools such as Application Insights ingest as one searchable event, whereas indented JSON would be split across many lines.

For a guided walkthrough with expected outcomes, see [`samples/README.md`](samples/README.md).

## Tests

```bash
pytest -q
```
NOTE: Can use a variety of PyTest flags according to preference I like -sv when debugging and -v when demoing

200+ tests run against the in-memory mocks. They're a mix of unit, mock, sync and failure tests:

- Decide tests (`test_decide.py`) check the identity decision for each case: create, update, disable, no change, or manual review (ambiguous match, privileged account, malformed record).
- Sync tests (`test_sync.py`) run the whole service over a seeded mock tenant, including dry runs, scheduled starts and leavers, the bulk-disable limit, mailbox timeouts, write-back conflicts and replayed input.
- Sample tests (`test_samples.py`) run each pack in `samples/` in order and check its documented outcome, so the walkthrough always matches the code.
- Failure tests make API calls fail, or succeed with the response lost. They use a fixed random seed, so every failure lands in the same place on every run and can be reproduced. The invariant tests repeat this over several seeds and check that no second account is ever created, no HRIS email is overwritten, and later runs end in the same state a clean run would.

## How it works
1. Loads an input file of worker records into the mock HRIS (optional: a run with no input skips to step 5).
2. Begins the identity pass. Each record is validated first; an invalid record goes to ManualReview.
3. Looks up each valid record in Entra by `employeeId` and decides what it needs: Create, Update (department, manager, enable), Disable, NoChange or ManualReview. Future start and end dates are scheduled for later.
4. Plans every record before acting, so a bulk disable can be stopped, then makes the changes.
5. Makes a scheduled pass, enabling staged joiners whose start date has arrived and disabling non-admin users on or past their end date.
6. Makes a mailbox pass, checking each pending mailbox once. When one is ready, the confirmed address is written back to HRIS, but only if the HRIS email is empty.
7. Prints every action to stdout and records it in the audit log, then a summary of anything needing attention. The exit code (0 ok, 1 unusable input, 2 needs attention) lets the proposed Azure Function App raise alerts so changes can be actioned.


## Failure injection

The mock APIs can be made to fail, to show how the service recovers:

```bash
hris-sync sync --input samples/01-joiners.json --today 2026-11-06 --apply --failure-rate 0.3 --lost-response-rate 0.2 --random-seed 1
```

- `--failure-rate` fails a call with a 429 or 503 before it takes effect, so nothing changed.
- `--lost-response-rate` lets a call take effect but loses the response, so the service can't tell whether it worked.
- `--random-seed` makes the failures land in the same places every time.

Invariant tests run this over several seeds. They check that no account is ever duplicated, no HRIS email is overwritten, no action is left Pending, every failure is
audited, and repeated runs end in the same state as a run with no failures.

This found three bugs, all now fixed:
- adopting an existing account crashed on a transient error
- a create whose response was lost was never recovered (fixed by trusting the write-ahead actions log)
- the HRIS write-back relied on the API to refuse an overwrite, instead of reading first

## Design decisions

- Python and SQLAlchemy were chosen for familiarity. An ORM avoids hand-written SQL, and the typed models catch errors in the IDE. The models are portable: moving from SQLite to Azure SQL is a change of connection string.
- Input is a JSON file: how the data arrives isn't specified, so the service reads a file. In production that could be a queue listener or a drop location, without changing the sync logic.
- `decide()` is a pure function: it takes a record and what Entra holds, and returns a decision without calling any API. This keeps the rules easy to test.
- Every record is decided before anything changes, so a bulk disable can be spotted and stopped before it starts. The dry run uses the same plan.
- Identity, mailbox and write-back are tracked separately, so a failed write-back only retries the write-back and can never recreate an identity.
- Each change is recorded before it's made. If a response is lost, the next run knows what was attempted and remediates the account rather than creating a second one.
- Provisioning can take up to 24 hours, so the service doesn't wait. The schedule interval is the back-off.
- Mocks are abstractions of GraphAPI, not HTTP servers. The HRIS mock is my best guess at a typical HRIS API. A mock OAuth system was left out, as time was better spent on the decision logic.


## Assumptions

### Source data
- HRIS is the source of truth.
- The JSON input file is change by change, not a full snapshot. Each input file contains only new or changed records. Nothing depends on a record being sent again:
  - date-driven follow-ups (a staged joiner's start date, a future end date) are scheduled when the record is processed, then applied by a later run once they're due. This means that someone starting in the future is considered "Active" in HRIS but a real system might have "Pending"
  - The Status would be considered an Enum owned by HRIS but no other statuses apart from Active are provided in the examples. I created "Terminated" and "Leaver"
  - pending mailboxes and write-backs are tracked in the service's own state
  - a worker missing from a file is never disabled, even if a full snapshot is sent. Absence isn't proof that someone has left, and a truncated export would otherwise disable everyone it cut off. Only a `Leaver`/`Terminated` status or a passed `endDate` disables an account
- A record identical to one already handled successfully is skipped and logged as `duplicate input`. This covers a replayed file or a full snapshot. Records that went to ManualReview or Failed are never remembered, so if HRIS sends the same problem record again, it's flagged again.
- Manual reviews are raised once, in that run's alert. Each one is listed in the run summary with the employeeId and reason, the run exits with code 2, and the audit log keeps it under the run's correlation ID. A later run doesn't repeat the flag, and a newer record for that worker is treated as the new truth, which may itself be the fix. The exception is work the service still holds in its own state (a scheduled change that couldn't be applied, a write-back being retried), which is flagged every run until it's resolved. I chose not to store open manual reviews in their own table, to keep the service lightweight and avoid duplicating what HRIS, Entra and the audit log already hold. In production, the exit code 2 alert would raise a ticket to track the work.
- The input is an HRIS export. It's loaded as-is into the mock HRIS table, invalid records included. The sync service does the validation, so a bad record becomes a ManualReview entry rather than failing the load.
- `employeeId` is the only key used to match a worker across HRIS, Entra ID and Exchange. Names and email addresses are never used for matching. In the real system, it would use a User Object Reference to get the mailbox
- EmployeeIDs can be any 16 character input. There could be a regex match that is start with E[0-9]. I've just kept it within the confines of Entra
- The data doesnt come with a timestamp or a version therefore the application just takes the latest input as most recent for an EmployeeID

### Identity decisions
- `Active` means the worker should have an enabled identity. `Leaver` and `Terminated` mean the worker has left. Any other status value is treated as malformed and sent to ManualReview.
- Future start dates (staged joiner): a joiner with a future `startDate` is created straight away with the account disabled, and an enable is scheduled for the start date. This gives Exchange time to provision the mailbox, so the email can be written back before day one.
  - The scheduled enable is applied by whichever run first falls on or after that date, whether or not the record is in that run's file.
  - Only accounts this service created are ever re-enabled. An Active worker whose account was disabled by someone else (for example a suspension) goes to ManualReview.
- Future end dates: an Active worker with a future `endDate` gets a disable scheduled for that date. When it falls due, the same safety rules as other leavers apply.
- The latest record replaces the schedule.
  - A moved date reschedules.
  - A withdrawn joiner (sent as `Leaver`/`Terminated`) cancels the pending enable.
  - A newer record that fails validation cancels a pending enable, because access is never granted on doubtful data. A pending disable is kept, because removing access is still safe.
- If the manager's employeeId isn't found in Entra, the user is still created or updated without one, and the audit reason says so. An existing manager is never blanked out.
- A leaver is disabled, not deleted. A disable action only happens when all of these are true; otherwise the record goes to ManualReview:
  - the status is `Leaver` or `Terminated`, or the `endDate` is today or earlier
  - exactly one Entra identity matches the `employeeId`
  - the account is not already disabled and is not a privileged (admin) account
  - the number of disables in the run is under a configured limit, because a sudden spike is more likely a bad export than a real mass departure. Scheduled disables that fall due count towards the same limit.

### Mailbox and write-back
- In a real tenant, the mailbox is created by Exchange Online asynchronously after an Exchange licence is assigned. The mock simulates this delay.
- The service never chooses or assumes the mailbox address. The confirmed address is taken from the primary `SMTP:` entry in `proxyAddresses` once Exchange reports provisioning as successful, mirroring Microsoft Graph.
- Mailbox provisioning mechanism:
  - Each run checks a pending mailbox once, without waiting. The run never blocks on Exchange, and the schedule interval (for example hourly) is the back-off between checks.
  - Until the mailbox is ready, there is no HRIS write-back.
  - If no mailbox is ready 24 hours after the identity was created, the worker moves to a visible `MailboxTimedOut` state.
- Conflicting email: the service reads the worker from HRIS before writing, and only ever writes into an empty email field. The same address gives NoChange. A different address gives ManualReview (`Conflict`), with the address HRIS holds named in the alert, and nothing is written. The service does not rely on the HRIS API to refuse an overwrite. If the field is filled between our read and our write, the HRIS's own refusal (409) is assumed as a second safeguard; a production API should support a conditional write (ETag / `If-Match`) to close that gap.
- Write-back failures: a transient or authorisation failure is retried on the next run, up to 5 runs, then the joiner moves to `WritebackFailed`. A permanent rejection (unknown worker, address refused) moves to `WritebackFailed` straight away, because retrying won't help. Either way, the confirmed address is shown in the alert and in `hris-sync status`, so a person can enter it by hand.
- Tenant has unlimited Exchange licences. The Application would still handle a scenario where they did run out of licences the same as mailbox failure. It is worth mentioning this scenario is not built into the Mocks


### Running the service
- Dry run by default. Changes are only made when `--apply` is passed, so running the command without flags can never perform a destructive action.
- Each `sync` command is one run of the scheduled job. `--input` is that run's delivery from HRIS. Leave it out when HRIS sent nothing: the run still applies scheduled changes that are due and checks pending mailboxes.
- `--today` is the scheduler's clock. It sets the date HRIS start and end dates are judged against, and the time the 24-hour mailbox deadline is measured from (`--today` at the real time of day). So a demo that skips ahead a few days also sees a pending mailbox time out, as it would in real life. Two runs on the same `--today` count as hours apart on the same day. Audit timestamps always use the real clock. In production, `--today` isn't passed.

## Known limitations
- The exception states have no remediation. They are to be fixed by a human: MailboxTimedOut, WritebackFailed, ManualReview
- Not every possible error in these mocked systems is simulated. Injected failures are 429/503, before or after the call; timeouts and random 401/403s are not
- Entra drift isn't corrected until HRIS sends a change. If someone edits a user within Entra by hand, the department and manager are only put back when HRIS next sends that worker's record with a change
- There is a race condition in the service's read then write to HRIS. The HRIS's 409 is the second safeguard, but a real API would have an ETag for versioning
- Changes are applied in the order they arrive. The feed has no timestamp or version, so an older record arriving late is treated as the latest
- A manager who isn't in Entra yet is never set later. For example, if a joiner and their new manager arrive in the same file, the joiner is created without one, and the manager stays blank until HRIS sends a changed record
- Leavers are only disabled. The normal offboarding flow is not followed such as removing licenses and sharing a mailbox with manager or colleagues
- There could be more statuses in the real HRIS system
- The design assumes that there are no overlapping jobs
- The head of organisation would be set to ManualReview, because `managerEmployeeId` is required. A record that names itself as its own manager ends up Failed
- A change to First Name or Last Name is not applied or flagged. The UPN and email are left alone on purpose, as renaming them is best done with manual intervention
- There's no bulk limit on creates. Anyone who can write to the input location could create accounts. In production, the drop location would be writable only by the HRIS integration, and a create limit like the disable limit would be worth adding.
- Certain names can break the email cleanse command. It will flag as failed instead of ManualReview but still needs attention

## What I would change for production

- The App would be hosted as an Azure Function App on a scheduled timer. This keeps costs down as the Application is charged on an operation basis as this is considered "Serverless" compute
- The service's state would move from SQLite to Azure SQL serverless
- Implement optimisations to work for the Function App and not the CLI. Making sure it's compatible with the Python Runtimes
- Microsoft have a Python SDK designed for the Graph API simplifying OAuth authentication. Being hosted on an Azure Service you can use managed identities making the authentication easy to implement
- Grant the managed identity only the least-privilege Graph permissions it needs
- Create Reusable Deployment templates (Terraform or Bicep depending on what is already in use)
- Application Insights alerting on a needs-attention result (exit code 2 in the CLI) raising a ticket within the host's ticketing service
- Implementing a resolve command or even a resolve interface for the administrators working on the application
- Using Azure Key Vault to obtain HRIS secrets
- Create a HRIS OAuth Component as an internal shared package which could be utilised by multiple apps and prevents the applications from becoming monolithic and overscoped and can reuse this Authentication component in other applications
- A reconciliation job comparing Entra with HRIS to correct drift
- A version or timestamp in the HRIS feed, so older records can be ignored
- Conditional HRIS writes (ETag / `If-Match`) to close the read then write gap
- Function Apps cannot use pyproject for its build/dependencies process would have to generate a requirements file instead 


## AI Assistance Note

Claude was the only AI Tool used

- I used Claude to interpret the original document as I was spending a lot of time second guessing the outline due to vagueness and some logical fallacies within it i.e. having a write back but not specifying where the data comes from. Ambiguous statuses
- Claude created tests based on all the functions that I created. Using a Test Driven Development mindset to make sure that the application was robust. The TDD helped us find niche cases and limitations that could be work around. Writing the code along with the tests would've taken far too long for me to do alone.
- Claude created the Repeatable Fault injection decorators so we could simulate faults happening.
- It helped finalise the sequence diagrams while I tweaked them for simplicity and readability
- Claude explained argparser so I could write a robust CLI application 
- Claude created the fixtures and sample data in conjunction with the tests so it could cover all bases in its own tests and in the demo. I used this to avoid bias when coming up with sample data
- Claude help me make more pythonic decisions by using some features such as lambda functions and comprehensions to leverage python built-ins. These popped up while it was generating tests for my Classes and Functions

### Rejections 

- I ended up rejecting a lot of Claude's scope creep. It kept wanting me to mock more elements of the mocked services which I thought were going out of scope of the project.
- It also tried pivoting the application to use full snapshot as I believe the project was going out of the context window of the LLM.
- I rejected a large amount of diagrams as they ended up getting lost in technical detail.

I personally ran through the outline document and made sure the minimum test cases all passed independently of Claude using singular records it had generated and running input files record by record to keep it auditable for myself

## Resources
- [How the proxyAddresses attribute is populated in Microsoft Entra ID](https://learn.microsoft.com/en-us/troubleshoot/entra/entra-id/user-prov-sync/proxyaddresses-attribute-populate)
- [New-EmailAddressPolicy: in Exchange Online, email address policies apply only to Microsoft 365 Groups](https://learn.microsoft.com/powershell/module/exchange/New-EmailAddressPolicy)
- [Create user](https://learn.microsoft.com/graph/api/user-post-users)
- [Update user](https://learn.microsoft.com/graph/api/user-update)
- [Assign manager](https://learn.microsoft.com/graph/api/user-post-manager)
- [assignLicense](https://learn.microsoft.com/graph/api/user-assignlicense)
- [Microsoft Graph throttling and Retry-After](https://learn.microsoft.com/graph/throttling)