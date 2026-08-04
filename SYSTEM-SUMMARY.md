# LCC-WFM — System Reference (for planning conversations)

*Generated 2026-08-03 by reading the live codebase at `/Users/denisetijerina/Documents/LCC-WFM` (branch `main`, HEAD `b2a4c8b`). This is a structural map, not a code dump — verify exact line numbers against current code before quoting them, since the app changes fast (several commits landed the same day this was written).*

---

## 1. Stack & Deploy

- **Framework**: Django 4.2.30 (Python). Single project `wfm` containing four apps: `scheduling`, `adherence`, `erlang`, `finance`.
- **Database**: SQLite locally (`db.sqlite3`, committed to the repo — this is intentional local dev data, not a mistake). **PostgreSQL in production** via `dj-database-url` + `psycopg2-binary`.
- **Hosting**: Render (`IS_RENDER` env detection; `DEBUG` off there). App server = gunicorn; static files via WhiteNoise (`CompressedManifestStaticFilesStorage`).
- **Repo**: GitHub `davescalante/LCC-WFM`, default branch `main`. Deploy = push to `main`; `build.sh` runs `pip install -r requirements.txt`, `collectstatic`, `migrate`.
- **Key deps** (`requirements.txt`): Django 4.2.30, gunicorn 23, whitenoise 6.11, psycopg2-binary, dj-database-url, openpyxl 3.1.5 (all Excel exports).
- **Frontend**: server-rendered Django templates, inline CSS, vanilla JS. No SPA framework, no build step. Small AJAX/JSON endpoints handle in-place updates (status pills, cell edits, live-poll badges via `/poll/` and `/adherence/poll/`).
- **Auth**: stock `django.contrib.auth`, login at `/accounts/login/`. Custom `SessionTimeoutMiddleware` (4h inactivity / 16h absolute) and `AgentAccessMiddleware` (role-based routing + badge counts) in `wfm/middleware.py`.
- **Tests**: `scheduling/tests.py`, `erlang/tests.py`, `adherence/tests.py`, `finance/tests.py` — run with `python3 manage.py test`. As of the last commit: 227/227 passing. Tests double as executable specs for the trickier rules (NR caps, bonus eligibility, request approvals, export field gating).
- **URL mounts** (`wfm/urls.py`): scheduling at site root (duplicated at `/scheduling/`), `/adherence/`, `/erlang/`, `/finance/`, plus `admin-codings/` and `admin-adherence/` mounted directly off the **root** urlconf (not under `/finance/` — see §4), Django admin at `/admin/`, auth at `/accounts/`.

---

## 2. Apps & Purpose

- **`scheduling`** — the backbone app. Owns the universal `Agent` profile (used by staff and agents alike), shift scheduling (templates + overrides), overtime (assigned + open-shift job board), the request/approval system, role-change scheduling, separations, the audit log, and the agent self-service portal views. By far the largest app (`views.py` ~4,000 lines).
- **`adherence`** — daily attendance status entry, manual "codings" (credited time blocks), Five9 Daily Hours CSV ingestion, the not-ready(NR)-deduction math that produces `actual_hours`, and the weekly adherence grid/bonus computation (`_build_maps`/`_build_rows`) that Finance reuses.
- **`finance`** — super-admin-only money section: billing (Infinity→LCC, USD), payroll (MXN), the versioned `BillingSettings`, Admin Codings/Admin Adherence views (business logic lives here even though the tabs are no longer under `/finance/` in nav), and every Excel export.
- **`erlang`** — Erlang-C staffing forecast calculator: hourly call-volume upload, required-vs-scheduled-vs-actual-agents grid, OT-shortfall visibility, saved report snapshots.
- **`wfm`** — the project shell: settings, root urlconf, the two custom middlewares, and shared `constants.py` (bonus qualification sets, portal-admin types, status-zeroing sets) and `utils.py` (week helpers).

---

## 3. Key Models

### `Agent` (scheduling/models.py) — the single profile for every human in the system (staff and agents alike; one per Django `User`, `OneToOneField`)
- **Identity/role**: `role` (`agent`/`admin`), `role_type` (12 choices — agent-side: `training`, `incubation`, `regular_agent`, `kill_team`, `night_shift`; admin-side: `supervisor`, `qa`, `cs`, `tester`, `sms_email`, `coordinator`, `trainer`), `status` (`active`/`inactive`), `agent_name` (display/call-center name — distinct from the Django legal name), `employee_id` (unique, nullable), `start_date`, `termination_date`.
- **Org**: `supervisor` — self-FK (`related_name='direct_reports'`), `employer` (`LCC`/`Infinity`, default Infinity), `billing_status` (`Billed`/`Not Billed`, informational), `track_attendance` (gates dashboard tallies + Records).
- **Contact**: `phone_country_code` (`+1` or `+52`, default `+1`), `phone_number`.
- **Legacy**: `five9_username`/`five9_password` — dead fields, superseded by `Five9Profile`; only a migration ever read them.
- **Pay**: `hourly_rate` (MXN, default 62.50), `billing_rate_usd` (per-agent override of the global USD rate).
- **Permission flags** (no Django groups/permissions used anywhere — access is these flags + `role`/`role_type` + middleware):
  - `is_official_admin` — payroll classification, not itself a permission. Excludes the agent from the regular Adherence/Codings tabs; routes them to Admin Adherence/Admin Codings instead; gives them the fixed **admin bonus** instead of the adherence bonus.
  - `is_super_admin` — full Finance access + can grant super admin to others. Also implies admin-tabs access.
  - `can_access_admin_tabs` — access to Admin Codings/Admin Adherence *without* full Finance access. A non-super-admin holder is **team-scoped**: sees only their own direct reports (+ self) among Official Admins; a super admin/superuser sees everyone. Added via migration `0046_agent_can_access_admin_tabs`.
  - `admin_bonus_mxn` — per-agent override of the global default admin bonus.
