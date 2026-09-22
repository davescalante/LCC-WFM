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
Currently **737**. The tests are the regression gate and double as executable specs for the
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
  `_holiday_worked_hours` discounts not-ready time **in excess of a flat 1-hour allowance per
  holiday day** (NOT `login × nr_ratio` — that was the old rule); the deduction reduces only the
  connected/login portion, never coded time. The money engine uses `(login + coded) × nr_ratio`
  pooled over the whole week and capped at 6 h/7 h; `_refresh_actual_hours` uses
  `(login + coded) × nr_ratio` per day, uncapped — those two are unchanged and still use
  `nr_ratio`. **The worked-holiday premium is paid on connected + coded, not login alone:** the
  nómina calls `_holiday_worked_hours_incl_coded`, which adds each person's coded hours on the
  holiday date (regular codings for agents, admin codings for official admins) to the
  NR-adjusted login hours, so the 2× premium covers all worked holiday hours and a worked
  holiday pays triple on them. (`_holiday_worked_hours` still takes an `nr_ratio` arg for
  signature compatibility but no longer uses it — the holiday allowance is a flat 1 h.) The
  holiday hour count can still legitimately differ from that day's contribution to `final_hrs`;
  this is not a bug to reconcile.
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

## Skills landmines

Skills (Phase 1, `c902cfc`, migration `0054`) — `Skill`, `Agent.skills` (M2M), `AgentSkillChange`
(permanent add/remove history), `SkillRenameHistory` (permanent rename history) in
`scheduling/models.py`. Management page at `/skills/`; assignment happens on the Edit User form.
Phase 2 (`03cb7fa`) added a Filters panel to the Adherence tab, beside the existing supervisor
dropdown, that narrows the grid by skill.

- **`_sync_agent_skills` in `scheduling/views.py` is the single write path for `Agent.skills`.**
  A direct `.add()`/`.remove()`/`.set()` anywhere else writes no `AgentSkillChange` row and no
  Activity Log entry — same rule as never calling `Coding.objects.create()` directly (see
  `create_coding`). `agent_edit` calls `agent_form.save(commit=False)` specifically so Django's
  own `BaseModelForm._save_m2m()` never runs as a second, silent writer of this field; `save_m2m()`
  would apply `cleaned_data['skills']` straight from the POST with no history row, no Activity Log
  entry, and no protection for a retired-but-assigned skill. `AgentEditSkillsCommitPinTests` pins
  this by mocking `_save_m2m` and asserting it is never called.
- **`/skills/` (create/rename/retire/restore) is super-admin only, and the gate fails closed.**
  `skill_list` checks `request.user.is_superuser or getattr(request, 'has_finance_access', False)`
  — `getattr`, not `request.has_finance_access` directly, because `AgentAccessMiddleware` swallows
  its own exceptions and the attribute can be missing; a missing attribute denies access rather
  than raising. There is no delete control anywhere in the UI or view — only retire/restore.
- **Assigning skills to an agent on the Edit User form is deliberately NOT super-admin-gated.**
  Unlike `can_access_admin_tabs`/`can_manage_loans`/`can_auto_code_requests` (popped from the form
  for non-super-admins), the `skills` field is left on `AgentForm` for everyone — any staff admin
  who can edit a user can add or remove their skills.
- **Retiring a skill (`is_active=False`) never touches any agent's assignment and never writes an
  `AgentSkillChange` row.** The Edit User picker's queryset only offers active skills
  (`Skill.objects.filter(is_active=True)`), so a retired skill an agent already holds has no
  checkbox to uncheck — `_sync_agent_skills` re-attaches it from the pre-save `before` set every
  time that agent is saved, and because it's always re-attached it can never land in `removed`.
  Practical effect: once a skill is retired, an agent who already had it can no longer have it
  removed through the UI at all (Django admin or a direct DB write are the only ways out).
- **The duplicate-name check ignores two Five9 sort-order prefixes, but storage/display always
  keep the exact typed string.** `_skill_dedupe_key` strips a leading `$` and a leading `Y`
  followed by whitespace (Five9 uses `$` to force a skill to the top of its list and `Y ` to force
  it to the bottom) before the usual case-insensitive/whitespace-trimmed comparison — so "Only the
  Best" and "$Only the Best" collide as duplicates, but a real word starting with "Y" (e.g.
  "Youth Injury Intake") is untouched since only "Y" followed by whitespace counts as a prefix.
  This function is comparison-only; `Skill.name` is never rewritten to a normalized form.
