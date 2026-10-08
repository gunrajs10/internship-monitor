# Biotech Opportunity Monitor

A zero-AI companion to the existing ATS internship monitor. It discovers openings
outside the original company list, ranks them for commercial strategy and adjacent early-career functions, and uses the existing email-and-tracker connection. It does not apply
for jobs or contact employers.

## What it looks for

- US internships, entry-level full-time positions, and rotational/development programs.
- Commercial and commercial strategy first; marketing, market access, business
  development, product strategy, operations, finance, and appropriate science roles follow.
- California roles rank first. Outside California, only confirmed US roles from
  the existing `companies.yaml` watchlist or the configured major-employer list
  qualify. Unknown locations are excluded. All role and experience checks still apply.
- Promising uncertain roles remain visible with specific eligibility cautions.
- Employer-disclosed pay appears when available. Salary, eligibility, and impact are
  never invented. “Fit” is an explainable rules score, not LinkedIn's badge or a hiring prediction.

## Sources and timing

The companion searches thirteen public LinkedIn queries without signing in and reads
sixteen additional employer inventories through Greenhouse, Lever, and Ashby.
Ten LinkedIn queries focus on California; three national queries retain approved
employer exceptions. The major-employer list is a fixed October 7, 2026 snapshot
of the first fifty companies in the linked biotech market-cap ranking, combined
with the existing watchlist and explicit employer-name aliases. It is not an
automatic ranking update or a claim that every listed company is pure biotech.
The original ATS monitor continues independently. Current source definitions live
in `opportunity_config.json`. The active Windows deployment saves source health,
pending candidates, and delivery receipts in `windows-monitor/state/opportunity.json`.

The active deployment uses a Windows task named **Biotech Opportunity Monitor**
every two hours. Keep the PC plugged in, online, and signed in; a locked screen is
fine. The task requests wake-up and catches up when Windows becomes available.
There is no ongoing service charge or AI usage. No Premium account or LinkedIn
cookie is required.

The included GitHub workflow is an alternative deployment. Its manual production
run succeeded, but automatic events did not arrive during setup, so that companion
workflow is disabled. GitHub schedules can be delayed or skipped. Use only one
active companion deployment and migrate the latest delivery ledger before switching.

Public LinkedIn search is undocumented and can be blocked or incomplete. The
monitor stops LinkedIn requests when access is denied, keeps pending candidates,
and continues employer feeds. It never bypasses a login wall or challenge.
Search pagination limits and outages appear as degraded coverage, not zero jobs.
It cannot promise every LinkedIn posting or recover a job that disappears before
any source exposes it to a successful scan.

## Delivery and recovery

Companion results go to a dedicated **LinkedIn Opportunity Monitor** Google Sheet,
including the additional employer-feed results. The original ATS monitor keeps
its own sheet. The receiver's private `OPPORTUNITY_SPREADSHEET_ID` script property
selects the companion destination; a missing property fails closed.
Historical results retain their original status, priority, notes, and eligibility
text. The tighter geography rules apply to new alerts and the undelivered queue.

Discovered candidates are queued before detail checks. Jobs beyond a processing
or email limit stay pending. A candidate becomes delivered only after the existing
webhook acknowledges success. A failed send preserves the batch for another run.

The email includes priority, California status, match reasons, posted pay when
available, and unresolved requirements. Initial runs can send several batches of
existing openings; later runs send new matches. No new-match email is sent on a
quiet run. Persistent source problems are rate-limited to one identical alert per
day. Other local execution failures also attempt a sanitized email, limited to one
attempt per day. Windows task status and the local log remain available if email,
network, the runtime, or the PC itself is unavailable.

The deployed email receiver keeps durable batch receipts in a hidden tracker tab.
If an acknowledgement is lost, a retry of a completed batch returns its receipt
without sending another email. A batch interrupted inside the receiver is held
for review rather than resent: email and spreadsheet writes are not one atomic
transaction, so absolute exactly-once delivery is not promised.
Companion tracker writes append only URLs that are not already logged. A tracker
write error does not block the job email. A held batch is released only after
reconciling its receipt, tracker rows, and mailbox to establish that no email was sent.
The old ATS monitor has a separate ledger, so overlap between the two monitors is
possible. Distinct requisitions are retained even when their titles are identical.

