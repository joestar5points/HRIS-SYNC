# Sample packs

Four small HRIS deliveries and one run with no delivery that, in order, show every main behaviour of the service. The outcome of every record is written below **before** you run anything, and `tests/test_samples.py` checks that the real output matches this page.


## Base state (after `hris-sync seed`)

The mocked Entra tenant already holds these accounts (defined in `src/hris_sync/mocks/fixtures.py`):

| employeeId | Who | Why it's there |
|---|---|---|
| E1000 | Sam Okafor, Executive | Top of the tree. Never in any input, which proves that missing from a file never means disabled |
| E1008 | Morgan Hale, Quality | The manager most joiners report to |
| E5001 | Asha Patel, Finance | A *different* Asha Patel who already has `asha.patel@`. Joiner E1042 must get `asha.patel2@` |
| E1051 | Ivo Novak | Staged joiner created disabled by an earlier run; start date now reached |
| E2001, E2002 | Jo Mills, Kai Lund | Workers about to move department / manager |
| E3001, E3002 | Lena Ross, Max Ortiz | Workers about to leave |
| E3003 | Nia Shaw, IT | Leaver who holds an **admin role** |
| E3006 | Yara Ali | Will leave on 27 November |
| E4001 (×2) | Quinn Doyle | **Two** accounts share one employeeId |
| E9001 | Theo Grant | Created by a run that crashed before saving its state |

## Samples

Run them in this order, one after another, on a freshly seeded tenant.

### 1. `01-joiners.json`: new starters (6 Nov)

```
hris-sync seed
hris-sync sync --input samples/01-joiners.json --today 2026-11-06            # dry run: shows the plan, changes nothing
hris-sync sync --input samples/01-joiners.json --today 2026-11-06 --apply
```

| employeeId | Outcome | Shows |
|---|---|---|
| E1042 | Create `asha.patel2@`; mailbox **pending**, no write-back yet | UPN clash; matching by employeeId, never by name; no write-back before the mailbox is ready |
| E1043 | Create; write back **`b.carter@`** | The address is read from Exchange, not assumed from the UPN (`ben.carter@`) |
| E1045 | Create; write-back **ManualReview (Conflict)** | HRIS already holds a different email, which is never overwritten |
| E1050 | Create **disabled**; enable scheduled for 28 Nov; email written back now | Staged joiner: address ready before day one |
| E1051 | Update: enable | Staged joiner from an earlier run; start date reached |
| E9001 | NoChange; existing account **adopted**, email written back | Crash recovery: no second account |

Exit code **2**: E1045 needs a person.

### 2. `02-movers-and-leavers.json` (6 Nov, the next hourly run)

```
hris-sync sync --input samples/02-movers-and-leavers.json --today 2026-11-06 --apply
```

| employeeId | Outcome | Shows |
|---|---|---|
| E2001 | Update department | Only the changed attribute is touched |
| E2002 | Update manager | |
| E3001 | Disable | Leaver by status. Disabled, never deleted |
| E3002 | Disable | Leaver by end date (status still says Active) |
| E3003 | **ManualReview** | Admin accounts are never disabled automatically |
| E3006 | NoChange; disable scheduled for 27 Nov | Future leaver |
| E1042 | *(not in this file)* email written back | The mailbox check works from saved state, not from the input |

Exit code **2**: E3003 needs a person.

### 3. `03-bad-data.json` (8 Nov)

```
hris-sync sync --input samples/03-bad-data.json --today 2026-11-08 --apply
```

| employeeId | Outcome | Why |
|---|---|---|
| E4001 | ManualReview | Two Entra accounts share this employeeId; the service never guesses |
| E8001 (×2) | ManualReview | Same employeeId twice in one file, with different data |
| *(missing)* | ManualReview | No employeeId |
| E7002 | ManualReview | Status `Retired` isn't a recognised status |
| E7003 | ManualReview | `startDate` is `30/10/2026`, not `YYYY-MM-DD` |

Nothing is changed in Entra. Exit code **2**, and every record is listed in the alert.

### 4. `04-corrections.json` (17 Nov)

```
hris-sync sync --input samples/04-corrections.json --today 2026-11-17 --apply
```

| employeeId | Outcome | Shows |
|---|---|---|
| E7003 | Create; email written back | HRIS fixed the date, which resolves the earlier flag |
| E1050 | NoChange; enable **rescheduled** to 22 Nov | A moved start date replaces the schedule |
| E1043 | Disable | A joiner this service created later leaves |
| E2001 | Update department again | A changed record is processed, never skipped as a duplicate |

Exit code **0**.

### 5. No delivery (28 Nov)

HRIS sends nothing at all, so there's no `--input`.

```
hris-sync sync --today 2026-11-28 --apply
hris-sync status
```

| employeeId | Outcome | Shows |
|---|---|---|
| E1050 | Enabled (start date 22 Nov reached) | Scheduled changes happen without HRIS sending the record again |
| E3006 | Disabled (end date 27 Nov reached) | |

Exit code **0**. `status` shows every joiner `WrittenBack`, except E1045 (`ManualReview`).

## Exit codes

- `0`: nothing needs attention.
- `1`: the input file couldn't be used; nothing was processed.
- `2`: the run finished, and at least one item needs a person. They are listed under *Needs attention*.