- **Known limitation, decided deliberately: Skills has no history of who held a skill on a past
  date.** `AgentSkillChange` records *when* a skill was added or removed, but no screen
  reconstructs a past week's roster for a skill — there is no "who had Skill X on date Y" view.
- **The Adherence tab's skill filter (Phase 2, `03cb7fa`) narrows AFTER `_get_adherence_agent_pks`
  resolves — in `_apply_skill_filter`, the same display layer as `_apply_supervisor_filter` — and
  it must never move into the roster query.** Filtering is AND ("holds all selected skills"), not
  OR, and it stacks with the supervisor filter. The natural way to write "holds all" is one
  `.filter(skills__id=...)` per skill chained onto the caller's queryset, but that needs
  `.distinct()` to collapse the joins, and DISTINCT combined with that queryset's ordering on
  related fields (`supervisor__user__last_name`, ...) is the exact PostgreSQL failure
  `_get_adherence_agent_pks`'s own docstring already warns about (see the roster landmine above).
  `_apply_skill_filter` sidesteps it by building the AND on a separate, unordered `Agent`
  queryset and narrowing the caller's queryset with `pk__in` instead — no join, so the ordering
  survives untouched. `AdherenceSkillFilterTests.test_roster_pks_identical_with_and_without_a_skill_filter`
  pins that `_get_adherence_agent_pks` returns an identical pk set whether or not a skill filter is
  active. `adherence_week`'s POST branch (the one that writes `AdherenceRecord` rows) is
  deliberately NOT narrowed by it — a display filter must never change which agents a write path
  visits.
- **Only active skills are offered in the Filters panel, and a retired id sitting in someone's
  session is dropped on read and the reconciled list written back.** Without that, a skill retired
  while it was selected in someone's session would keep narrowing their grid forever — no
  checkbox, no pill, no visible cause, surviving every week change — instead of simply stopping.
- **A bad `skills=` value is skipped, never raised.** `_get_skill_filter` swallows a non-numeric or
  unknown skill id rather than throwing, because an exception inside `adherence_rows_fragment`
  marks every supervisor group as failed on screen, not just the one bad value.
- **Removing the last skill pill emits an explicit empty `skills=` sentinel, not an absent
  param.** The view's fallback for "no `skills` param at all" is the session's stored filter, so
  clearing the last pill has to say "empty" out loud (`skills=`) or the old filter would silently
  reassert itself from the session on the very next load.
- **A supervisor group whose whole team lacks the selected skill(s) disappears entirely, header
  included — this is intended, and it is NOT the same as a group that failed to load.** A failed
  group always renders its own marker row with a Retry link (pre-existing, untouched by this
  work); an empty-because-filtered group renders nothing at all. The "no agents match" message
  itself only ever comes from the full-table request or from the progressive loader noticing every
  group loaded (none failed) and nothing matched.
- **The skill filter's session key (`adh_skill_filter`) is Adherence-only, deliberately not shared
  with `supervisor_filter`'s session key**, which several other pages reuse on purpose so one
  supervisor choice follows the user between tabs. Sharing the skill key would let a skill picked
  on Adherence silently narrow Codings, Daily Hours or Payroll too.
- **Measured cost: the skill filter adds exactly 2 queries when active (one to validate the
  requested ids against active skills, one to resolve the AND into a pk set), and 0 when not** —
  flat in both the number of skills selected and the size of the roster.
  `AdherenceSkillFilterTests` pins all three (unfiltered tab runs no skill queries at all; query
  count doesn't grow with more skills selected; doesn't grow with more agents on the roster).
- **Pre-existing issue found during this work, deliberately NOT fixed: the Adherence tab's
  30-second poll never establishes a baseline on a week with zero `AdherenceRecord`/`Coding`
  activity.** `adherence_poll` returns `latest: null` for such a week; the client's `_startPoll` only
  compares once `_adhLastTimestamp` is non-null, so on an empty week it keeps re-arming the
  "establish baseline" branch every tick instead of ever comparing — the first piece of activity
  that actually populates the week is the one the poll silently misses (later changes poll
  normally, since the timestamp is non-null after that). This affects the unfiltered tab and the
  supervisor filter identically, predates the Skills work, and is unrelated to the skill filter
  itself.
