# LCC-WFM — System Reference (for planning conversations)

*Generated 2026-08-03 by reading the live codebase at `/Users/denisetijerina/Documents/LCC-WFM` (branch `main`, HEAD `2ecb0c7`). This is a structural map, not a code dump — verify exact line numbers against current code before quoting them, since the app changes fast (several commits landed the same day this was written).*

---

## 1. Stack & Deploy

- **Framework**: Django 4.2.30 (Python). Single project `wfm` containing four documented apps: `scheduling`, `adherence`, `erlang`, `finance`, plus a 5th app `nomina` (payroll) not covered by this document — see §2.
- **Database**: SQLite locally (`db.sqlite3` — **gitignored and untracked**; it was tracked once and deliberately removed in `ed1022e`, so local DB state does not travel with the repo and each developer's copy can be missing migrations already applied elsewhere). **PostgreSQL in production** via `dj-database-url` + `psycopg2-binary`.
- **Hosting**: Render (`IS_RENDER` env detection; `DEBUG` off there). App server = gunicorn; static files via WhiteNoise (`CompressedManifestStaticFilesStorage`).
- **Repo**: GitHub `davescalante/LCC-WFM`, default branch `main`. Deploy = push to `main`; `build.sh` runs `pip install -r requirements.txt`, `collectstatic`, `migrate`.
- **Key deps** (`requirements.txt`): Django 4.2.30, gunicorn 23, whitenoise 6.11, psycopg2-binary, dj-database-url, openpyxl 3.1.5 (all Excel exports).
- **Frontend**: server-rendered Django templates, inline CSS, vanilla JS. No SPA framework, no build step. Small AJAX/JSON endpoints handle in-place updates (status pills, cell edits, live-poll badges via `/poll/` and `/adherence/poll/`).
- **Auth**: stock `django.contrib.auth`, login at `/accounts/login/`. Custom `SessionTimeoutMiddleware` (4h inactivity / 16h absolute) and `AgentAccessMiddleware` (role-based routing + badge counts) in `wfm/middleware.py`.
- **Tests**: `scheduling/tests.py`, `erlang/tests.py`, `adherence/tests.py`, `finance/tests.py`, `nomina/tests.py` — run with `python3 manage.py test`. As of the last commit: 464/464 passing. Tests double as executable specs for the trickier rules (NR caps, bonus eligibility, request approvals, export field gating).
- **Diagnostics**: two read-only management commands, neither reachable from a request path and neither writing anything — `verify_adherence_roster` (roster pk-set parity, `--weeks` default 8, exits 1 on a mismatch) and `schedule_data_inventory` (row counts, date ranges, future-dated counts for the schedule/adherence tables). See §10 items 2–3.
- **URL mounts** (`wfm/urls.py`): scheduling at site root (duplicated at `/scheduling/`), `/adherence/`, `/erlang/`, `/finance/`, plus `admin-codings/` and `admin-adherence/` mounted directly off the **root** urlconf (not under `/finance/` — see §4), Django admin at `/admin/`, auth at `/accounts/`.

---

## 2. Apps & Purpose

- **`scheduling`** — the backbone app. Owns the universal `Agent` profile (used by staff and agents alike), shift scheduling (templates + overrides), overtime (assigned + open-shift job board), the request/approval system, role-change scheduling, separations, the audit log, and the agent self-service portal views. By far the largest app (`views.py` ~4,000 lines).
- **`adherence`** — daily attendance status entry, manual "codings" (credited time blocks), Five9 Daily Hours CSV ingestion, the not-ready(NR)-deduction math that produces `actual_hours`, and the weekly adherence grid/bonus computation (`_build_maps`/`_build_rows`) that Finance reuses.
- **`finance`** — super-admin-only money section: billing (Infinity→LCC, USD), payroll (MXN), the versioned `BillingSettings`, Admin Codings/Admin Adherence views (business logic lives here even though the tabs are no longer under `/finance/` in nav), and every Excel export.
- **`erlang`** — Erlang-C staffing forecast calculator: hourly call-volume upload, required-vs-scheduled-vs-actual-agents grid, OT-shortfall visibility, saved report snapshots.
- **`wfm`** — the project shell: settings, root urlconf, the two custom middlewares, and shared `constants.py` (bonus qualification sets, portal-admin types, status-zeroing sets) and `utils.py` (week helpers, including `get_monday_choices()` for populating week-picker `<select>` dropdowns).
- **`nomina`** (payroll) — super-admin-only weekly payroll section that produces the Agent and Admin Nómina Excel files for Infinity employees in MXN. It reuses `finance.views._get_billable_weekly_data` for all computed hours and pay and layers manual inputs, loans, bonuses, deductions, overrides and a Finalize/freeze step on top. It also owns the LFT vacation-balance helpers and the `Holiday` model that `scheduling`, `adherence` and `finance` import. It carries its own `_infinity_agents`, `_admin_agents` and `_pay_window` copies. **Fully documented in §11.**

---

## 3. Key Models

### `Agent` (scheduling/models.py) — the single profile for every human in the system (staff and agents alike; one per Django `User`, `OneToOneField`)
- **Identity/role**: `role` (`agent`/`admin`), `role_type` (12 choices — agent-side: `training`, `incubation`, `regular_agent`, `kill_team`, `night_shift`; admin-side: `supervisor`, `qa`, `cs`, `tester`, `sms_email`, `coordinator`, `trainer`), `status` (`active`/`inactive`), `agent_name` (display/call-center name — distinct from the Django legal name), `employee_id` (unique, nullable), `start_date`, `termination_date`, `adherence_start_date` (nullable, migration `0049` — floor-only first-appearance week on the Adherence tab; see HANDOFF.md §7 items 24–27 for the caveats).
- **Org**: `supervisor` — self-FK (`related_name='direct_reports'`), `employer` (`LCC`/`Infinity`, default Infinity), `billing_status` (`Billed`/`Not Billed`, informational), `track_attendance` (gates dashboard tallies + Records).
- **Contact**: `phone_country_code` (`+1` or `+52`, default `+1`), `phone_number`.
- **Legacy**: `five9_username`/`five9_password` — dead fields, superseded by `Five9Profile`; only a migration ever read them.
- **Pay**: `hourly_rate` (MXN, default 62.50), `billing_rate_usd` (per-agent override of the global USD rate).
- **Permission flags** (no Django groups/permissions used anywhere — access is these flags + `role`/`role_type` + middleware):
  - `is_official_admin` — payroll classification, not itself a permission. Excludes the agent from the regular Adherence/Codings tabs; routes them to Admin Adherence/Admin Codings instead; gives them the fixed **admin bonus** instead of the adherence bonus.
  - `is_super_admin` — full Finance access + can grant super admin to others. Also implies admin-tabs access.
  - `can_access_admin_tabs` — access to Admin Codings/Admin Adherence *without* full Finance access. A non-super-admin holder is **team-scoped**: sees only their own direct reports (+ self) among Official Admins; a super admin/superuser sees everyone. Added via migration `0046_agent_can_access_admin_tabs`.
  - `can_auto_code_requests` — super-admin-only. When set, approving this agent's **coding request** creates the `Coding` automatically instead of a supervisor entering it by hand (see §6 and §10). Default `False`; added via migration `0050_agent_can_auto_code_requests`. Stripped server-side for non-super-admins by the same `AgentForm` `can_grant_admin_tabs` pop() that gates `can_access_admin_tabs` — a crafted POST is discarded at save, and editing as a non-super-admin preserves an existing `True` rather than resetting it.
  - `admin_bonus_mxn` — per-agent override of the global default admin bonus.
- **Property**: `separation` — the latest non-cancelled `AgentSeparation` for this agent, or `None`.

### `Coding` (adherence/models.py)
Manual credited-time block: `agent`, `date`, `start_time`, `end_time`, `notes`, `is_admin_coding` (splits regular Codings-tab entries from Finance-only admin entries — regular Codings/Adherence queries always exclude `is_admin_coding=True`). Cannot cross midnight (UI-enforced). Helper methods compute duration (`total_hours`, `total_hhmmss`). Created through the single shared path `adherence.views.create_coding` since `d4fd7d6` — see §6 for the recompute rule it owns.

### `AdherenceRecord` (adherence/models.py)
One row per agent per day: `status` (17-value `STATUS_CHOICES` — see §6.1), `actual_hours` (computed, written by the daily NR-deduction pipeline — see §5), `notes` (unused field; real per-cell notes live in the separate `AdherenceNote` model), `unique_together (agent, date)`.

### `DailyAgentHours` / `DailyUpload` (adherence/models.py)
`DailyUpload` = one Five9 "Daily Hours" CSV per calendar date (`unique=True` on `date`; re-uploading replaces it). `DailyAgentHours` = one row per Five9 username in that upload: `login_seconds`, `not_ready_seconds`, `agent` (nullable FK — unmatched rows stay in the table for later re-matching), `five9_username`.

### `Shift`/`ShiftBlock` vs `ShiftTemplate`/`ShiftTemplateBlock` (scheduling/models.py)
`ShiftTemplate` = recurring weekly schedule, effective-dated (`effective_from`/`effective_until`; `NULL` `effective_from` behaves as "forever"). `Shift` = a specific-date override that always wins over any template. Both support up to 3 time blocks/day via their `*Block` child tables (split shifts). "Best template resolution" (override beats template; among templates covering the date, latest `effective_from` wins) is duplicated in **six** places: the canonical `scheduling.views._best_shift_template` (reused twice, including an import into `erlang/views.py`), plus five independent reimplementations — `shift_list` and `shift_week` in `scheduling/views.py`, and `_build_maps`, `_effective_template` (module-level as of `577774e`, previously a closure nested inside `_build_rows`), and `agent_my_adherence` in `adherence/views.py`. A known consistency risk, not consolidated as part of this work — see Quirks in HANDOFF.md.

### `PayrollAdjustment` (adherence/models.py)
Per agent-week: `commission_deduction` (%). **Known limitation**: this value is displayed on Payroll but never actually subtracted from `total_pay_mxn` — commission tracking is explicitly unfinished.

### `Five9Profile` (scheduling/models.py)
Multiple Five9 logins per agent: `five9_username`/`five9_password`, `label`, `role_type`, `billable` (drives whether this account's hours count toward billing/payroll), `is_primary` (used for CSV matching/display). Replaces the legacy `Agent.five9_username` field.

### `AgentSeparation` (scheduling/models.py)
`separation_type` (`quit`/`terminated`/`abandonment`/`contract_end`/`resigned_notice`), `status` (`in_progress`/`finalized`/`cancelled`), `last_day_worked`, `remove_from_adherence_date` (the week the agent stops appearing on Adherence/billing/payroll; must be a Monday, **enforced server-side** in `process_separation` and `update_separation` as of `d167897` via a server-rendered dropdown of labeled Mondays rather than a free date input — legacy rows written before `d167897` can still hold a non-Monday value and continue to load and save unchanged), audit fields (`processed_by`, `finalized_by`). Finalizing cascades: sets agent inactive, closes the `EmploymentPeriod`, cancels future OT/role-changes, auto-rejects pending requests, auto-codes the remainder of the last week (Quit/NCNS depending on type). No un-finalize flow exists.

### `BillingSettings` / `BillingSettingsHistory` (finance/models.py)
`BillingSettings` is a **singleton** (`BillingSettings.get()`) holding every rate: `billing_rate_usd` (15.00), `usd_to_mxn` (17.0), `nr_cap_regular_hours` (6.00), `nr_cap_kill_team_hours` (7.00), `default_admin_bonus_mxn` (500.00), `adherence_bonus_max_mxn` (400.00), `adherence_bonus_full_hours` (40.00), `nr_ratio` (0.1250), `default_tardy_hours` (0.25). Saving from Finance → Settings snapshots the current values into `BillingSettingsHistory` keyed by an effective Monday; `BillingSettings.get_for_week(week_start)` returns the most-recent snapshot at or before that week (falling back to the singleton), so historical weeks always recompute with the rates in force at the time.

---

## 4. Navigation / Tabs

Everyone has exactly one `User` + one `Agent`. Access is entirely by `role`/`role_type` + the boolean flags + middleware — there is no Django groups/permissions usage.

**Portal users** (`role='agent'` of any type, plus admins with `role_type` in `PORTAL_ADMIN_TYPES = {cs, tester, sms_email}`) are hard-locked by `AgentAccessMiddleware` to path prefixes `/agent/`, `/adherence/my/`, `/accounts/`, `/static/`; anything else redirects to My Shifts. Inactive portal users are forced to `/agent/inactive/`.

| Tab | URL | Who | View / notes |
|---|---|---|---|
| Dashboard | `/` | Staff | `scheduling.views.dashboard` — pending requests, today's attendance tallies, missing-adherence agents |
| Users | `/agents/` | Staff | `scheduling.views.agent_list` — add/edit/delete, detail page `/agents/<pk>/`, history `/agents/<pk>/history/`. Excel export via `?export=1` with a **column-picker popup** (see §7) |
| Shifts | `/shifts/` , `/shifts/week/` | Staff | `shift_list`, `shift_week` — weekly grid + per-agent editor, templates/overrides/date-ranges |
| OT Shifts | `/overtime/` , `/overtime/week/` | Staff | `overtime_list`/`overtime_week` — assigned OT grid, Open Shifts posting, claim/cancel approvals |
| Available OT / My OT Shifts | `/agent/available-ot/`, `/agent/my-ot-shifts/` | Portal | Agent-side OT claim board + own OT list with cancel-request |
| Adherence | `/adherence/` (`adherence_dashboard`) | Staff | `adherence.views.adherence_week` — regular roster only (Official Admins excluded) |
| Admin Adherence | `/admin-adherence/` (root-mounted) | Super admins + `can_access_admin_tabs` holders | `finance.views.admin_adherence` / `admin_adherence_export` — **moved out of `/finance/` nav in a 5-part migration** (see §10); team-scoped for non-super-admin holders |
| Codings | `/adherence/codings/` | Staff | `adherence.views.codings_week` — regular (non-admin) codings only |
| Admin Codings | `/admin-codings/` (root-mounted) | Super admins + `can_access_admin_tabs` holders | `finance.views.admin_codings` |
| Daily Hours | `/adherence/daily/` | Staff | `adherence.views.daily_hours_week` — Five9 Daily CSV upload/re-match/delete |
| My Adherence | `/adherence/my/` | Portal | `agent_my_adherence` |
| Staffing | `/erlang/` | Staff | `erlang.views.erlang_calculator` — upload, grid, save/download reports |
| Requests | `/requests/` , `/requests/mine/` | Staff | `requests_list` (Team Requests) / `staff_my_requests` (My Requests) |
| My Requests | `/agent/my-requests/` | Portal | `agent_my_requests` |
| Records | `/records/`, `/records/hours/`, `/records/roles/`, `/records/separations/` | Staff | Read-only filtered lists + CSV export |
| Activity | `/activity/` | Staff | `activity_log` — last 500 `AuditLog` entries |
| Finance | `/finance/` | **Super admins only** (`finance_access_required` — `is_super_admin` or Django `is_superuser`) | `finance_dashboard` |
| Billing Report | `/finance/billing/`, export `/finance/billing/export/`, v2 export `/finance/billing/v2/export/` | Super admin | `billing_report` / `billing_export` / `billing_export_v2` |
| Payroll Report | `/finance/payroll/`, export `/finance/payroll/export/` | Super admin | `payroll_report` / `payroll_export` |
| Settings | `/finance/settings/` | Super admin | `finance_settings` — per-week effective-dated rate changes |
| (Finance) Export Adherence | `/finance/adherence/export/` | Super admin | `finance.views.adherence_export` |
| (Finance) User Setup Audit | `/finance/user-audit/export/` | Super admin | `finance.views.user_audit_export` |
| (Finance) Codings export | `/finance/codings/export/` | Super admin | `finance.views.codings_export` |

Access-control functions worth knowing: `finance._has_finance_access` / `finance_access_required` (is_super_admin or superuser); `finance._has_admin_tabs_access` / `admin_tabs_access_required` (is_super_admin, can_access_admin_tabs, or superuser); `finance._admin_tabs_access(user)` returns `(has_access, team_pks)` — `team_pks=None` means "see everyone," a set means "see only these agent pks" (own direct reports + self). OT posting/approval gating is `_is_ot_approver` (role_type supervisor/coordinator, super admins, superusers) in `scheduling/views.py`.

---

## 5. The Hours/Payroll Engine

Core function: **`finance.views._get_billable_weekly_data(agents, week_dates, settings)`** (finance/views.py). For each agent it pulls raw `DailyAgentHours` (login/not-ready seconds) restricted to that agent's **billable** Five9 usernames, sums coded hours from exactly one path per person (Official Admins from their **admin** codings, everyone else from **regular** codings, never both — the `is_admin_coding`/`is_official_admin` partition), and applies the weekly NR deduction:

- **One weekly allowance, not two competing checks**: `nr_allowed = min(nr_cap, connected × nr_ratio)`, where `connected` is the pre-deduction total (login + coded) and `nr_ratio` defaults to 12.5%. Cap = `nr_cap_regular_hours` (6h) normally, `nr_cap_kill_team_hours` (7h) for `kill_team` role type.
- **VTO-raises-to-cap rule**: if any day that week has a VTO-type status (`VTO_TYPE_STATUSES = {VTO, P+VTO, T+VTO}` in `wfm/constants.py`), the allowance becomes the flat cap for that agent-week instead of the ratio.
- **Deduction**: `max(0, weekly_NR − nr_allowed)`. There is **no 48-hour threshold and no larger-of-two comparison** — `nr_ratio_max_hours` was removed from both `BillingSettings` and `BillingSettingsHistory` in migration `0008` (2026-08-05), and the ratio is now taken against connected time rather than raw login.
- `final_hours = max(0, login + coded − deduction)` — this is the number that drives both billing and payroll.

This is computed **from raw login seconds each time**, independent of the daily-adjusted `AdherenceRecord.actual_hours` (a separate, adherence-tab-only pipeline — see §6). `_get_billable_weekly_data` is reused by `billing_report`, `billing_export`, `payroll_report`, `payroll_export`, `billing_export_v2`, and `finance_dashboard`.

Billing (Infinity → LCC, USD): `billing_usd = final_hours × rate` where rate = per-agent `billing_rate_usd` override or the global rate. `billing_report`/`billing_export` (v1) only include agents with at least one billable Five9 profile; separated agents drop off once `remove_from_adherence_date <= week_start`. **`billing_export_v2` has a different roster**: the pay-window predicate (§8) plus `.exclude(employer='LCC')`, with no billable-Five9 narrowing at all — an agent with no billable Five9 profile still gets a row, zero-filled, rather than being excluded.

Payroll (MXN): base = `final_hours × hourly_rate` (per-agent MXN), plus OT incentive top-ups (only `status='completed'` OT shifts — Time & a Half adds `0.5×rate×incentivized_hours`, Power Hour adds `1.0×rate×incentivized_hours`), plus adherence bonus or admin bonus. Commission deduction is stored but not subtracted (see `PayrollAdjustment` note above).

---

## 6. Adherence Engine

Core functions: **`adherence.views._build_maps(agents, week_dates)`** and **`adherence.views._build_rows(agents, week_dates, shift_map, record_map, coded_map, ot_map=None, extra_hrs_map=None, split_labels_map=None, tmpl_by_agent_dow=None, billing_settings=None)`**.

- `_build_maps` gathers, per agent per day: the resolved shift (override-beats-template lookup), the `AdherenceRecord` status, coded hours (non-admin), OT shifts, and template lookups needed for scheduled-hours computation. **Scheduled hours are computed on the fly from these maps — never stored** on a model.
- `_build_rows` turns those maps into the actual per-agent weekly display/report rows: it zeroes scheduled hours for `SCHED_HOURS_ZEROING_STATUSES = {VTO, LOA, V}` (per §1's rule: an agent isn't expected to work that day even though a shift exists), computes green/red met-vs-shortfall variance against scheduled hours, determines bonus qualification from `BONUS_QUALIFYING`/`BONUS_DISQUALIFYING` (`wfm/constants.py`), and re-applies the weekly NR cap on top of the daily-adjusted hours for display. Note: this "Scheduled Hours" total **includes overtime** (`cal_sched = sched_hrs + ot_hrs + spill_hrs`) — a day whose only schedule is an OT shift still counts toward it; pinned by `test_shift_hours_excludes_overtime` in `finance/tests.py`.
- `_build_rows` also returns a `shift_hours` key per row: the raw weekly total from the agent's regular schedule only, gated by the same `is_scheduled_day` condition as `sched_hours` (so both totals always cover the same set of days) — but never zeroed by `SCHED_HOURS_ZEROING_STATUSES` and excluding all overtime. Powers the "Shift Hours" column on `finance.views.adherence_export`.
- **As of `577774e`, this sum is no longer accumulated inline.** `_build_rows` delegates to a new module-level `adherence.views._compute_shift_hours(agent_pk, week_dates, shift_map, ot_map, extra_hrs_map, tmpl_by_agent_dow)` — the single implementation of this calculation. Overtime (including OT spillover from the previous day) still determines the `is_scheduled_day` gate; it just never contributes to the summed hours — that distinction is preserved exactly from the original inline logic and is easy to break if this function is ever "simplified." `_effective_template` (the previous-day template lookup this needs) was promoted in the same commit from a closure nested inside `_build_rows` to a module-level function taking `tmpl_by_agent_dow` explicitly, so the new function could reuse it instead of writing a seventh copy of best-template resolution (see §3).
- `finance.views.billing_export_v2` reuses `_build_maps` + `_compute_shift_hours` directly for its own "Shift Hours" column — never `_build_rows`, which also carries bonus eligibility, NR re-capping, and variance math that has no business in a money export. Because both the combined Adherence export and Billing v2 now call the exact same `_compute_shift_hours` on the exact same maps, the two reports are structurally incapable of disagreeing on this number.
- These same two functions (`_build_maps`/`_build_rows`) are reused directly by `finance.views.admin_adherence` (Official Admins, admin codings) and `finance.views.adherence_export` (a **combined** export merging the regular Adherence roster with Official Admins from Admin Adherence, "sourcing every value from `_build_maps`/`_build_rows` exactly as both on-screen tabs already do — no new adherence/bonus/scheduling math," per the code's own docstring).

Daily NR deduction (writes `AdherenceRecord.actual_hours`, distinct from the weekly Finance math above): applied per day whenever a Daily Hours CSV is uploaded (and re-applied when codings change):
```
total      = login_seconds + coded_seconds (non-admin codings)
allowance  = total × nr_ratio        (12.5% default)
excess_NR  = max(0, not_ready_seconds − allowance)
final      = max(0, login_seconds − excess_NR)  →  actual_hours
```
As of the most recent commit (`b2a4c8b`, "Fix stale actual_hours surviving Daily Hours delete/replace"), there's a `_reconcile_stale_actual_hours(upload_date)` helper wired into upload/re-match/delete of Daily Hours, all wrapped in `transaction.atomic()`, so deleting or replacing an upload no longer leaves stale `actual_hours` behind for agents who dropped off the new file. This is a **display-layer fix only** — billing/payroll never read `actual_hours`; they always recompute from raw `DailyAgentHours` seconds independently (see §5, Known Quirk #2 conceptually).

The recompute triggered by a coding change is `adherence.views._refresh_actual_hours`, and since `d4fd7d6` the one place that fires it on creation is the shared `adherence.views.create_coding` (used by both the Codings tab's `add_coding_ajax` and the coding-request auto-code hook). **It runs for regular codings only.** `_refresh_actual_hours` sums `is_admin_coding=False` rows exclusively, so calling it for an admin coding would not even include the new row — it would simply write an `AdherenceRecord` for an Official Admin who should not have one. `finance.views.add_admin_coding_ajax` deliberately never recomputes either. See HANDOFF.md §7 item 52.

---

## 7. Existing Exports

All Excel (`.xlsx`, via openpyxl) unless noted. Every export below calls `log_action(...)` to the Activity Log **except the two Nómina exports**, which write nothing to it.

| Export | View | Produces |
|---|---|---|
| Billing Report | `finance.views.billing_export` | Per-agent USD billing, grouped by employer then role type, with subtotals |
| Billing Report v2 | `finance.views.billing_export_v2` | Detailed per-agent time breakdown (login/NR/coded/connected/allowed-NR/deduction/final) in HH:MM:SS + decimal, formatted like a raw payroll worksheet. Includes a **Shift Hours** column (position D, immediately after the identity columns, ahead of the login/NR/deduction chain) — same definition and same underlying `_compute_shift_hours` calculation as the combined Adherence export's Shift Hours column |
| Payroll Report | `finance.views.payroll_export` | Per-agent MXN payroll split Infinity vs LCC Direct, base + top-ups + bonus |
| Admin Adherence export | `finance.views.admin_adherence_export` | Official-Admins-only adherence/payroll export, admin-bonus column |
| Combined Adherence export | `finance.views.adherence_export` | Regular Adherence roster + Official Admins from Admin Adherence, one workbook, sourced exactly from `_build_maps`/`_build_rows`. Both the "Adherence" and "VTO Agents" sheets include a **Shift Hours** column (position 8, immediately after Scheduled Hours) — raw regular-schedule hours Mon–Sun, excluding overtime, not zeroed by VTO/LOA/V |
| Codings export | `finance.views.codings_export` | One-row-per-agent weekly coding matrix (regular + admin combined), Mon–Sun day totals + weekly total; read-only, excludes `employer='LCC'` agents |
| User Setup Audit export | `finance.views.user_audit_export` | One row per Five9 account (or one zero-account row per person) for every active/inactive user — legal name, agent name, employee ID, contact, role/permission flags, effective billing/hourly rates + rate source, team password, all Five9 credentials. Super-admin, read-only |
| Users export | `scheduling.views.agent_list` (`?export=1`) | **Column-picker popup** (added same-day, three commits: `faf1837`, `6bfe156`, `ba5eba8`) — 16 available fields (`USER_EXPORT_FIELDS`): Agent name, Legal name, Username, Email, Employee ID, Employer, Role, Role type, Status, Supervisor, Primary/All Five9 usernames, Start date, Years with us, Full phone number, Hourly rate (MXN). Defaults to the classic 8-field set (`USER_EXPORT_DEFAULTS`) if nothing is picked. **Financial columns gated to super admins**: `USER_EXPORT_FINANCIAL = {'hourly_rate'}` is hidden from the picker for non-super-admins and stripped server-side even if crafted into the query string. Respects current status/role/supervisor filters and the same active-OR-pay-window rule as Finance |
| OT payroll export | `scheduling.views.overtime_export` | OT shift payroll CSV/export from the OT Shifts tab |
| Records exports | `scheduling.views.records_*` | CSV exports from each Records sub-page (Attendance, Hours, Role Log, Separations) |
| Staffing reports | `erlang.views.erlang_download` / `erlang_save_report` | Save/download Erlang-C calculator snapshots |
| Agent Nómina | `nomina.views.agent_export` | Two sheets — `Yours` (raw, with a Notes column) and `Mine` (corrected) — 19 columns of weekly agent payroll in MXN. Super-admin; see §11.10 |
| Admin Nómina | `nomina.views.admin_export` | One sheet (`Yours`, with Notes) — 17 columns of Official-Admin payroll in MXN, two Prestamo columns. Super-admin; see §11.10 |

---

## 8. Key Business Rules

- **Pay-window rule for recently-separated agents**: an agent counts toward Adherence/billing/payroll/exports if `status='active'` **OR** (`status='inactive'` AND a `finalized` separation with `remove_from_adherence_date > week_start` for the week being viewed). This exact predicate is duplicated across `billing_report`, `billing_export`, `payroll_export`, `codings_export`, `agent_list`'s export, `adherence._get_adherence_agent_pks`, and `nomina.views._pay_window` (plus a deliberate inactive-only half inside `_agent_nomina_data`) — always Monday-of-week comparisons.
- **Week model**: always Monday–Sunday; `week_start` params are snapped to Monday everywhere (`wfm.utils.get_week_start`/`parse_week_param`).
- **USD billing vs MXN pay**: Infinity bills LCC in USD (`billing_rate_usd`, per-agent override or global); agents are paid in MXN (`hourly_rate`); `usd_to_mxn` converts for USD-equivalent payroll display. Both rates are per-week-versioned via `BillingSettingsHistory`.
- **Bonus qualifying/disqualifying statuses**: qualifying = `{P, OT, MUT, VTO, P+VTO, V, Holiday}`; disqualifying = `{Absent, NCNS, T, T+VTO, T+I, I, LOA, S, Issues}` (`wfm/constants.py`). Any disqualifying status anywhere in the week (including an OT shift marked No Show) kills the whole week's bonus. Official Admins never get the adherence bonus — they get the fixed admin bonus instead.
- **Scheduled-hours zeroing**: `SCHED_HOURS_ZEROING_STATUSES = {VTO, LOA, V, Holiday}` zero the day's scheduled hours since the agent wasn't expected to work; `P+VTO` and `T+VTO` instead cap the day at the hours actually worked.
- **VTO raises the weekly NR cap to the flat allowance**, bypassing the 48h ratio check for that agent-week.
- **`is_admin_coding` and `is_official_admin` are the hard partition** between "regular" (Codings/Adherence) and "admin" (Admin Codings/Admin Adherence) data — never mix the two query paths.
- **Team-scoping for `can_access_admin_tabs` holders** (non-super-admin): limited to their own direct reports + self among Official Admins; super admins/superusers see everyone. This is enforced server-side in `_admin_tabs_access`, not just hidden in the UI.
- **Financial export fields are super-admin-gated server-side**, not just hidden in the UI (see `USER_EXPORT_FINANCIAL` pattern in §7) — the intended pattern for any future financial field added to a non-Finance export.
- **`db.sqlite3` is gitignored and untracked** — it is *not* committed. It was tracked historically and deliberately removed in `ed1022e` ("Add .gitignore; stop tracking db.sqlite3"). Local DB state does not travel with the repo: every developer has their own file, and a local database can be missing migrations that are already applied in production (or vice versa) — check `python3 manage.py showmigrations` rather than assuming. Production data lives in Render Postgres; never assume local DB state reflects it.
- **Full known-quirks list lives in HANDOFF.md §7**, not duplicated here — it's the single source of truth for implementation-level gotchas (orphaned production columns, caching lag, and admin-flag edge cases).

---

## 9. Standing Build Principles (David's standing instructions, since 2026-07-08)

1. **Simple first** — always the simpler way; never over-engineer.
2. **Clean UI always** — polished, uncluttered; if an addition makes a page busier, find a better presentation.
3. **User-friendly above everything** — the users are busy, non-tech-savvy ops coordinators/supervisors on a live call floor; minimize clicks.
4. **Never break what works** — verify existing behavior after every change.
5. **Less is more** — when in doubt, leave it out.
6. **Consistency** — match existing styles/patterns exactly (badge/card/modal/status-pill conventions); no new UI patterns.
7. **Performance** — fast loads, no full-page reloads when a small in-place AJAX update will do.

*Why this matters*: the product runs a live call-center floor; complexity and clutter cost real operational time. Recite these before proposing any new feature or UI surface.

---

## 10. Recent Work (most recent first, as of `e2509eb`)

1. **Fix the Adherence 502: split the roster query's four-way join fanout** (`86ab564`) — `/adherence/rows/` hung ~110s and returned 502; the combined Adherence export and the grid's bulk save shared the fault. `_get_adherence_agent_pks` expressed its activity gate as one `filter()` with four OR'd conditions across four multi-valued relations, so each got an unrestricted `LEFT JOIN` and every date predicate fell into the `WHERE` — the database materialised (every shift) × (every OT) × (every template) × (every adherence record) per agent: 502,948,740 rows to return 93 integers. **Not an indexing problem** — every useful index already existed; the cost was the join shape. Now two steps: an eligibility query, then four scoped activity queries unioned in Python. Signature, cache key, 300s TTL, `set` return type and all three callers unchanged; no migration. Same pk set, verified in production across 8 consecutive weeks, all identical. Tests: 441 → 461. See HANDOFF.md §7 items 54–56 for the three rules this leaves load-bearing.
2. **Read-only schedule/adherence data inventory command** (`6877c40`) — `schedule_data_inventory` prints row counts, earliest/latest date and future-dated counts for `Shift`, `ShiftBlock`, `ShiftTemplate`, `ShiftTemplateBlock`, `OvertimeShift`, `AdherenceRecord`, `Coding` and `DailyAgentHours`, plus ShiftTemplate-rows-per-agent and OT-duplicate-hotspot distributions. Counting only, no write transaction, not reachable from any request path. Production was measured with it on September 1, 2026 — volumes are small and healthy, and that run is the baseline.
3. **Read-only Adherence roster parity command** (`e2509eb`) — `verify_adherence_roster` holds the pre-`86ab564` single-query implementation verbatim, runs it and the current helper against the same database week by week, and prints any difference in the pk sets by agent name and status. `--weeks` (default 8); exits 1 on a mismatch so it can gate a deploy. This is what confirmed the production parity above. The helper decides who appears on the Adherence tab and in the combined Adherence export, so a changed pk set would silently drop agents from the tab and their adherence bonus with nothing warning anyone.

4. **Auto-code approved coding requests for permitted agents** (`d4fd7d6`) — approving a coding request from an agent with `can_auto_code_requests` now creates the `Coding` automatically instead of a supervisor retyping it on the Codings tab. Coding creation was extracted out of `add_coding_ajax` into a new module-level `adherence.views.create_coding` — one implementation, two callers, the same kind of shared-helper extraction as `_compute_shift_hours` in item 13. `is_admin_coding` mirrors the agent's `is_official_admin`, so an Official Admin's row lands in Admin Codings and a regular agent's in Codings, preserving the partition. `_refresh_actual_hours` runs for **regular codings only**: it sums non-admin codings exclusively, so firing it for an admin coding would write an `AdherenceRecord` that should not exist — matching `finance.views.add_admin_coding_ajax`, which never recomputes either. The approval runs the existing shared path unchanged and the auto-code hooks in *after* its bookkeeping, so `request_approve` is insertion-only (+8 lines, 0 removals, verified by AST hash against the previous commit). A failed auto-code leaves the request **approved** with a warning to code it manually — the approval is never rolled back. The request is auto-marked done with `done_by` = the approver; provenance is carried in `auto_action_log` and the coding's `notes`. No date limit and no status/separation check: the pay-window predicate is deliberately **not** applied here, since an agent may be owed paid time for days they already worked. No billing, payroll, bonus, or NR math changed. Tests: 407 → 416.
5. **Add `can_auto_code_requests` permission flag to `Agent`** (`3408769`, migration `0050_agent_can_auto_code_requests`) — new super-admin-only boolean, default `False`, inert on its own (nothing read it until item 4). Gated by the existing `AgentForm` `can_grant_admin_tabs` pop() pattern that already gates `can_access_admin_tabs`: the field is removed from the form entirely for non-super-admins, so a crafted POST is discarded at save rather than merely hidden, and editing as a non-super-admin preserves an existing `True` instead of resetting it to the model default. Both behaviors are pinned by tests. Tests: 404 → 407.
6. **Coding requests: typed 24-hour HH:MM:SS time entry** (`44c027e`) — the agent and staff coding-request forms replaced native `<input type="time">` pickers with plain typed text inputs (`H:MM:SS`, seconds optional), matching how the Codings tab already works. Server-side validation in `_fill_request_from_post` moved from Django's lenient `parse_time` to the Codings tab's zero-pad-hour + `time.fromisoformat` + end-after-start check, so a malformed or backwards time can no longer be stored; that end-after-start comparison is also what enforces "cannot cross midnight." **Coding branch only** — every other request-type branch is unchanged. Request detail renders coding times as `H:i:s` so an approver sees the exact stored value; `summary()` and the Team Requests list stay on `H:i`. Tests: 401 → 404.
7. **Fix recurring shift saves not taking effect + add save-path guardrails** (`2ecb0c7`) — a Recurring save in the Shifts editor appeared to do nothing in the week on screen: a specific-date `Shift` override always beats a `ShiftTemplate`, and the Recurring branch of `shift_week` wrote the new template correctly but never touched `Shift` rows already sitting in that week (left behind by a prior One-time save, Copy-prev-week, or the Adherence quick-edit modal), so the change looked like it only took effect the following week. A Recurring save now also deletes the agent's `Shift` overrides inside the week on screen, from the chosen effective date forward, never touching another week. Two guardrails landed with it: the Permanent/One-time radio now defaults off `agent_has_recurring` (does the agent have a `ShiftTemplate` at all) rather than `has_any_template` (does one cover the week on screen), which could silently pre-check Permanent on a past week and wipe forward schedule history via `_save_shift_template`; and `shift_week` now rejects a missing or unrecognized `edit_type` POST value server-side instead of defaulting to `'permanent'`. A Permanent save also now shows a confirmation dialog when the effective date is retroactive and/or the save would remove an approved day-off request, driven by server-rendered `day_off_warnings` read by the page's own JavaScript. The Shifts save path had zero automated test coverage before this fix; see `ShiftSaveTests`. Tests: 381 → 401. Full detail in HANDOFF.md §7 items 31 and 43–48.
8. **Replace `remove_from_adherence_date` free date input with a dropdown of Mondays; add server-side Monday validation** (`d167897`) — the field controls the first week a separated agent stops appearing on Adherence, Codings, billing, and payroll; it was a free `<input type="date">`, Monday by convention only, so a non-Monday value silently behaved as the following Monday — this caused a production incident. Both the Process Separation and Finalize Separation modals now render a plain server-rendered `<select>` of labeled Mondays (four weeks back through eight weeks forward) via a new `wfm.utils.get_monday_choices()` helper, anchored on `timezone.localdate()`. `process_separation` and `update_separation` each got a server-side Monday check added to their existing `errors`-list/`messages.error` pattern — validation is view-layer only, never on the model, so legacy non-Monday rows already in production continue to load and save unchanged. Removed the now-redundant "Next Monday"/+2/+3-week shortcut buttons; kept the auto-fill on Last Day Worked. Label and help text now state plainly that the date also removes the agent from billing and payroll. Tests: 376 → 381.
9. **Pay separated agents correctly for their final pay window** (`4f23ec5`) — `_finalize_separation` permanently sets `track_attendance=False`, which broke two payroll surfaces for a separated agent's own final week. In nómina, the non-billable-overpay guard in `_agent_nomina_data` zeroed base pay, bonus, and hours for any untracked agent with no billable Five9 profile, catching separated agents alongside the sales-type agents it was written for; it now skips zeroing when the agent is inactive, inside their pay window, and has non-admin `Coding` rows for that week. It deliberately uses only the inactive half of the pay window, never `_pay_window()` itself, so active untracked agents are still zeroed. In finance, `payroll_report` and `payroll_export` required `track_attendance` or a billable Five9 profile as a base filter, so a separated agent with neither never reached the pay-window exclude; a third OR branch was added to both. No pay, bonus, or hours math changed. Tests: 369 → 376.
10. **Fix Codings roster dropping finalized-separated agents from past weeks** (`62f1be4`) — `codings_week` filtered agents on `status='active'` only, so finalizing a separation removed the agent from every past week's Codings grid, including weeks they actually worked. Caused a real production loss of billing/payroll visibility for the week of 2026-07-27. Fix applies the same pay-window carve-out `_get_adherence_agent_pks` already uses, written inline as a sixth copy of the predicate (per existing convention — not extracted), using the two-step pk-collection pattern to avoid duplicate rows fanning out through the separations join. `is_official_admin=False` on both branches preserves the Codings/Admin Codings partition. Purely additive: no agent visible before this change can be removed by it. Tests: 367 → 369.
11. **Add per-agent Adherence start date floor** (migration `0049_agent_adherence_start_date`, commit `a8e6a11`) — new nullable `Agent.adherence_start_date`, a floor-only control on the first week an agent can appear on the Adherence tab (it can remove an agent from a week, never add one; `NULL` preserves prior behavior exactly). Validated server-side as a Monday in `AgentForm.clean_adherence_start_date` on both create and edit. Consumed by the single shared `_get_adherence_agent_pks` helper, so `finance.views.adherence_export` stays consistent with the screen automatically. See HANDOFF.md §7 items 24–27 for the caveats this leaves behind (activity gate isn't date-scoped, 5-minute cache lag, no UI indicator when an agent is floored out). Tests: 336 → 353 (the bulk of that increase is from `nomina/tests.py`, not from this change).
12. **Fix Agent creation 500 in production: default the orphaned `can_access_admin_attendance` column** (migration `0048_set_default_admin_attendance_column`, commit `d3ded47`) — Postgres was rejecting every Agent `INSERT` with `null value in column "can_access_admin_attendance" violates not-null constraint`, breaking agent creation for all non-superuser supervisors. Root cause: the fully-reverted Admin Attendance feature (item 25 below) had already applied its `AddField` migration (originally `0046_agent_can_access_admin_attendance`) to production before the revert shipped. Django's `AddField` adds the column with a temporary default, backfills existing rows, then strips the default — so the column stayed `NOT NULL` with no database-level default even after the migration file and model field were deleted. UPDATEs never touch the column, so editing an agent worked the whole time, which is why this went unnoticed: local SQLite never had the migration applied so it couldn't reproduce locally, and `makemigrations --check` reported no drift because the models and migration files genuinely agreed — the mismatch was between the code and the production database, visible only in the Render Postgres log. Fix: migration `0048` sets a Postgres-only `ALTER COLUMN ... SET DEFAULT false` if the column exists (no-op on SQLite); it does not drop the column or touch the model, since the column is deliberately preserved in case the feature is re-landed. Verified in production: agent creation confirmed working again for a previously-blocked supervisor account. See HANDOFF.md §7 items 19–22 for the related quirks this leaves behind. Tests: 336/336 passing (unchanged).
13. **Add Shift Hours to Billing Report v2 export; extract shared computation** (`577774e`) — new column D on Billing v2, same definition as the Adherence export's Shift Hours (raw regular-schedule hours Mon–Sun, excluding overtime, not zeroed by VTO/LOA/V). Extracted the summation out of `_build_rows` into a new module-level `adherence.views._compute_shift_hours` — the single implementation both exports now call, so they cannot drift — and promoted `_effective_template` from a closure nested in `_build_rows` to a module-level function so the new helper could reuse it. Tests: 222 → 227.
14. **Add "Shift Hours" column to combined Adherence export** (`4949f7a`) — raw regular-schedule hours Mon–Sun, excluding overtime, not zeroed by VTO/LOA/V, on both the Adherence and VTO Agents sheets. New `shift_hours` key on `_build_rows`' row dict, gated by the same `is_scheduled_day` condition as `sched_hours`.
15. **Fix stale `actual_hours` surviving Daily Hours delete/replace** (`b2a4c8b`) — added `_reconcile_stale_actual_hours(upload_date)`, wired into upload/rematch/delete, all now atomic. Display-layer only; billing/payroll unaffected since they never read `actual_hours`.
16. **Users export fields overhaul** (`ba5eba8`) — added "Agent name" as its own column, renamed "Full name"→"Legal name," dropped Django-split First/Last name columns, replaced "Phone country code" with a single formatted "Full phone number."
17. **Gate financial export columns to super admins** (`6bfe156`) — `USER_EXPORT_FINANCIAL = {'hourly_rate'}`; hidden from the picker and stripped server-side for non-super-admins.
18. **Users export: column-picker popup** (`faf1837`) — turned the fixed 8-column Users export into an 18-field (now 16 after the rename/consolidation above) checkbox picker, defaulting to the classic set, respecting current filters.
19. **Exclude LCC-employer users from three Finance exports** (`9d4c3f3`) — likely Codings/Adherence/Billing-family exports; confirmed on `codings_export` (`.exclude(employer='LCC')`).
20. **Fix admin adherence tab false "missing time" for Official Admins** (`69aae21`).
21. **Atomic agent create/edit transactions** (`30cc78d`) — prevents orphaned Django `User` rows if agent-profile creation fails mid-request.
22. **User Setup Audit export** (`5e807be`) — new super-admin, read-only, one-row-per-Five9-account audit workbook.
23. **Combined "Export Adherence" report** (`412f445`) — merges regular Adherence + Admin Adherence into one workbook via shared `_build_maps`/`_build_rows`.
24. **Admin Codings/Admin Adherence relocation, 5-part migration** (`3a5fbb9`, `494e260`, `3755b60`, `8b09140`, `a55e44e`): moved these two tabs out of the Finance section into root-level URLs (`/admin-codings/`, `/admin-adherence/`), introduced the `can_access_admin_tabs` permission flag (independent of `is_super_admin`/Finance access), added team-scoping so a non-super-admin holder sees only their own Official-Admin direct reports, and locked down server-side edit enforcement to match.
25. An **"Admin Attendance" feature was added then fully reverted** (`c1772c0` → reverted by `4f88bb9`/`f7045d2`) before the relocation work above — worth knowing if old branches/PRs reference "Admin Attendance," since it no longer exists under that name. Its `AddField` migration (`0046_agent_can_access_admin_attendance`) is the one that left the orphaned production column described in item 12.

**Note**: a stray `.claude/worktrees/agent-a2f0b18fec4b3873b/` directory exists in the repo root containing a parallel checkout with slightly older line numbers for `finance/views.py`/`adherence/views.py` — this is agent scratch state, not part of the real app; don't confuse it with the top-level `finance/`/`adherence/` app directories when citing file paths.

---

## 11. The Nómina App (Payroll)

Super-admin-only weekly payroll section at `/nomina/`, built by jholopez. It produces two Excel
files per Monday–Sunday week — `LCC AGENT NOMINA <MMDDYYYY>.xlsx` and
`LCC ADMIN NOMINA <MMDDYYYY>.xlsx` — in the exact column layout of the files the payroll
processor already used. **Infinity employees only**; `employer='LCC'` people never appear.

### 11.1 Roster — who lands on which sheet

Three helpers in `nomina/views.py`, all week-scoped by a local `_pay_window(week_start)` copy of
the §8 pay-window predicate:

- `_infinity_agents(week_start)` → Agent Nómina: `employer='Infinity'`, `.exclude(is_official_admin=True)`.
  Deliberately **no** `track_attendance` / billable-Five9 filter — sales-type agents take no calls
  but earn LPO, so they must appear.
- `_admin_agents(week_start)` → Admin Nómina: `employer='Infinity'`, `is_official_admin=True`.
- `_unrostered_infinity(week_start)` → Infinity people in the pay window on **neither** sheet.
  Rendered as a warning banner on the Agent Nómina so nobody is silently left unpaid.

### 11.2 What Nómina reads from the rest of the system

- **`finance.views._get_billable_weekly_data`** — the single source of computed hours and pay.
  Nómina calls it in `_agent_nomina_data`, `_admin_nomina_data`, `admin_hours` and `overrides`,
  and uses `final_hrs`, `hourly_mxn`, `base_pay_mxn`, `bonus_mxn`, `admin_bonus_mxn`,
  `commission_pct`. It never re-derives any of them.
- **`finance.models.BillingSettings.get_for_week(week_start)`** — historical rates, correctly
  per-week; `settings.nr_ratio` also feeds the holiday not-ready allowance.
- **`adherence.models.DailyAgentHours`** — read directly in `_holiday_worked_hours` only, for
  per-day login/not-ready seconds on holiday dates, scoped by `wfm.utils.get_billable_username_map`.
- **`adherence.models.AdherenceRecord`** — **`status` only**, never `actual_hours`: `'V'` for
  vacation pay and days, `'Holiday'` for the not-worked holiday case, and the full status history
  for `admin_bonus_penalty`.
- **`adherence.models.Coding`** — non-admin rows for the week, used only by the separated-agent
  carve-out in the non-billable guard.
- **`adherence.views._build_maps` / `_scheduled_hours`** — reused for per-day scheduled hours in
  `_vacation_hours`, `_holiday_not_worked_hours` and `_admin_bonus_factors`.
- **`scheduling.models.Agent`** — `employer`, `is_official_admin`, `role_type`, `employee_id`,
  `agent_name`, `hourly_rate`, `track_attendance`, `five9_profiles`, `employment_periods`,
  `start_date`, and the permission flags.

**Nómina honors the two-pipelines rule.** `grep actual_hours nomina/` returns nothing: no money in
this app comes from `AdherenceRecord.actual_hours`. All hours and base pay trace to
`_get_billable_weekly_data`'s recompute from raw `DailyAgentHours` seconds.

### 11.3 Models (`nomina/models.py`, migrations `0001`–`0013`)

| Model | Holds | Keyed by |
|---|---|---|
| `NominaWeek` | `spiff_fx_rate` — the week's USD→MXN rate. Nullable, **no default and no carry-over**; must be entered fresh each week | `week_start` (unique) |
| `WeeklyPayInput` | The manual paste-in columns: `lpo`, `spiff_usd`, `extra_hours`, `hours_add`, `hours_deduct`, `welcome`, `referral`, `kill_team_qa` (nullable), `comedor`, `transportation` | `(agent, week_start)` unique |
| `Holiday` | A company holiday date + optional name. Also read by `adherence` and `finance` | `date` (unique) |
| `Loan` | `principal`, `term_weeks` (1 or 2), `rate`, `start_week`, `granted_by`. `total_owed = principal × rate`; `weekly_repayment = total_owed / term_weeks`; `installment_for_week()` is 0 outside the term | per row |
| `WelcomeBonusEnrollment` | `amount`, `num_weeks`, `start_week`; `covers_week()` is a flat calendar-week window | per row |
| `NominaOverride` | One manual value replacing one computed cell | `(agent, week_start, field)` unique |
| `VacationAdjustment` | Super-admin ± days on a vacation balance | `(agent, year)` unique, where `year` is the **anniversary** year from `_vacation_year` |
| `UnmatchedInputRow` | A file row an upload could not match to an agent, with `acknowledged` | `(week_start, input_key)` |
| `PayrollRun` | The frozen snapshot of a finalized week: `agent_rows`, `agent_yours_rows`, `agent_totals`, `admin_rows`, `admin_totals` (JSON), `finalized_by`, `finalized_at` | `week_start` (unique) |
| `AdminBonusDeduction` | The coder-entered admin-bonus deduction % (0–100) | `(agent, week_start)` unique |
| `BreakAbuseIncident` | One logged break-abuse incident | per row (matched to a week by `date`) |

### 11.4 Agent Nómina money paths (`_agent_nomina_data`)

```
Pay (48)  = base_pay_mxn  [+ extra_hours × rate] [+ vacation_hours × rate]
Subtotal  = Pay (48) + Adherence + Net LPO + Spiff + Welcome + Referral + Kill Team QA + Holiday Pay
Total     = Subtotal − Cafeteria − Transportation − Prestamo        (may go negative)
Hours Worked = final_hrs [+ extra_hours];  Total Hours = Hours Worked [+ vacation_hours]
```
The bracketed terms apply in the corrected ("Mine") variant only.

| Column | Originates | Transformed by | Ends up |
|---|---|---|---|
| Pay (48) | `base_pay_mxn` from the engine (`final_hrs × hourly_rate`) | plus extra-hours pay and vacation pay; `base_pay` override replaces **only** the engine term | Subtotal |
| Adherence | `bonus_mxn` from the engine | forced to 0 by any `BreakAbuseIncident` that week; then the `adherence` override | Subtotal, **and gates the Welcome Bonus** |
| Net LPO | `WeeklyPayInput.lpo` (upload or manual) | "Mine": `× (1 − commission_pct/100)` where `commission_pct` is `PayrollAdjustment.commission_deduction`; "Yours": gross | Subtotal |
| Spiff | `WeeklyPayInput.spiff_usd` (USD, summed across repeat rows) | `× NominaWeek.spiff_fx_rate`; **an unset rate makes every spiff $0** | Subtotal |
| Welcome Bonus | `WelcomeBonusEnrollment.amount` | paid only when `covers_week(week)` **and** that week's Adherence bonus > 0; otherwise falls back to `WeeklyPayInput.welcome` | Subtotal |
| Referral | `WeeklyPayInput.referral` (manual add-list) | — | Subtotal |
| Kill Team QA | `WeeklyPayInput.kill_team_qa` | `NULL` + `role_type='kill_team'` → the $400 default; any stored value, **including an explicit 0**, wins | Subtotal |
| Holiday Pay | `Holiday` dates ∩ the week | `worked_hrs × rate × 2 + not_worked_hrs × rate` | Subtotal |
| Extra Hours | `WeeklyPayInput.extra_hours` (Extra Hours module) | `× rate` into Pay (48); hours into Hours Worked | Pay (48) |
| Vacation | `AdherenceRecord.status='V'` days | `min(scheduled, 8)` per scheduled day, flat 8 on a day off; `× rate` | Pay (48), Total Hours |
| Cafeteria | `WeeklyPayInput.comedor` (POS upload, matched by EMP #) | — | deducted |
| Transportation | `WeeklyPayInput.transportation` (manual add-list) | — | deducted |
| Prestamo | `Loan.installment_for_week(week_start)`, summed | — | deducted |

**Holiday hours.** `_holiday_worked_hours` recomputes per holiday day from `DailyAgentHours`:
`worked = login_h − max(0, nr_h − login_h × nr_ratio)`. A day already marked `'Holiday'` in
adherence (scheduled, not worked) is excluded from the worked set and paid the 1× not-worked way
instead. Because the engine's `final_hrs` already contains the 1× for a worked holiday, the +2×
premium makes it triple.

### 11.5 "Yours" vs "Mine"

`_agent_nomina_data(..., corrected=)` produces two genuinely different sheets from one function.
`corrected=False` ("Yours") ignores every `NominaOverride`, shows **gross** LPO, and excludes
vacation pay and manual extra hours from Pay (48) and the hours columns. `corrected=True` ("Mine")
applies all of them. `_agent_note` diffs the two and writes a plain-English Notes cell on the Yours
sheet ("LPO should be $X", "Agent had N days of vacation, total hours worked should be Y").
**The on-screen Agent Nómina always renders "Mine."**

### 11.6 Admin Nómina (`_admin_nomina_data`)

One sheet, and its numbers are **unmodified** — corrections live in the Notes column, not in the math:

```
Hours     = final_hrs + (hours_add − hours_deduct)
Base Pay  = base_pay_mxn + (hours_add − hours_deduct) × rate      [base_pay override replaces the whole thing]
Subtotal  = Base Pay + Holiday Pay + Spiffs + LPO + Referral + Prestamo given
Total     = Subtotal + FULL admin bonus − Cafeteria − Prestamo repay − Transportation
```
- **Admin bonus** is `admin_bonus_mxn` from the engine (per-agent `Agent.admin_bonus_mxn` or the
  `BillingSettings` default). The `Total` column pays it in full. `admin_bonus_corrected` —
  `max(0, gross × (worked_days / scheduled_days) × (1 − deduction_pct/100))` — is computed and
  stated in the Note only.
- **`AdminBonusDeduction.deduction_pct`** is entered from the **Admin Adherence tab in `finance`**
  (`save_admin_deduction`), not from any Nómina page. `admin_bonus_penalty(agent, week_start)`
  recommends a % from the penalty matrix (Tardy and Incomplete as separate stacking escalation
  tracks, a third Issues track, `Absent`/`NCNS`/`S` = 100%, capped at 100) and is served to that
  tab as JSON by `finance.views.admin_penalty_reco`. It is a guide; the coder types the final %.
- **Two Prestamo columns.** `prestamo_given` credits the loan manager (`Loan.granted_by`) this
  week's repayment for loans they handed out — the one place a loan **adds** money — and is added
  before Subtotal. `prestamo_repay` deducts that admin's own installment. Repayments whose
  `granted_by` is unset or is no longer an official admin are surfaced as
  `uncredited_loans` / `uncredited_repay` in the totals, because the borrower is deducted regardless.
- Admins have no adherence bonus, no Welcome Bonus, and no Kill Team QA column.

### 11.7 Inputs modules and file upload

`/nomina/inputs/` is a hub over the `INPUT_TYPES` registry; each entry maps to one `WeeklyPayInput`
field. Modules cover the **whole roster** — agents and official admins — so one uploaded file
matches everyone.

| Module | Field | Source | Match |
|---|---|---|---|
| LPO | `lpo` | upload or manual grid | username, then employee ID |
| Spiffs | `spiff_usd` | upload (aggregating: repeat rows summed) + additive "add to a person" | username / ID |
| Extra Hours | `extra_hours` | manual add-list + additive add | agents only |
| Referral | `referral` | manual add-list | — |
| Kill Team QA | `kill_team_qa` | upload or grid; `role_type='kill_team'` only | username / ID |
| Comedor | `comedor` | upload (aggregating), real cafeteria POS export | EMP # |
| Transportation | `transportation` | manual add-list | — |
| Admin Hours | `hours_add` / `hours_deduct` | its own page, official admins only | — |

`_read_rows` accepts `.csv` (sniffing `,` / `;` / tab) and `.xlsx`. Columns are found by header
keyword; when the amount header is blank, `_detect_amount_col` scores columns by how many cells
look like money. `_dec` tolerates `$`, thousands commas and `(50.00)` negatives — **anything it
cannot parse becomes 0**. The scalar FX rate uses `_parse_rate` instead, where a comma can only be
a decimal separator, and rates outside `0 < r < 1000` are rejected.

**Upload is wipe-and-replace**: the module's field is zeroed for the entire week's roster first,
then only the file's rows are written. Rows that match nobody become persisted `UnmatchedInputRow`
records that stay on screen until an operator acknowledges each one. Manual Save and the additive
"add more" flow are separate, non-wiping paths.

### 11.8 Finalize / `PayrollRun`

`POST /nomina/finalize/` computes Mine, Yours and the admin rows, bakes the Notes into the snapshot,
and writes one `PayrollRun` for the week. From then on `agent_nomina`, `admin_nomina`,
`agent_export` and `admin_export` read the stored JSON and never recompute — a finalized week's
numbers cannot move even if rates, hours, codings or inputs change afterward.

**Finalizing is irreversible.** There is no un-finalize view, no UI, and no management command; the
only route back is deleting the `PayrollRun` row directly in the database. The button is behind a
JS `confirm()`, and `week_start` is unique so a second finalize is a no-op with an info message.
While finalized, `inputs`, `input_type`, `admin_hours` and `overrides` reject POSTs for that week.

### 11.9 Permissions

`nomina/access.py`, enforced per view with decorators — no Django groups, consistent with the rest
of the app:

- `nomina_access_required` — `user.is_superuser` **or** `agent.is_super_admin`. Guards every
  Nómina view including both exports and `finalize`.
- `loan_access_required` — the above **or** `agent.can_manage_loans`. Guards `/nomina/loans/` only.
- `vacations` (`/vacations/`, root-mounted) is `@login_required` with scoping inside the view:
  `role='admin'` or super admin sees everyone with search + supervisor filter, any other agent sees
  only their own row, and only a super admin can POST an adjustment.
- Nav links match the gates: the Nómina link renders under `request.has_finance_access`
  (which the middleware sets to `is_super_admin`), and a standalone Loans link under
  `request.can_manage_loans`.
- **There is no team scoping in Nómina.** `finance._admin_tabs_access` is not used here; every
  Nómina page shows the whole Infinity roster to anyone who passes the gate.
- `AdminBonusDeduction` is the one Nómina-owned pay input writable outside these gates — its editor
  lives on Admin Adherence, behind `admin_tabs_access_required`.

### 11.10 Exports

Both are `.xlsx` via openpyxl, share `_write_nomina_sheet` (bold header, real numeric cells, `0`
for employee IDs, `#,##0.00` for hours, `$#,##0.00` for money), and **neither calls `log_action`** —
they are the exception to the §7 rule.

- **Agent export** (`/nomina/exports/agent/`) — two sheets. `Yours` = the raw variant plus a Notes
  column; `Mine` = the corrected variant, no Notes. 19 columns: EMP, Legal name, User, Hours Worked,
  Holiday, Holiday Pay, Total Hours, Pay (48), LPO, Referral, Welcome Bonus, Kill Team QA Bonus,
  Spiff, Adherence, Sub Total, Cafeteria, Prestamo, Transportation, Total.
- **Admin export** (`/nomina/exports/admin/`) — one sheet, titled `Yours`, with Notes. 17 columns:
  ID, Username, Nombre, Admin Wage, Hours Worked, Holiday, Holiday Pay, Spiffs, LPO, Refferal,
  Prestamo (given), Subtotal, Bonus, Cafeteria, Prestamo (repay), Transportation, Total.
  Two columns are both headed "Prestamo" — given before Subtotal, repaid after.

### 11.11 Vacation and holiday helpers Nómina owns for the rest of the app

`nomina/views.py` owns the LFT vacation math and is imported by two other apps:

- `vacation_balance(agent, as_of=None)` → `(accrued, used, remaining)` for the agent's **current
  work-anniversary year**, not the calendar year. Accrued comes from `_lft_vacation_days` (Mexican
  "Vacaciones Dignas" schedule: 12/14/16/18/20 for years 1–5, 22 through year 10, +2 per 5 years
  after); used counts `'V'` `AdherenceRecord` days since the most recent hire anniversary; plus the
  `VacationAdjustment` for that anniversary year.
- `vacation_request_check(agent, start, end)` adds `new_days` and an `overdraw` flag.
- Consumers: `scheduling.views.request_detail` and `request_approve` (a supervisor is blocked from
  approving an overdrawing vacation request — super admin only), and
  `adherence.views.save_adherence_cell` (a non-super-admin cannot place a `'V'` that would push the
  balance below 1).
- `nomina.models.Holiday` is read by `adherence.views` for display-only holiday tags on the date
  headers and by `finance.views.admin_adherence`.

### 11.12 Dependency map — what breaks if you change X

- Change `_get_billable_weekly_data`'s returned keys or math → every Nómina money column moves, on
  both sheets, alongside billing and payroll.
- Change `BillingSettings.nr_ratio` → holiday-worked hours change as well as the weekly NR allowance.
- Change `adherence.views._build_maps`' return tuple order → `_vacation_hours`,
  `_holiday_not_worked_hours` and `_admin_bonus_factors` read positions `[0]` and `[4]` by index.
- Change `_scheduled_hours` → vacation pay and holiday not-worked pay change.
- Rename or move `vacation_balance` / `vacation_request_check` / `admin_bonus_penalty` /
  `Holiday` / `AdminBonusDeduction` → breaks `scheduling`, `adherence` and `finance` imports.
- Change `AdherenceRecord.status` values `'V'` or `'Holiday'` → silently changes vacation and
  holiday pay.
- Change `PayrollAdjustment.commission_deduction` semantics → changes Net LPO on the Agent Nómina.
- Change the `Agent` flags `employer`, `is_official_admin`, `role_type`, `track_attendance`,
  `hourly_rate`, `admin_bonus_mxn` → changes who is on which sheet and what they are paid.
