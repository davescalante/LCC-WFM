# LCC-WFM — Working Notes for Claude Code

Django 4.2 workforce-management app for a legal-intake call center. Four apps: `scheduling`,
`adherence`, `finance`, `erlang`. SQLite locally, Postgres on Render. Server-rendered Django
templates with vanilla JS and small AJAX endpoints. No SPA, no build step.

Fuller detail lives in `SYSTEM-SUMMARY.md` and `HANDOFF.md`. **The live code wins over both
documents** whenever they disagree — the app changes faster than the docs.

## Before planning anything

- Pull the latest `main` first. Two people build in this repo independently, and a stale
  checkout will silently miss the other person's work.
- **Ignore `.claude/worktrees/` completely.** It contains a parallel checkout of
  `finance/views.py` and `adherence/views.py` with older line numbers. Citing it produces
  wrong file references.
- `db.sqlite3` is committed intentionally for local dev. It tells you nothing about
  production data.

## Tests

`python3 manage.py test` — the full suite must pass before any commit. Report the pass count.
The tests are the regression gate and double as executable specs for the trickier rules
(NR caps, bonus eligibility, request approvals, export field gating).

## The rule that matters most: there are two separate hours pipelines

- `AdherenceRecord.actual_hours` is **display only**, written by the daily NR-deduction
  pipeline in `adherence/`.
- Money comes from `finance.views._get_billable_weekly_data`, which recomputes from raw
  `DailyAgentHours` login/not-ready seconds every single time.
- **Billing and payroll must never read `actual_hours`.** The two pipelines are independent
  by design. Do not "simplify" by merging them, however tempting it looks.
- Read-only reports and exports **reuse the engine** rather than recalculating. Never
  re-derive billing, payroll, bonus, or scheduled-hours math in a new place — a report that
  recalculates will eventually disagree with what the screen shows.

## Landmines

- **Pay-window predicate.** An agent counts toward a given week if `status='active'` OR
  they are inactive with a `finalized` separation whose `remove_from_adherence_date >
  week_start`. This predicate is duplicated across 8 call sites: `_get_adherence_agent_pks`
  in `adherence/`, the `agent_list` export in `scheduling/views.py`, and `billing_report`,
  `billing_export`, `billing_export_v2`, `payroll_report`, `payroll_export`, and
  `codings_export` in `finance/views.py`. Four of these (`billing_report`, `billing_export`,
  `payroll_report`, `payroll_export`) use an `.exclude()`-shaped form rather than the `Q()`
  form — the same rule in two different shapes, so a change to this predicate has to check
  both and can easily miss a site. Prefer additive changes; do not consolidate it into one
  helper unless that is explicitly the task.
- **Best-template resolution.** A specific-date `Shift` override always beats a
  `ShiftTemplate`; among templates covering the date, the latest `effective_from` wins
  (`None` treated as earliest). A shared helper, `_best_shift_template` in
  `scheduling/views.py`, already exists and is reused twice (including an import into
  `erlang/views.py`), but the same comparison logic is independently reimplemented in 5
  other places. Use `_best_shift_template` rather than adding a sixth reimplementation.
- **`is_admin_coding` and `is_official_admin` are a hard partition.** Regular
  Codings/Adherence queries exclude admin rows entirely; Admin Codings/Admin Adherence use
  the admin path. Never mix the two query paths.
- **Historical rates.** Always `BillingSettings.get_for_week(week_start)`, never the
  `BillingSettings.get()` singleton — otherwise past weeks recompute with today's rates
  instead of the rates in force at the time.
- **`PayrollAdjustment.commission_deduction`** is stored and displayed but deliberately
  never subtracted from pay. Commission tracking is unfinished. Do not wire it up
  opportunistically while working nearby.
- **`AgentSeparation` finalize is irreversible.** It cascades: deactivates the agent, closes
  the employment period, cancels future OT and role changes, auto-rejects pending requests,
  and auto-codes the remainder of the last week. There is no un-finalize flow.
- **Permissions are boolean flags on `Agent` plus middleware** — no Django groups or
  permissions anywhere. Enforce every rule server-side in the view; hiding a button is not
  access control. Financial columns must be stripped server-side, not merely hidden from
  the picker — `USER_EXPORT_FINANCIAL` in `scheduling/views.py` is the one existing worked
  example of this, used by the Users export.
- **Team scoping.** A `can_access_admin_tabs` holder who is not a super admin sees only
  their own direct reports plus themselves. `finance._admin_tabs_access(user)` returns
  `(has_access, team_pks)`, where `team_pks=None` means "see everyone."

## Conventions

- Weeks are Monday–Sunday everywhere. Snap `week_start` values using
  `wfm.utils.get_week_start` / `parse_week_param`.
- Excel exports use openpyxl and call `log_action(...)` to write to the Activity Log.
- Shared constants live in `wfm/constants.py`: bonus qualifying/disqualifying status sets,
  VTO-type statuses, the scheduled-hours zeroing set, portal admin types.
- **Find the real function or field name in the code before using it.** Do not assume names
  from these notes or from the reference docs.
- Match the existing UI vocabulary — badges, cards, modals, status pills. Do not introduce
  new patterns for the same job.