- **Phase 3 (`0ce7c22`) added a skill coverage column to the Staffing tab and, in the same
  commit, promoted the Filters panel to genuinely shared markup.** `templates/includes/
  skill_filters_popover.html` is now the one Filters popover template for both Adherence and
  Staffing — not two similar copies — and `scheduling/views.py` gained three shared primitives
  (`_active_skills`, `_agents_with_all_skills`, `_resolve_skill_filter`) that `adherence.views`'s
  `_get_skill_filter`/`_apply_skill_filter` and `erlang.views`'s `_get_skill_filter` are now thin
  wrappers over. Extend the shared helpers, not either tab's wrapper, when the rule itself needs
  to change.
- **Adherence and Staffing use separate session keys on purpose** — `adh_skill_filter` vs.
  `erlang_skill_filter` — so a skill picked on one tab's Filters panel never silently narrows the
  other. Same reasoning as `adh_skill_filter` already being kept off `supervisor_filter`'s shared
  key (see the Phase 2 note above); do not consolidate them.
- **The Staffing skill column is a strict subset of Scheduled Staff by construction, and must stay
  that way.** `erlang._build_skill_maps` narrows the exact `agents_map`/`excluded_map` lists
  `_build_scheduled_map` already built, using the `agent_id` now carried on every entry — it
  reimplements none of `_build_scheduled_map`'s exclusion rules (adherence-status exclusions, the
  Quit/Baja mark, per-cell dedupe). Anyone changing what counts as "scheduled" must change it once
  in `_build_scheduled_map`; the skill column inherits it for free. The column itself only renders
  when a skill is selected (`{% if selected_skill_ids %}`), sits immediately right of Scheduled
  Staff, uses AND semantics (holds every selected skill), colors red only at zero, and its header
  shows the one selected skill's name or `"<name> +N"` for more than one.
- **`.staffing-display` is load-bearing JavaScript on the Staffing tab, not styling** — the day
  badge, the day summary bar and the Variance column all select on it. The new skill column
  deliberately uses its own class (`skill-coverage-display`) and id prefix (`skillcov-`) rather
  than joining that class. `StaffingSkillColumnRenderTests` pins the count of `.staffing-display`
  elements as identical with the filter on and off, and asserts no element ever carries both
  classes.
- **Scheduled Staff and the skill column are allowed to diverge on overtime, deliberately.**
  Scheduled Staff counts any non-cancelled `OvertimeShift` regardless of role or skill; the skill
  column counts an OT agent only if they hold every selected skill. An OT agent without the skill
  raises Scheduled Staff but not the skill count — this is the intended behavior, not a
  reconciliation bug.
- **The existing Scheduled Staff popover (`SCHEDULED_AGENTS`/`EXCLUDED_AGENTS`/`STATUS_SUMMARY`)
  is untouched by the skill filter — pinned byte-for-byte.** `StaffingSkillColumnRenderTests`
  extracts those three JSON blobs from the rendered page with the filter on and off and asserts
  they're identical; the skill popover's own data (`SKILL_AGENTS`/`SKILL_EXCLUDED`/
  `SKILL_STATUS_SUMMARY`) is a separate, additional payload.
- **The Staffing CSV download and saved Erlang reports were deliberately left out of Phase 3** —
  neither reflects the skill filter or the skill column. Not an oversight to "finish" later without
  being asked.
- **Measured query cost on Staffing: 25 queries unfiltered, 27 with a skill filter active** — the
  same flat `+2` Adherence's own filter pays (validate the requested ids, resolve the AND to a pk
  set), regardless of how many skills are selected or how many agents are scheduled.
  `StaffingSkillFilterQueryCountTests` pins the delta, not an absolute count.
- **Retired skills and bad `skills=` input behave on Staffing exactly as Phase 2 specified for
  Adherence** (dropped on read, the reconciled list written back to session, never raising) —
  because both tabs now call the same `_resolve_skill_filter`, this is guaranteed rather than
  separately maintained.
- **Known limitation: the skill column shows expected coverage, not live coverage.** It counts
  agents who are scheduled and hold the selected skill(s) in this app — it cannot know whether an
  agent is actually logged into that skill in Five9 at that moment. See SYSTEM-SUMMARY.md §14.7.

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
