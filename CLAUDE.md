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
Currently **464**. The tests are the regression gate and double as executable specs for the
trickier rules (NR caps, bonus eligibility, request approvals, export field gating).

Two read-only management commands exist for diagnosis; neither is reachable from a request
path and neither writes anything. `verify_adherence_roster` runs the old and new roster
implementations against the same database and reports any difference in the pk sets
(`--weeks`, default 8). `schedule_data_inventory` prints row counts, date ranges and
future-dated counts for the schedule/adherence tables.

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
  `scheduling/views.py`, already exists and is reused twice (including an import into
  `erlang/views.py`), but the same comparison logic is independently reimplemented in 5
  other places. Use `_best_shift_template` rather than adding a sixth reimplementation.
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
  takes `supervisor_id`, but uses it only to build the cache key — the query always scans
  the whole roster. Narrowing happens afterwards in `_apply_supervisor_filter` and the
  `group=` param. So filtering the tab to one supervisor does not make its query cheaper,
  which is genuinely counterintuitive and cost real diagnostic time.
- **Historical rates.** Always `BillingSettings.get_for_week(week_start)`, never the
  `BillingSettings.get()` singleton — otherwise past weeks recompute with today's rates
  instead of the rates in force at the time.
- **`OvertimeShift` has no uniqueness guard** (model `Meta` has indexes only; the original
  `unique_together` was dropped in migration `0018` to allow split OT). `overtime_week` uses
  a bare `.create()` in a loop whose count comes straight from the POST body with no cap,
  unlike `open_ot_create`, which clamps to 20. Duplicate rows mean duplicated hours, and
  those feed billing and payroll. Known and unfixed — see `HANDOFF.md` §7.
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
