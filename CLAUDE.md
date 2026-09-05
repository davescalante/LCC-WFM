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
- `db.sqlite3` is **gitignored and untracked** (`.gitignore` line 1; untracked since
  `ed1022e`). Local database state does **not** travel with the repo — every developer
  has their own, and yours can be missing migrations that are already applied in
  production. Run `python3 manage.py showmigrations` before trusting local behavior,
  and never infer production data from it.

## Tests

`python3 manage.py test` — the full suite must pass before any commit. Report the pass count.
Currently **602**. The tests are the regression gate and double as executable specs for the
trickier rules (NR caps, bonus eligibility, request approvals, export field gating).

Three read-only management commands exist for diagnosis; none is reachable from a request
path and none writes anything. `verify_adherence_roster` runs the old and new roster
implementations against the same database and reports any difference in the pk sets
(`--weeks`, default 8). `verify_ot_topups` (`370a2de`) does the same job for the OT incentive
top-ups — it holds the pre-dedupe plain-`+=` summation and the current deduped one frozen side
by side, compares the money week by week with the engine's exact expressions, and exits 1 on
any difference (`--weeks`, default 12). `schedule_data_inventory` prints row counts, date ranges and
future-dated counts for the schedule/adherence tables, plus three OT-duplicate sections
(`ccb64cf`): exact-duplicate slots keyed on agent/date/start/end with cancelled rows
excluded, the money-exposed subset (extra rows that are `completed` **and** incentivized,
priced and broken out per week), and the origin split by write path.

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
  week_start`. This predicate is duplicated across 11 call sites: `_get_adherence_agent_pks`
  and `codings_week` in `adherence/`, the `agent_list` export in `scheduling/views.py`,
  `billing_report`, `billing_export`, `billing_export_v2`, `payroll_report`,
  `payroll_export`, and `codings_export` in `finance/views.py`, and `_pay_window` plus
  `_agent_nomina_data` in `nomina/views.py` (the latter deliberately uses only the
  inactive-separated half — see its comment). Four of these (`billing_report`,
  `billing_export`, `payroll_report`, `payroll_export`) use an `.exclude()`-shaped form
  rather than the `Q()` form — the same rule in two different shapes, so a change to this
  predicate has to check both and can easily miss a site. Prefer additive changes; do not
  consolidate it into one helper unless that is explicitly the task.
- **Best-template resolution.** A specific-date `Shift` override always beats a
  `ShiftTemplate`; among templates covering the date, the latest `effective_from` wins
  (`None` treated as earliest). A shared helper, `_best_shift_template` in
  `scheduling/views.py`, already exists and is reused three times (`agent_my_shifts`,
  an import into `erlang/views.py`, and the shared `_resolve_schedule_blocks` that
  `_ot_schedule_conflict` and `overtime_week`'s `_resolve_agent_week_schedule` both call),
  but the same comparison logic is independently reimplemented in 5 other places. Use
  `_best_shift_template` rather than adding a sixth reimplementation.
- **`is_admin_coding` and `is_official_admin` are a hard partition.** Regular
  Codings/Adherence queries exclude admin rows entirely; Admin Codings/Admin Adherence use
  the admin path. Never mix the two query paths.
- **Coding creation goes through `adherence.views.create_coding`.** Extracted from
  `add_coding_ajax` in `d4fd7d6`; the Codings tab and the coding-request auto-code hook
  are its two callers. It also owns the recompute rule: `_refresh_actual_hours` fires for
  **regular codings only**. That function sums `is_admin_coding=False` rows exclusively,
  so calling it for an admin coding would not even include the new row — it would just
  write an `AdherenceRecord` for an Official Admin who should not have one.
  `finance.views.add_admin_coding_ajax` deliberately never recomputes either. Do not add
  a bare `Coding.objects.create()` anywhere; it silently skips the recompute.
- **The Adherence roster query is not indexable.** `_get_adherence_agent_pks` collects pks
  in two steps — eligibility, then four scoped activity queries unioned in Python — and it
  must stay that way (`86ab564`). As one `filter()` with four OR'd conditions across four
  multi-valued relations, each relation got an unrestricted `LEFT JOIN` and every date
  predicate landed in the `WHERE`, so the database materialised the cartesian product of
  every shift, OT, template and adherence record per agent: half a billion rows to return
  93 integers, and a 502. Every useful index already exists; the cost is the join shape,
  so do not reach for an index here. Two things inside it are load-bearing: the eligibility
  conditions must stay in a **single `.filter()` chain** (split across calls, each gets its
  own join and an agent with several separation rows is judged on a combination no single
  row satisfies), and the `ShiftTemplate` branch stays **unscoped by date** — it looks wrong
  and is deliberate; `adherence_start_date` is the only floor on it.
- **The Adherence supervisor filter does not filter in SQL.** `_get_adherence_agent_pks`
  takes `supervisor_id`, but the query always scans the whole roster regardless — that
  parameter used to also key a 300-second cache, removed in `23c67a8` since the query costs
  single-digit ms post-`86ab564` and isn't worth caching. Do not re-add a cache here; the
  tab calls this helper once per supervisor group per page load with identical arguments
  every time, which is redundant by design but cheap. Narrowing happens afterwards in
  `_apply_supervisor_filter` and the `group=` param. So filtering the tab to one supervisor
  does not make its query cheaper, which is genuinely counterintuitive and cost real
  diagnostic time.
- **Historical rates.** Always `BillingSettings.get_for_week(week_start)`, never the
  `BillingSettings.get()` singleton — otherwise past weeks recompute with today's rates
  instead of the rates in force at the time.
- **`OvertimeShift` has no database-level uniqueness guard, deliberately.** Model `Meta` has
  indexes only; `unique_together = ('agent','date')` was dropped in migration `0018` so split
  OT can hold several rows per agent-day. Enforcement is **application-level only**, in the two
  views that create rows: `open_ot_claim` and `overtime_week` (both `b544e4d`/`5ea019f`, keyed
  on the exact slot — agent + date + `start_time` + `end_time` — so split OT at different hours
  is untouched by construction). A DB constraint **cannot** be added until the duplicate rows
  already in production are cleaned up: Postgres validates a new unique constraint against
  existing data and would fail the deploy. Any new OT create path must carry its own guard.
- **OT overlapping a scheduled shift is blocked on the claim path only, and several of its
  rules look wrong on purpose.** `scheduling.views._ot_schedule_conflict` (`7b7e0b5`) sits next to
  `_best_shift_template` and reuses it; it returns a `(day_label, time_label)` tuple or `None`,
  and each caller writes its own sentence because the agent-facing and approver-facing messages
  differ in voice. `open_ot_claim` calls it for immediate feedback; `ot_claim_approve` is the
  **authoritative** check — a claim can sit for days while the schedule changes underneath it,
  and that view is the only path turning a claim into an `OvertimeShift`. Do not "clean up" any
  of these:
  - The comparison is **strict half-open**, so exactly back-to-back is **allowed**. A shift
    ending 17:00 with OT starting 17:00 is the normal way OT attaches to a shift; blocking it
    would break the most common legitimate case.
  - It resolves **three** days (`d-1`, `d`, `d+1`). `d-1` catches a previous-day shift spilling
    past midnight; `d+1` catches OT that itself wraps past midnight into the next day's shift.
    Both directions are needed — a two-day window silently misses the second.
  - The wrap rule is `end < start`, matching `OvertimeShift.total_shift_hours()`, **not**
    `end <= start`. A zero-length slot returns `None` early rather than becoming 24 h.
  - **No schedule data means no conflict.** Unknown is not a conflict; blocking on absent data
    would stop OT for every agent without a template, a worse failure than letting one through.
  - **`is_off` days allow OT** — that is the normal OT case, not an oversight.
  Split shifts are covered: `extra_blocks` is read from both `ShiftBlock` and
  `ShiftTemplateBlock`. `overtime_week` is deliberately **not** blocked and blocking it is not
  planned — coordinators sometimes need to enter an intentional overlap, and once
  `ot_claim_approve` blocks, that editor is the only remaining path for one. A blocked approval
  leaves the claim `pending`, the posting `open` and backup claims untouched (it returns before
  the `transaction.atomic()` that auto-rejects them), so the approver can fix the schedule and
  approve, or reject with a reason.
- **The approver inbox's conflict warning (`22705ec`) informs, it does not prevent — approving a
  flagged claim still creates the duplicate.** `scheduling.views.overtime_list` flags a pending
  `OTShiftClaimRequest` when its requester already has another pending claim for the identical
  slot, or already holds a non-cancelled `OvertimeShift` there — mirroring the same-slot guard
  `open_ot_claim` enforces at submission time (`b544e4d`), applied display-only because a claim
  can go stale after that guard already ran (most concretely, `overtime_week` assigning the same
  slot directly, since that path has never checked pending claims). **`ot_claim_approve` was not
  changed.** Clicking Approve on a flagged claim still creates a second `OvertimeShift` for that
  slot — verified directly: approving a claim flagged "Already holds an OT shift for this slot"
  produced exactly that duplicate. Do not treat the warning as a guard, and do not assume flagging
  a claim is enough to stop the duplicate it names — that requires either the approver reading it
  and acting, or `ot_claim_approve` itself being changed to check pending claims and cross the two
  creation paths, which has not happened.
- **The finance/adherence OT dedupe asymmetry is RESOLVED (`370a2de`) — both sides now collapse
  on `(start_time, end_time, status)` per agent/day.** `finance.views._get_billable_weekly_data`
  used to loop every `status='completed'` `OvertimeShift` and do a plain `+= total_shift_hours()`
  per incentive type, so a slot recorded twice paid its premium twice through `ph_topup_mxn` /
  `ot_1_5_topup_mxn` and `total_pay_mxn`; `adherence.views._build_maps` already collapsed exact
  duplicates on that key (and `_net_ot_evening_hours` unions intervals rather than summing). The
  finance loop now uses the identical key. Four things about it are deliberate:
  - **Split OT is untouched by construction.** Two rows on one day at *different* hours have
    different keys and both still count — which is what migration `0018` dropped
    `unique_together` for. Do not widen the key to `(agent, date)`.
  - **`incentive_type` is deliberately NOT in the key.** Including it would keep both rows of a
    slot recorded once as `power_hour` and once as `time_and_a_half` and pay **both** premiums —
    the exact overpay this closes. One slot pays one premium, always.
  - **`.order_by('pk')` is the deterministic tiebreak**, and it is load-bearing. When a
    duplicated slot's rows disagree on `incentive_type` the earliest-created row is the one that
    pays, matching how `schedule_data_inventory` attributes a duplicated slot. Without it, which
    incentive pays depends on database row order — `OvertimeShift.Meta.ordering` is `['date']`
    and gives no tiebreak, so pay would be non-deterministic.
  - **The `no_show` loop just above is untouched** and must stay that way. It assigns a boolean
    into `bonus_map`, so duplicates were always harmless there; `status` in the dedupe key is
    what keeps a `completed` and a `no_show` row at identical times from collapsing into one
    consequence.
- **A dedupe can only ever reduce a top-up, never increase it — and in an already-paid week that
  is a reconciliation item, not a display bug.** Removing rows from a sum is one-directional, so
  no input to this code can raise anyone's pay. But the **Finance payroll report recomputes live
  for every week, forever** (finalized Nómina weeks are safe — they render from a `PayrollRun`
  snapshot). So if a duplicated slot ever does accumulate two `completed` incentivized rows in a
  week that has already been paid out, that week's report will show **less** than what actually
  left the bank. That gap is real money already disbursed and belongs to whoever owns payroll —
  do not "fix" the report to match the payment.
- **`finance/management/commands/verify_ot_topups.py` is the standing regression check for the
  above.** Read-only, not reachable from any request path, `--weeks` default 12, exits 1 on any
  difference. It holds **both** summations frozen — `_topups_original` (the pre-`370a2de` plain
  `+=`) and `_topups_deduped` (what the engine now does) — and compares them week by week with
  the engine's exact money expressions. It was the pre-deploy gate and it still works as a
  regression check, so keep both loops frozen; a change to the engine's OT summation should be
  mirrored into `_topups_deduped`, never into `_topups_original`. Production proof before deploy
  (2026-09-02): clean across **both 12 and 52 weeks**, including the busy weeks of 2026-06-01
  (17 agents with completed OT) and 2026-08-10 (24 agents). **Zero pay changed.**
- **Nómina is unaffected by the OT top-ups entirely.** It never reads `total_pay_mxn` and never
  reads any `ot_*` field — only `final_hrs`, `hourly_mxn`, `base_pay_mxn`, `bonus_mxn`,
  `admin_bonus_mxn` and `commission_pct` come out of the engine into `nomina/`. The OT premium
  reaching the Finance payroll report but not Nómina is a deliberate asymmetry, not a gap.
- **Cancelling an OT row is the safe void; deleting one destroys history.** Every consumer
  already excludes `status='cancelled'` (`adherence._build_maps`, `erlang._build_scheduled_map`,
  `_zero_missing_scheduled`, the OT grid, `overtime_export`), and `finance` counts only
  `completed`/`no_show` — so a cancel removes a row from hours, pay and staffing while keeping
  the row, its times and its `cancellation_reason`. `_finalize_separation` is the existing
  precedent, retiring future OT with a bulk `.update(status='cancelled', ...)`. A hard delete
  cascades and takes `OTShiftVerification` (`OneToOneField`) and the whole
  `OTCancellationRequest` audit trail with it. **Any future cleanup must cancel, never delete.**
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

- **My Requests merges OT shift claims into the same table as the six `AgentRequest` types —
  deliberately, not as a separate section.** `agent_my_requests` normalizes `OTShiftClaimRequest`
  rows (date/times come from `claim.open_shift`, not the claim itself) into the same
  `pending`/`approved`/`rejected` vocabulary and merges them by `submitted_at` alongside
  `AgentRequest` rows. This duplicates the Available OT page's own "My Shift Requests" history
  table (`agent_available_ot`, capped at 30) on purpose — Available OT serves the moment of
  claiming a specific shift, My Requests serves the general "what have I asked for" question, and
  neither should be collapsed into the other. **`OTShiftClaimRequest.requester_read` is never
  touched by `agent_my_requests`** — that flag belongs to `agent_available_ot`, the only view that
  mutates it, and marking it read from a second page the agent might glance at without noticing
  the OT row would clear the unread badge before they ever saw the outcome on its native page.

## Nómina landmines

Nómina (`nomina/`) is the weekly payroll section: super-admin only, Infinity employees only, two
Excel files a week. Full map in `SYSTEM-SUMMARY.md` §11. **It honors the two-pipelines rule** —
nothing in `nomina/` reads `AdherenceRecord.actual_hours`; all hours and base pay come from
`finance.views._get_billable_weekly_data`.

- **Finalizing a week is irreversible and there is no un-finalize.** `POST /nomina/finalize/`
  writes one `PayrollRun` holding a JSON snapshot of the Agent (Mine + Yours), and Admin rows and
  totals. After that, both screens and both exports read the snapshot and never recompute. Nothing
  in the codebase deletes or rewrites a `PayrollRun` — the only way back is a manual database
  delete. `week_start` is unique, so re-finalizing is a no-op.
- **Finalize locks only four editors.** `inputs`, `input_type`, `admin_hours` and `overrides` check
  `_finalized_run` and reject POSTs. Loans, Break Abuse, Holidays, Welcome enrollment,
  `VacationAdjustment` and `AdminBonusDeduction` do **not** check it. That is currently safe only
  because the finalized week renders from the snapshot. Anything new that reads live data for a
  finalized week breaks the freeze.
- **The Admin Nómina pays the FULL admin bonus.** The `Total` column is
  `Subtotal + gross_bonus − deductions`. The penalty % (`AdminBonusDeduction`) and the
  worked÷scheduled vacation proration are computed into `admin_bonus_corrected` and stated in the
  Notes cell **only** — they are never subtracted from the exported Total. Do not "fix" this by
  wiring `admin_bonus_corrected` into `total`; that changes what admins are paid.
- **`AdminBonusDeduction` is written from outside Nómina.** Its editor is the Admin Adherence tab
  (`finance.views.save_admin_deduction`, gated by `admin_tabs_access_required`), and
  `finance.views.admin_penalty_reco` serves the recommendation from `nomina.views.admin_bonus_penalty`.
  A non-super-admin with admin-tabs access can therefore change a Nómina input.
- **Break abuse silently kills the Welcome Bonus too.** In `_agent_nomina_data`, one
  `BreakAbuseIncident` in the week forces `bonus` to 0, and the Welcome Bonus is then paid only if
  `bonus > 0`. The same chain runs through the `adherence` override: raising it restores the
  Welcome Bonus. Two payouts, one variable.
- **An unset `NominaWeek.spiff_fx_rate` pays every spiff $0.** `fx = nweek.spiff_fx_rate or Decimal('0')`.
  The rate is nullable with no default and no carry-over between weeks, on purpose. The
  `spiff_needs_rate` / `spiff_unpaid_count` banner is the only thing standing between that and a
  silent underpay — do not remove it.
- **Uploads are wipe-and-replace across the whole roster.** `input_type` zeroes the module's field
  for every rostered agent that week before writing the file's rows. For Kill Team QA this writes an
  explicit `0`, which is **not** the same as `NULL`: `kill_team_qa` is nullable precisely so that
  NULL means "never entered → pay the $400 default." So a Kill Team QA upload that omits someone
  drops them from $400 to $0, permanently for that week.
- **`_dec` turns anything unparseable into 0.** A malformed amount in an uploaded file is imported
  as zero, not flagged as unmatched. Only rows that match no agent become `UnmatchedInputRow`.
- **The `base_pay` override behaves differently on the two sheets.** On the Agent Nómina it replaces
  the engine's `base_pay_mxn` and extra-hours and vacation pay are still added on top. On the Admin
  Nómina it replaces `base_pay_mxn + extra_hrs × rate` — the whole thing, Admin Hours included.
- **Only three override fields have a UI, but `_agent_nomina_data` honors more.** The Overrides page
  writes `base_pay`, `adherence`, `holiday` (admins: `base_pay`, `admin_bonus`, `holiday`), yet
  `ov()` is also called for `net_lpo`, `spiff`, `welcome`, `referral`, `kill_qa`, `comedor`,
  `transport` and `loan`. A `NominaOverride` row with one of those `field` values silently takes
  effect with nothing on screen to create or reveal it. Overrides apply to "Mine" only.
- **The non-billable-overpay guard does not zero holiday or vacation pay.** For an untracked agent
  with no billable Five9 profile, `_agent_nomina_data` zeroes `base_pay_mxn`, `bonus_mxn` and
  `final_hrs` but deliberately leaves `hourly_mxn` intact. `_holiday_worked_hours` reads
  `DailyAgentHours` independently and falls back to counting **every** Five9 username when the agent
  has no billable profile, so such an agent can still be paid a 2× holiday premium on non-billable
  hours. The guard's separated-agent carve-out uses only the inactive half of the pay-window
  predicate on purpose — `_pay_window()`'s `status='active'` branch would spare the very agents the
  guard exists for (`4f23ec5`).
- **Net LPO is the one live consumer of `PayrollAdjustment.commission_deduction`.** Everywhere else
  that field is stored and displayed but never subtracted; in `_agent_nomina_data` the corrected
  ("Mine") LPO is `gross × (1 − commission_pct/100)`. Changing that field's meaning changes pay here.
- **Two hours fields, split by role, and they do not fall back to each other.** Agents use
  `WeeklyPayInput.extra_hours` (Extra Hours module); official admins use
  `hours_add` / `hours_deduct` (Admin Hours module). `_admin_nomina_data` reads only
  `hours_add − hours_deduct`, so an admin left with a stale `extra_hours` value is paid nothing for
  it. Migration `0013` backfilled the existing rows; do not reintroduce `extra_hours` for admins.
- **`WeeklyPayInput.welcome` has no writer.** No `INPUT_TYPES` module maps to it, so the fallback
  branch when an enrolled agent earns no adherence bonus always evaluates to 0. The real source of a
  welcome payment is `WelcomeBonusEnrollment` (or a `NominaOverride`).
- **Nómina has no team scoping and no activity logging.** Every page shows the whole Infinity roster
  to anyone who passes `nomina_access_required` (`is_superuser` or `is_super_admin`);
  `finance._admin_tabs_access` is not used here. `can_manage_loans` widens access to `/nomina/loans/`
  only. Neither export calls `log_action`, unlike every other export in the app.
- **`Loan.granted_by` is how loan money reconciles.** The borrower is deducted
  `installment_for_week` regardless, but the offsetting `Prestamo given` credit lands only if the
  manager is an official admin on that week's Admin Nómina. `_admin_nomina_data` surfaces the gap as
  `uncredited_loans` / `uncredited_repay`; keep that reporting when touching loans. `Loan` also has
  no uniqueness guard and `loans` uses a bare `.create()`.
- **`VacationAdjustment.year` is an anniversary year, not a calendar year.** It comes from
  `_vacation_year`, which returns the calendar year the agent's *current* work-anniversary period
  began, so an adjustment survives the Dec→Jan boundary. Keying it by `today.year` would silently
  drop adjustments for anyone hired mid-year.
- **`_vacation_hours`, `_holiday_not_worked_hours` and `_admin_bonus_factors` read
  `adherence.views._build_maps` by tuple index** (`[0]` = shift map, `[4]` = split-shift extra
  hours). Reordering that return tuple changes vacation and holiday pay with no error.

## Vacations landmines

Full map in `SYSTEM-SUMMARY.md` §12. The math lives in `nomina/views.py` and is imported by
`scheduling` and `adherence`, so a change there moves three screens at once.

- **The balance is per work anniversary; the overdraw check is per calendar year.** They
  disagree on purpose in neither direction — `vacation_balance` counts `'V'` days since the
  agent's hire anniversary, but `vacation_request_check` counts `new_days` only for dates whose
  `d.year == today.year`. A request whose dates fall in the next calendar year therefore scores
  `new_days = 0`, `overdraw` is never `True`, and a supervisor can approve it with no balance
  left. Anything touching either function has to keep both keyings straight.
- **`used` counts only up to today, so approved future vacation is invisible to the next
  check.** `vacation_balance` filters `date__lte=today`. Two future requests can each pass the
  overdraw gate against the same untouched balance and together exceed it. Approving does not
  reserve the days.
- **The `V` safety net exists on one write path out of three.** Only
  `adherence.views.save_adherence_cell` checks the balance (and only on a transition *into*
  `'V'`, and only when `remaining < 1`). The bulk grid POST in `adherence.views.adherence_week`
  writes `status_val` straight through with no check, and vacation-request approval enforces the
  super-admin tier instead. Do not assume placing a `'V'` is gated.
- **Approving a vacation request marks every calendar day in the range**, weekends and scheduled
  days off included, and `update_or_create` **overwrites** whatever status was on those days
  (`actual_hours` is left alone). The LOA branch filters to scheduled days; the vacation branch
  deliberately does not — and `_vacation_hours` pays a flat 8 h for a `'V'` on an unscheduled
  day, so a Sat–Sun in the range is 16 paid hours.
- **Nothing validates the request's date range.** `_fill_request_from_post` stores
  `vacation_start`/`vacation_end` unchecked: an end before the start silently marks zero days and
  never overdraws; there is no cap on the length.
- **`/vacations/` scopes on `role == 'admin'`, and it is on the portal allowlist.** The
  `cs`, `tester` and `sms_email` portal admin types have `role='admin'`, and `/vacations/` is in
  `_AGENT_ALLOWED` in `wfm/middleware.py` — so those portal users see the whole active roster's
  balances, not just their own row. Accrued/Used and the edit form stay super-admin-only.
- **The Vacations page shows `status='active'` agents only** — no pay-window carve-out, so a
  separated agent inside their final pay window has no row even though their `'V'` days still pay.
- **Vacation pay is Agent Nómina only.** `_admin_nomina_data` computes `vac_hrs` and the
  worked÷scheduled bonus proration but states both in the **Notes column only**; the exported
  Total never includes them. Do not "fix" that — it changes what admins are paid.
- **`VacationAdjustment` stores a delta, not the number on screen.** The `/vacations/` form takes
  a target *available* figure and saves `available − (accrued − used)`, keyed by
  `_vacation_year(agent)`. Later `'V'` days still deduct on top of it, so re-reading the stored
  `days` as "their balance" is wrong. Written from that one form; no `log_action` anywhere.

## Holiday landmines

Full map in `SYSTEM-SUMMARY.md` §13.

- **The holiday not-ready allowance is a third NR rule and must stay different.**
  `_holiday_worked_hours` uses `login × nr_ratio` **per holiday day, uncapped, with coded time
  excluded** from the allowance base. The money engine uses `(login + coded) × nr_ratio` pooled
  over the whole week and capped at 6 h/7 h; `_refresh_actual_hours` uses `(login + coded) ×
  nr_ratio` per day, uncapped. All three read the same `nr_ratio`. The premium is meant to be
  paid on the day's productive hours (`d5ecf07`), so the holiday hour count can legitimately
  differ from that day's contribution to `final_hrs` — "triple pay" holds only when the two
  figures happen to agree. This is not a bug to reconcile.
- **`status='Holiday'` and worked holiday hours are mutually exclusive by design.** A day marked
  `'Holiday'` is dropped from `_holiday_worked_hours` entirely, even if Five9 login exists for it,
  and paid the 1× not-worked way instead (`4f152b0`). Removing that exclusion double-pays.
- **`'Holiday'` is bonus-qualifying and zeroes scheduled hours.** It is in `BONUS_QUALIFYING` and
  `SCHED_HOURS_ZEROING_STATUSES`, absent from `BONUS_DISQUALIFYING` and `COS_INCLUDE_STATUSES`.
  The zeroing is adherence/NR accounting only — `_holiday_not_worked_hours` reads the resolved
  shift directly, so holiday pay does not see it.
- **An agent with no billable Five9 profile gets holiday hours counted from *every* username.**
  `_holiday_worked_hours` falls back to `bn is None → count everything`, the same fallback the
  non-billable-overpay guard exists to contain — and that guard never zeroes `holiday_pay`.
- **Holiday tags on the two adherence grids are display only.** `adherence_week` and
  `admin_adherence` read `Holiday` for the visible week purely to tint the date header; no
  `AdherenceRecord` is ever created from a `Holiday` row. The `'Holiday'` status is always set by
  hand.
- **Deleting or moving a `Holiday` silently repays an open week.** `/nomina/holidays/` and the
  Django admin both write with no finalize check and no `log_action`; a finalized week is safe
  only because it renders from its `PayrollRun` snapshot. Any open week recomputes on the next
  page load.

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
