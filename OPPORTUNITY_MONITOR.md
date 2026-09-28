# Biotech Opportunity Monitor

A zero-AI companion to the existing ATS internship monitor. It discovers openings
outside the original company list, ranks them for commercial strategy and adjacent early-career functions, and uses the existing email-and-tracker connection. It does not apply
for jobs or contact employers.

## What it looks for

- US internships, entry-level full-time positions, and rotational/development programs.
- Commercial and commercial strategy first; marketing, market access, business
  development, product strategy, operations, finance, and appropriate science roles follow.
- California is highlighted; other US locations remain eligible for alerts.
- Promising uncertain roles remain visible with specific eligibility cautions.
- Employer-disclosed pay appears when available. Salary, eligibility, and impact are
  never invented. “Fit” is an explainable rules score, not LinkedIn's badge or a hiring prediction.

## Sources and timing

The companion searches ten public LinkedIn queries without signing in and reads
sixteen additional employer inventories through Greenhouse, Lever, and Ashby.
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

Delivery is at-least-once: a crash or timeout after the email was sent but before
the acknowledgement was saved can cause a repeat. Stable event IDs are included,
but exact-once delivery requires the existing webhook to enforce idempotency.
The old ATS monitor has a separate ledger, so overlap between the two monitors is
possible. Distinct requisitions are retained even when their titles are identical.

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
python opportunity_monitor.py --dry-run
python opportunity_monitor.py --audit
```

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