`monitor_email_cleanup.gs` archives these monitor's self-sent `[Internships]`
notification threads once daily, approximately 6–7 p.m. Pacific, when every
message in the thread is older than 24 hours. It labels them
`Internship Monitor/Archived` and removes them from the inbox. It never trashes
mail and preserves unrelated messages and threads with newer replies. The jobs
remain in their tracker sheets. Run `cleanupDryRun` to preview, `installCleanupTrigger`
to install/replace only this cleanup trigger, and `cleanupOldMonitorEmails` to run now.

## Operation

Use Windows Task Scheduler to run, pause, or re-enable **Biotech Opportunity Monitor**.
The local `windows-monitor/README.md` explains the setup and controls. The latest
preview is `outputs/current-opportunities.html`; completion status is saved in
`windows-monitor/last-run.json`.

For the optional GitHub deployment, its manual run modes are:

- `dry-run`: live discovery and a readable preview; no emails or delivery-state changes.
- `audit`: source coverage check only.
- `test-email`: clearly labeled delivery test through the existing email connection.
- `normal`: discover, rank, send new jobs, and save the queue and receipts.

Windows stores the existing email endpoint encrypted for the signed-in user and
passes it only to the monitor process. The optional GitHub workflow uses the
existing `WEBHOOK_URL` secret. Never paste the endpoint into source files, logs,
chat, or documentation. Deferred candidates are saved locally before delivery.

For local verification with Python 3.12 or newer (standard library only):

```text
python -m unittest discover -p "test_opportunity_*.py" -v
node test_opportunity_receiver.cjs
node test_monitor_email_cleanup.cjs
python opportunity_monitor.py --dry-run
python opportunity_monitor.py --audit
```

`opportunity_receiver.gs` is the receipt wrapper added to the existing Apps Script
receiver. Its original `doPost` is renamed `handleMonitorPost_`. In that handler's
spreadsheet selection, companion event IDs (`opportunity-` or
`windows-opportunity-`) must use `opportunitySpreadsheet_()`; other events use
`SpreadsheetApp.getActiveSpreadsheet()`. Set the private script property before
deploying so both job rows and companion failure logs use the dedicated sheet.
Add `monitor_email_cleanup.gs` in place of the old cleanup routines and install
the daily trigger once under the recipient account. In the handler's
`new_roles` branch, companion event IDs use `opportunityWriteRows_(ss, payload)`
inside a try/catch that logs tracker errors and continues to the existing email
send. Other events retain the original tracker-write branch. The helper uses the
existing `TAB_PROGRAMS`, `TAB_EARLY`, `getSheet`, and `fmtDate` definitions.
The wrapper handles companion event IDs and passes
other requests through. An existing deployment must be updated to a new version
after adding it. The deployed receiver's address and recipient configuration are
private and are not included in this repository.

Do not delete or reset the active `opportunity.json` ledger: it prevents repeat
alerts and preserves pending candidates. Disable the Windows scheduled task to
pause this companion. The original ATS monitor remains a separate workflow.

## Reading results

“Potential fit” means worth reviewing. Graduation date, program timing, particular
required skills, and availability must be checked on the employer posting. The
monitor does not infer work authorization, citizenship, or clearance.
Unknown pay does not mean low pay. Public posting timestamps may be missing; the
monitor distinguishes first-seen time from a supplied publication date.

## References

- [GitHub-hosted runner pricing](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [GitHub schedule limitations](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows)
- [Greenhouse Job Board API](https://developers.greenhouse.io/job-board.html)
- [Lever Postings API](https://github.com/lever/postings-api)
- [Ashby Public Job Posting API](https://developers.ashbyhq.com/docs/public-job-posting-api)
- [Biotech companies ranked by market capitalization](https://companiesmarketcap.com/biotech/largest-companies-by-market-cap/)