- **Property**: `separation` — the latest non-cancelled `AgentSeparation` for this agent, or `None`.

### `Coding` (adherence/models.py)
Manual credited-time block: `agent`, `date`, `start_time`, `end_time`, `notes`, `is_admin_coding` (splits regular Codings-tab entries from Finance-only admin entries — regular Codings/Adherence queries always exclude `is_admin_coding=True`). Cannot cross midnight (UI-enforced). Helper methods compute duration (`total_hours`, `total_hhmmss`).

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
`separation_type` (`quit`/`terminated`/`abandonment`/`contract_end`/`resigned_notice`), `status` (`in_progress`/`finalized`/`cancelled`), `last_day_worked`, `remove_from_adherence_date` (must be a Monday — the week the agent stops appearing on Adherence/billing/payroll), audit fields (`processed_by`, `finalized_by`). Finalizing cascades: sets agent inactive, closes the `EmploymentPeriod`, cancels future OT/role-changes, auto-rejects pending requests, auto-codes the remainder of the last week (Quit/NCNS depending on type). No un-finalize flow exists.

### `BillingSettings` / `BillingSettingsHistory` (finance/models.py)
`BillingSettings` is a **singleton** (`BillingSettings.get()`) holding every rate: `billing_rate_usd` (15.00), `usd_to_mxn` (17.0), `nr_cap_regular_hours` (6.00), `nr_cap_kill_team_hours` (7.00), `default_admin_bonus_mxn` (500.00), `adherence_bonus_max_mxn` (400.00), `adherence_bonus_full_hours` (40.00), `nr_ratio` (0.1250), `nr_ratio_max_hours` (48.00), `default_tardy_hours` (0.25). Saving from Finance → Settings snapshots the current values into `BillingSettingsHistory` keyed by an effective Monday; `BillingSettings.get_for_week(week_start)` returns the most-recent snapshot at or before that week (falling back to the singleton), so historical weeks always recompute with the rates in force at the time.

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

Core function: **`finance.views._get_billable_weekly_data(agents, week_dates, settings)`** (finance/views.py). For each agent it pulls raw `DailyAgentHours` (login/not-ready seconds) restricted to that agent's **billable** Five9 usernames, sums coded hours (excluding admin codings unless the caller wants admin math), and applies the weekly NR cap/deduction:

- **Check 1 — absolute cap**: NR hours above a flat cap are deducted. Cap = `nr_cap_regular_hours` (6h) normally, `nr_cap_kill_team_hours` (7h) for `kill_team` role type.
- **Check 2 — 48-hour ratio rule**: only when the week's pre-deduction total (login+coded) is ≤ `nr_ratio_max_hours` (48h default), deduct `max(0, weekly_NR − raw_login × nr_ratio)` (nr_ratio default 12.5%). Skipped entirely above 48h.
- **Larger-of-two**: applied deduction = `max(check1, check2)`, never both.
- **VTO-raises-to-cap rule**: if any day that week has a VTO-type status (`VTO_TYPE_STATUSES = {VTO, P+VTO, T+VTO}` in `wfm/constants.py`), the weekly NR allowance is raised to the flat cap for that agent-week, effectively skipping the ratio check regardless of hours.
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

---

## 7. Existing Exports

All Excel (`.xlsx`, via openpyxl) unless noted. Every export calls `log_action(...)` to the Activity Log.

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

---

## 8. Key Business Rules

- **Pay-window rule for recently-separated agents**: an agent counts toward Adherence/billing/payroll/exports if `status='active'` **OR** (`status='inactive'` AND a `finalized` separation with `remove_from_adherence_date > week_start` for the week being viewed). This exact predicate is duplicated across `billing_report`, `billing_export`, `payroll_export`, `codings_export`, `agent_list`'s export, and `adherence._get_adherence_agent_pks` — always Monday-of-week comparisons.
- **Week model**: always Monday–Sunday; `week_start` params are snapped to Monday everywhere (`wfm.utils.get_week_start`/`parse_week_param`).
- **USD billing vs MXN pay**: Infinity bills LCC in USD (`billing_rate_usd`, per-agent override or global); agents are paid in MXN (`hourly_rate`); `usd_to_mxn` converts for USD-equivalent payroll display. Both rates are per-week-versioned via `BillingSettingsHistory`.
- **Bonus qualifying/disqualifying statuses**: qualifying = `{P, OT, MUT, VTO, P+VTO, V}`; disqualifying = `{Absent, NCNS, T, T+VTO, T+I, I, LOA, S}` (`wfm/constants.py`). Any disqualifying status anywhere in the week (including an OT shift marked No Show) kills the whole week's bonus. Official Admins never get the adherence bonus — they get the fixed admin bonus instead.
- **Scheduled-hours zeroing**: `VTO`, `LOA`, `V` zero (or cap, for `P+VTO`) the day's scheduled hours since the agent wasn't expected to work.
- **VTO raises the weekly NR cap to the flat allowance**, bypassing the 48h ratio check for that agent-week.
- **`is_admin_coding` and `is_official_admin` are the hard partition** between "regular" (Codings/Adherence) and "admin" (Admin Codings/Admin Adherence) data — never mix the two query paths.
- **Team-scoping for `can_access_admin_tabs` holders** (non-super-admin): limited to their own direct reports + self among Official Admins; super admins/superusers see everyone. This is enforced server-side in `_admin_tabs_access`, not just hidden in the UI.
- **Financial export fields are super-admin-gated server-side**, not just hidden in the UI (see `USER_EXPORT_FINANCIAL` pattern in §7) — the intended pattern for any future financial field added to a non-Finance export.
- **`db.sqlite3` is committed to git** for local dev; production data lives in Render Postgres — never assume local DB state reflects production.

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

## 10. Recent Work (most recent first, as of `577774e`)

1. **Add Shift Hours to Billing Report v2 export; extract shared computation** (`577774e`) — new column D on Billing v2, same definition as the Adherence export's Shift Hours (raw regular-schedule hours Mon–Sun, excluding overtime, not zeroed by VTO/LOA/V). Extracted the summation out of `_build_rows` into a new module-level `adherence.views._compute_shift_hours` — the single implementation both exports now call, so they cannot drift — and promoted `_effective_template` from a closure nested in `_build_rows` to a module-level function so the new helper could reuse it. Tests: 222 → 227.
2. **Add "Shift Hours" column to combined Adherence export** (`4949f7a`) — raw regular-schedule hours Mon–Sun, excluding overtime, not zeroed by VTO/LOA/V, on both the Adherence and VTO Agents sheets. New `shift_hours` key on `_build_rows`' row dict, gated by the same `is_scheduled_day` condition as `sched_hours`.
3. **Fix stale `actual_hours` surviving Daily Hours delete/replace** (`b2a4c8b`) — added `_reconcile_stale_actual_hours(upload_date)`, wired into upload/rematch/delete, all now atomic. Display-layer only; billing/payroll unaffected since they never read `actual_hours`.
4. **Users export fields overhaul** (`ba5eba8`) — added "Agent name" as its own column, renamed "Full name"→"Legal name," dropped Django-split First/Last name columns, replaced "Phone country code" with a single formatted "Full phone number."
5. **Gate financial export columns to super admins** (`6bfe156`) — `USER_EXPORT_FINANCIAL = {'hourly_rate'}`; hidden from the picker and stripped server-side for non-super-admins.
6. **Users export: column-picker popup** (`faf1837`) — turned the fixed 8-column Users export into an 18-field (now 16 after the rename/consolidation above) checkbox picker, defaulting to the classic set, respecting current filters.
7. **Exclude LCC-employer users from three Finance exports** (`9d4c3f3`) — likely Codings/Adherence/Billing-family exports; confirmed on `codings_export` (`.exclude(employer='LCC')`).
8. **Fix admin adherence tab false "missing time" for Official Admins** (`69aae21`).
9. **Atomic agent create/edit transactions** (`30cc78d`) — prevents orphaned Django `User` rows if agent-profile creation fails mid-request.
10. **User Setup Audit export** (`5e807be`) — new super-admin, read-only, one-row-per-Five9-account audit workbook.
11. **Combined "Export Adherence" report** (`412f445`) — merges regular Adherence + Admin Adherence into one workbook via shared `_build_maps`/`_build_rows`.
12. **Admin Codings/Admin Adherence relocation, 5-part migration** (`3a5fbb9`, `494e260`, `3755b60`, `8b09140`, `a55e44e`): moved these two tabs out of the Finance section into root-level URLs (`/admin-codings/`, `/admin-adherence/`), introduced the `can_access_admin_tabs` permission flag (independent of `is_super_admin`/Finance access), added team-scoping so a non-super-admin holder sees only their own Official-Admin direct reports, and locked down server-side edit enforcement to match.
13. An **"Admin Attendance" feature was added then fully reverted** (`c1772c0` → reverted by `4f88bb9`/`f7045d2`) before the relocation work above — worth knowing if old branches/PRs reference "Admin Attendance," since it no longer exists under that name.

**Note**: a stray `.claude/worktrees/agent-a2f0b18fec4b3873b/` directory exists in the repo root containing a parallel checkout with slightly older line numbers for `finance/views.py`/`adherence/views.py` — this is agent scratch state, not part of the real app; don't confuse it with the top-level `finance/`/`adherence/` app directories when citing file paths.
