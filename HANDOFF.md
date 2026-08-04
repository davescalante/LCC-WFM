# LCC WFM — System Handoff Document

*Last updated: July 20, 2026. Written for a new assistant/developer with no prior exposure to this project.*

---

## 1. Purpose

LCC WFM is a workforce-management web application for a bilingual call-center operation run by **Infinity** (a staffing vendor) on behalf of **LCC (Legal Conversion Center)**. Supervisors and operations coordinators use it daily to manage the live call floor: agent profiles and schedules, overtime shifts (including an open-shift job board), attendance/adherence tracking driven by Five9 CSV uploads, Erlang-C staffing forecasts, an approval-based request system (vacation, VTO, LOA, schedule changes, coding requests, shift claims, cancellations), and the money side — weekly billing from Infinity to LCC in USD and agent payroll in MXN with bonuses and OT incentives. Agents have a limited self-service portal to see their schedules, request time off, and claim open overtime.

---

## 2. Tech Stack

| Layer | Choice |
|---|---|
| Framework | Django 4.2.30 (Python; single project `wfm` with 4 apps: `scheduling`, `adherence`, `erlang`, `finance`) |
| Database | SQLite locally (`db.sqlite3`, committed to the repo); **PostgreSQL in production** via `dj-database-url` + `psycopg2-binary` |
| Hosting | **Render** (`IS_RENDER` env detection; `RENDER_EXTERNAL_HOSTNAME` added to `ALLOWED_HOSTS`; `DEBUG` off on Render). Deploy = push to `main` on GitHub (`davescalante/LCC-WFM`); `build.sh` runs `pip install`, `collectstatic`, `migrate` |
| App server / static | gunicorn + WhiteNoise (`CompressedManifestStaticFilesStorage`) |
| Excel exports | openpyxl |
| Frontend | Server-rendered Django templates with inline CSS, vanilla JS. No framework, no build step. Small AJAX endpoints (JSON `fetch`) for in-place updates: OT status pills, adherence cells, codings, notes, live-refresh polling (`/poll/`, `/adherence/poll/`) |
| Auth | Stock `django.contrib.auth` (login at `/accounts/login/`), custom session-timeout + access middleware |
| Tests | `scheduling/tests.py`, `erlang/tests.py`, `adherence/tests.py`, `finance/tests.py` — run `python3 manage.py test`. As of the last commit: 222/222 passing |

URL mounts (`wfm/urls.py`): scheduling app at **site root** (and duplicated at `/scheduling/`), `/adherence/`, `/erlang/`, `/finance/`, Django admin at `/admin/`, auth at `/accounts/`.

---

## 3. Every Page / Tab

### Staff navigation (all non-portal users)

| Tab | URL | Access | What it does |
|---|---|---|---|
| Dashboard | `/` | Staff | Landing page: pending request list (top 5 + unread count), today's attendance tallies, agents missing an adherence record today, active-agent count |
| Users | `/agents/` | Staff | Paginated agent list with supervisor/status filters (incl. "Separation in progress"). Add/edit/delete users, per-agent detail page (`/agents/<pk>/`) with separation processing, scheduled role changes, employment periods, Five9 profiles, and a full history page (`/agents/<pk>/history/` — roles, schedules, week-by-week attendance/hours, OT, CSV exports) |
| Shifts | `/shifts/` | Staff | Weekly shift grid, agents grouped into Morning / Afternoon / Kill Team / Admin sections. Per-agent editor at `/shifts/week/` (permanent template, one-time override, or date-range modes; up to 3 time blocks/day for split shifts). Copy-week-forward tools, inline quick-edit (AJAX), clear-recurring |
| OT Shifts | `/overtime/` | Staff | Weekly OT grid (agent rows × day columns): status pills (Pending/Done/No Show/Cancelled — AJAX), incentive badges (1.5x / 2x), Five9 verification results, offered/earned totals. Plus: **Open Shifts row** (dashed cards, OPEN badge), **Post Open Shift** panel, **requests inbox** (claim + cancellation approvals), day-header totals ("X open · Y pending · Z filled"), payroll CSV export, Five9 login-logout verification upload. Per-agent OT editor at `/overtime/week/` |
| Adherence | `/adherence/` | Staff | Weekly attendance grid (rows load async): per-day cell = schedule label, status code, worked hours (login+coded), color coding; bulk or single-cell status entry; per-cell notes; Cost of Schedule footer. Excludes Official Admins |
| Codings | `/adherence/codings/` | Staff | Weekly grid of manual coded-time blocks (credited time). Add/edit/delete inline (AJAX). Regular codings only (admin codings live in Finance) |
| Daily Hours | `/adherence/daily/` | Staff | Per-day Five9 "Daily Hours" CSV uploads. Table per upload: Login Time, Not Ready, Coded Time, NR Allowance (12.5%), Excess NR, Final Hours. Re-match tool for unmatched rows |
| Staffing | `/erlang/` | Staff | Erlang-C Staffing Calculator: upload Five9 hourly call CSV, per-day hourly table — Agents Required (+shrinkage), Scheduled Staff (click to see who), Actual Agents (editable), Variance, **OT Posted** (open/filled), **Net Gap** (colored), **+ Post OT** shortcut for approvers. Save/download reports |
| Requests | `/requests/` | Staff | Two sub-tabs: **Team Requests** (review/approve agent + staff requests, filterable, grouped Pending/Approved/Rejected/Done) and **My Requests** (`/requests/mine/`, staff submit their own — see §6.14). Detail page per request with approve/reject/mark-done |
| Records | `/records/` | Staff | Four read-only record sets with filters + CSV export: Attendance (`/records/`), Hours (`/records/hours/`), Role Log (`/records/roles/`), Separations (`/records/separations/`) |
| Activity | `/activity/` | Staff | Last 500 audit-log entries (every mutation in the system calls `log_action`), filter by user/date |
| Finance | `/finance/` | **Super admins only** | See below |

### Finance section (super admin / Django superuser only)

| Page | URL | What it does |
|---|---|---|
| Finance Dashboard | `/finance/` | Weekly summary cards: total final hours, estimated billing to LCC (USD), estimated payroll (MXN + USD), bonus totals, OT top-up totals, exchange rate |
| Billing Report | `/finance/billing/` | Per-agent billing (Infinity → LCC), grouped by employer then role type, with subtotals; Excel export (`/finance/billing/export/`) |
| Payroll Report | `/finance/payroll/` | Per-agent MXN payroll split Infinity vs LCC Direct; Excel export (`/finance/payroll/export/`) |
| Admin Codings | `/finance/admin-codings/` | Weekly coded-time grid for Official Admins (`is_admin_coding=True` codings — invisible on the regular tabs) |
| Admin Adherence | `/finance/admin-adherence/` | Adherence grid restricted to Official Admins; uses admin codings; bonus column shows the fixed admin bonus; Excel payroll export |
| Settings | `/finance/settings/` | Edit all financial rates with **per-week effective dating** + change history (see §6.9) |

### Agent portal (agents + portal-admin types)

| Tab | URL | What it does |
|---|---|---|
| My Shifts | `/agent/my-shifts/` | The portal landing page — own weekly schedule (overrides + templates) |
| My OT Shifts | `/agent/my-ot-shifts/` | Own OT shifts with status badges; **Request Cancellation** (reason required) on upcoming shifts + request status inline |
| Available OT | `/agent/available-ot/` | Open OT shift board: request-to-claim buttons, "Requested by X — pending approval" markers, and "My Shift Requests" history with results |
| My Adherence | `/adherence/my/` | Own weekly attendance/hours view |
| My Requests | `/agent/my-requests/` | Submit and track requests (6 types), status badges, rejection reasons |
| Inactive | `/agent/inactive/` | Static "no longer active" page — inactive portal users are forced here |

Portal users are hard-locked by middleware to path prefixes `/agent/`, `/adherence/my/`, `/accounts/`, `/static/` — anything else redirects to My Shifts.

---

## 4. User Roles & Permissions

Everyone has exactly one `User` (Django auth) + one `Agent` profile row. The profile is the single source of role truth. There is no Django groups/permissions usage — access is by role fields + two boolean flags + middleware.

**`role`**: `agent` or `admin`.
**`role_type`** (12): agents — `training`, `incubation`, `regular_agent`, `kill_team`, `night_shift`; admins — `supervisor`, `qa`, `cs`, `tester`, `sms_email`, `coordinator`, `trainer`.

| Group | Who | Sees / can do |
|---|---|---|
| **Portal users** | `role='agent'` (any type), plus admins with role_type in `{cs, tester, sms_email}` (`PORTAL_ADMIN_TYPES`) | Agent portal only (middleware-enforced). Submit requests, claim open OT, request cancellations, view own data. Inactive portal users see only the "inactive" page |
| **Staff** | All other admins: supervisor, coordinator, qa, trainer | Full staff navigation. Can approve/reject **agent** requests, edit schedules, adherence, codings, uploads. QA/trainer accounts are created with an **unusable password** (they exist as records but cannot log in — only supervisor, coordinator, cs, tester, sms_email admins get working logins) |
| **OT approvers** | role_type `supervisor` or `coordinator`, plus super admins and Django superusers | Additionally: post/edit/delete open OT shifts, approve/reject claim requests and cancellation requests |
| **Staff-request approver** | The one person set as `supervisor` on the requester's profile | The only person who can action a *staff member's own* request (see §6.14). Self-approval blocked for everyone, everywhere |
| **Super admin** | `Agent.is_super_admin=True` (or Django `is_superuser`) | Everything above + the entire Finance section. "Can grant super admin to others" |
| **Official Admin** | `Agent.is_official_admin=True` | Not a permission — a payroll classification. Excluded from the regular Adherence tab; tracked on Finance → Admin Adherence with admin codings; receives the fixed **admin bonus** instead of the adherence bonus |

Other per-agent fields that alter behavior: `track_attendance` (gates dashboard attendance + records), `employer` (LCC / Infinity, default Infinity), `billing_status` (Billed / Not Billed — informational grouping), `status` (active/inactive).

---

## 5. Key Data Models (quick reference)

**scheduling** — `Agent` (the universal profile), `Five9Profile` (multiple Five9 logins per agent; `billable` flag drives billing/payroll; `is_primary` for matching/display), `EmploymentPeriod` (employment spans with end reasons), `ShiftTemplate` + `ShiftTemplateBlock` (recurring weekly schedule with `effective_from`/`effective_until` versioning; blocks = split shifts), `Shift` + `ShiftBlock` (per-date overrides, always win over templates), `OvertimeShift` (assigned OT: times, incentive type, `incentivized_hours`, `base_hourly_rate`, status pending/completed/no_show/cancelled + `cancellation_reason`), `OpenOTShift` (unassigned OT posting: open/filled/removed), `OTShiftClaimRequest` (claim an open shift; backups allowed), `OTCancellationRequest` (owner asks to cancel an assigned shift), `LoginLogoutUpload` + `AgentLoginSession` (Five9 login-logout report rows, used only for OT verification), `OTShiftVerification` (per-shift verified seconds/coverage %), `AgentRequest` (the 6-type request system incl. staff self-service fields), `ScheduledRoleChange` (future-dated role change), `RoleHistory` (role audit trail with effective dating), `AgentSeparation` (offboarding), `AuditLog` (+ `log_action()` helper — the Activity Log).

**adherence** — `AdherenceRecord` (one per agent per day: `status` code + computed `actual_hours`), `Coding` (manual credited time block; `is_admin_coding` separates Finance-only admin codings), `DailyUpload` + `DailyAgentHours` (one Five9 Daily CSV per day; one row per Five9 username with `login_seconds`/`not_ready_seconds`), `AdherenceNote` (per agent+date notes), `PayrollAdjustment` (per agent+week commission-deduction %).

**finance** — `BillingSettings` (singleton of all rates) + `BillingSettingsHistory` (per-week snapshots — see §6.9).

**erlang** — `ErlangCallRow` (uploaded hourly call volumes per week), `ErlangWeekParams` (per-week calculator parameters incl. per-day weeks-of-data), `ErlangActualStaff` (manually entered actuals), `ErlangReport` (saved snapshots).

---

## 6. Core Business Logic

### 6.1 Attendance status codes (`AdherenceRecord.status`)

| Code | Meaning | Bonus effect |
|---|---|---|
| `P` | Present | qualifying |
| `T` | Tardy | **disqualifying** |
| `Absent` | Absent (shown as "A") | **disqualifying** |
| `NCNS` | No Call No Show | **disqualifying** |
| `S` | Suspension | **disqualifying** |
| `VTO` | Voluntary Time Off | qualifying; zeroes scheduled hours |
| `IMSS` | Medical/IMSS leave | neutral |
| `MUT` | Make Up Time | qualifying |
| `OT` | Overtime day | qualifying |
| `P+VTO` | Present, then took VTO | qualifying; scheduled capped at hours actually worked |
| `T+VTO` | Tardy, then VTO | **disqualifying** |
| `I` | Incomplete shift | **disqualifying** |
| `T+I` | Tardy and incomplete | **disqualifying** |
| `Quit` | Quit (auto-set by separations) | neutral |
| `Baja` | Terminated | neutral |
| `V` | Vacation | qualifying |
| `LOA` | Leave of Absence | **disqualifying**; zeroes scheduled hours |

Statuses are set **manually** by supervisors (cell click or bulk save) or automatically by request approvals (V/VTO/LOA) and separations (Quit/NCNS). There is **no automated tardy detection** — uploads only zero the hours of scheduled agents missing from the file; they never set a status.

### 6.2 Adherence bonus

- Constants: `BONUS_QUALIFYING = {P, OT, MUT, VTO, P+VTO, V}`, `BONUS_DISQUALIFYING = {Absent, NCNS, T, T+VTO, T+I, I, LOA, S}` (`wfm/constants.py`).
- **Eligibility**: any disqualifying status in the week → no bonus. An OT shift marked **No Show** also disqualifies. Requires at least one recorded status. Official Admins never get it (they get the admin bonus).
- **Amount** (proportional rule): `bonus = min(max_bonus, final_hours / full_hours × max_bonus)` — defaults **400 MXN max**, **40 h threshold**. At or above 40 final hours → full 400; below → pro-rated (e.g. 30 h → 300 MXN).
- `final_hours` is the week's login+coded hours after all NR deductions.

### 6.3 Admin bonus

Official Admins (`is_official_admin=True`) receive a **fixed** bonus instead: the per-agent `admin_bonus_mxn` if set, else the global `default_admin_bonus_mxn` (**500 MXN** default). Their adherence bonus is forced to zero.

### 6.4 Daily 12.5% not-ready deduction (adherence)

Applied per **day** when a Daily Hours CSV is uploaded (and re-applied whenever codings change):

```
total      = login_seconds + coded_seconds (non-admin codings)
allowance  = total × nr_ratio          (default 0.125 = 12.5%)
excess_NR  = max(0, not_ready_seconds − allowance)
final      = max(0, login_seconds − excess_NR)  →  AdherenceRecord.actual_hours
```

So each agent gets an NR allowance of 12.5% of their worked time; only NR **beyond** the allowance is deducted. Coded time enlarges the allowance but is never itself reduced. This daily math affects the adherence displays and `actual_hours` only.

### 6.5 Weekly NR caps (billing/payroll)

Computed in Finance per agent per week, **from raw login seconds** (not the daily-adjusted values):

- **Check 1 — absolute cap**: weekly NR hours above the cap are deducted. Cap = **6 h** regular, **7 h** for `kill_team` role type.
- **Check 2 — 48-hour ratio rule**: only when the week's pre-deduction total (login+coded) is **≤ 48 h**, deduct `max(0, weekly_NR − raw_login × 12.5%)`. Above 48 h this check is skipped entirely.
- **Larger-of-two rule**: the deduction applied is `max(check1, check2)` — never both.
- `final_hours = max(0, login + coded − deduction)` → drives both billing and payroll.

Note the adherence tab shows only the check-1 portion in its `NR Cap Adj` column (its daily 12.5% was already applied at upload); Finance recomputes independently from raw seconds.

### 6.6 Cost of Schedule (COS)

Adherence-tab footer measuring % of scheduled hours lost. Per day per agent: `Absent/NCNS/IMSS/S` lose the full scheduled hours; `T`/`T+I` lose `scheduled − actual` (or a default **0.25 h / 15 min** if no login); `OT`/`MUT` hours *above* schedule offset losses **within the same day only**. Day COS % = net loss ÷ scheduled × 100; week = summed. Color bands: 0% green → >35% red.

### 6.7 Billing (Infinity → LCC)

- **Infinity bills LCC** in USD for every agent with at least one **billable Five9 profile**.
- `billing_usd = final_hours × rate`, where rate = per-agent `billing_rate_usd` override or the global `billing_rate_usd` (**$15.00/h** default).
- **Multi-account agents**: only `DailyAgentHours` rows whose Five9 username belongs to one of the agent's *billable* profiles count; non-billable accounts (e.g. a Kill Team OT login) are excluded. The primary billable username is used for display.
- OT base hours are already inside `final_hours` (agents log into Five9 during OT), so billing needs no separate OT line; incentive top-ups and bonuses are shown as separate USD-equivalent totals.
- Report groups by employer (Infinity vs LCC Direct) then by role type; separated agents drop off from their `remove_from_adherence_date` week.

### 6.8 Payroll (MXN)

- Base pay = `final_hours × hourly_rate` (per-agent MXN rate, default **62.50 MXN/h**).
- **OT incentive top-ups** (only `status='completed'` OT shifts count; these are premiums *on top of* base pay since the base OT hour is already in `final_hours`):
  - Time & a Half → extra `0.5 × rate × incentivized_hours`
  - Power Hour → extra `1.0 × rate × incentivized_hours` (2× total)
  - The docstrings identify these premiums as LCC-funded incentives.
- Total pay = base + top-ups + adherence bonus **or** admin bonus. USD equivalent via the `usd_to_mxn` setting (default 17.0).
- **Commission deduction %** is stored per agent-week (`PayrollAdjustment`) and *displayed* on payroll — **but it is never actually subtracted from total pay** (see Quirks), and the "Comm. Earned" export column is hardcoded to "—" ("commission earnings tracking coming soon").
- Reports split Infinity vs LCC Direct employees; the totals block covers Infinity only.

### 6.9 Per-week versioned financial settings

`BillingSettings` is a singleton holding every rate: billing rate, USD→MXN, NR caps (6/7), NR ratio (0.125), 48-h ceiling, tardy default (0.25), bonus max/threshold (400/40), default admin bonus (500). Saving from Finance → Settings picks an **effective week**; the values are snapshotted into `BillingSettingsHistory` keyed by that week's Monday *and* written to the singleton. All calculations call `get_for_week(week_start)` — the most recent snapshot at or before that week — so **historical weeks always recompute with the rates that were in force then**.

### 6.10 Five9 CSV uploads

Two distinct formats:

1. **Daily Hours** (Adherence → Daily Hours, one per day): columns `AGENT`, `LOGIN TIME`, `NOT READY TIME` (+ optional `AGENT GROUP`), times as HH:MM:SS. Rows match to agents by **Five9 username** (case-insensitive) via `Five9Profile`; unmatched rows are kept and can be re-matched later. On upload: daily NR math runs (§6.4) for the *billable* row only, and every agent who was scheduled that day but absent from the file gets `actual_hours = 0` (no status set). Re-uploading a date replaces it.
2. **Login-Logout report** (OT Shifts → Verify): columns `AGENT`, `DATE` (YYYY/MM/DD), `LOGIN TIMESTAMP` / `LOGOUT TIMESTAMP`. Builds `AgentLoginSession`s and verifies every OT shift on those dates: Five9 session time and coding time are clipped to the shift window, merged, and stored as `OTShiftVerification` (coverage %, "username not found", "covered by codings" annotations shown on the OT grid).

3. The **Staffing Calculator** has its own third upload: the Five9 *ACD Queue Quality of Service Details – Hourly* report (call volumes per day/hour).

### 6.11 Staffing Calculator (Erlang C)

- Upload hourly call volumes per week; parameters per week: target SL (default 80% in 20 s), shrinkage %, AHT (default 420 s), and **weeks of data** — a default plus optional per-day overrides (volumes are divided by weeks to get an hourly average).
- Erlang C computes Agents Required, then +shrinkage.
- **Scheduled Staff** counts, per (day, hour): call-role agents (`regular_agent`, `night_shift` — role checked **as of the viewed week** using RoleHistory for past weeks and pending role changes for future dates) from Shift overrides falling back to effective ShiftTemplates, **plus every non-cancelled OT shift for any agent regardless of role**. Overnight shifts split at midnight; each agent counts once per hour. Click a number to see who.
- **OT visibility columns**: "OT Posted" = open/filled posting counts overlapping the hour; "Net Gap" = `max(0, required − scheduled) − open_postings` (filled postings already count inside Scheduled Staff, so they are *not* subtracted again). Red = post more, amber 0 = covered only by unclaimed postings, green ✓ = covered. "+ Post OT" pre-fills the posting modal with date/hour/gap-count and returns to the calculator.
- Open (unfilled) postings **never** count as coverage — they aren't `OvertimeShift` rows at all.

### 6.12 Scheduled role changes & RoleHistory

- A supervisor schedules a future role change (new role type, optional new supervisor and new weekly schedule) from the agent detail page; **one pending change per agent**.
- The new schedule is written into ShiftTemplates **immediately** (effective-dated), so Shifts/Adherence/Staffing show the future schedule before it applies; cancelling the change reverses those templates.
- The change **applies** automatically on/after its effective date via three redundant paths: a middleware that runs once per calendar day on the first request, lazily whenever that agent's detail page loads, and a management command for cron.
- Application updates the Agent's role/role_type/supervisor, propagates role_type to matching Five9 profiles, closes the open `RoleHistory` row and opens a new one. `RoleHistory` (with `effective_from`/`effective_to`) is also written on any manual role-affecting edit and is what keeps the Staffing Calculator historically accurate.

### 6.13 Separation process

- Processed from the agent detail page; two modes:
  - **In Progress**: nothing changes — the agent stays active while termination documentation is collected (the detail page shows days-since-last-day and 30-day NCNS/Absent counts). Can later be finalized or cancelled.
  - **Finalized** (requires `remove_from_adherence_date`, a Monday): sets the agent inactive, stops attendance tracking, stamps the termination date, closes the open EmploymentPeriod with a mapped reason, **cancels all future pending OT shifts**, **auto-rejects pending requests**, **cancels future pending role changes**, and auto-codes the remainder of the last week (days after the last day worked): quit/terminated → `Quit`, abandonment → `NCNS`; contract-end and resigned-notice are not auto-coded.
- The agent remains on Adherence/billing until their `remove_from_adherence_date` week. Recently-separated agents still show on the adherence grid until that date passes.
- Cancelling an in-progress separation keeps the agent active. **There is no un-finalize flow** — reversing a finalized separation is manual.

### 6.14 Request system

**Types** (one `AgentRequest` model): Coding, Vacation, Day Off Change (one-time or permanent), VTO, LOA, Schedule Change.

**Who approves what:**
- **Agent requests** — any staff user can approve/reject/mark-done.
- **Staff requests** (supervisors/coordinators/admins submitting for themselves via Requests → My Requests): actionable **only** by the supervisor on the requester's profile (snapshotted at submission as `assigned_supervisor`). Others see the request read-only with "Only [name] can action this request." Submission is blocked if no supervisor is assigned.
- **No one can ever action their own request**, including the self-supervisor edge case.

**Auto-apply on approval** (identical for agents and staff):
- Vacation → `V` on each day; VTO → `VTO` on the date; LOA → `LOA` on scheduled working days only. (V/VTO/LOA zero or adjust scheduled hours per §6.1.)
- Day Off Change: one-time → `Shift` overrides (new day off + old day working); permanent → effective-dated `ShiftTemplate` updates.
- Schedule Change → `_save_shift_template` per selected weekday from the effective date.
- Coding → **no auto-apply** (status-only): requesters only estimate their missed time, so the supervisor pulls the exact amount and enters the coding manually on the Codings tab.
- Everything is logged (`auto_action_log` on the request + Activity Log); unread badges flow both directions (approver on submit, requester on response).

**Open OT shifts**: OT approvers post unassigned shift slots (multi-count for identical slots). Everyone can request them (agents via Available OT, staff via the OT grid). Multiple pending requests per shift are allowed as backups; approving one **creates a real `OvertimeShift`** for the winner (base rate = their hourly rate; incentivized hours = full shift length if an incentive is set), fills the posting, and auto-rejects the backups. Rejection returns the shift to fully open. Deleting a posting soft-deletes it (`removed`) and rejects pending claims with a notice.

**OT cancellation requests**: a shift's owner submits a reason; the shift **stays assigned and fully counted** until an approver acts. Approval flips it to the existing `cancelled` status (drops out of hours, pay, staffing); rejection changes nothing — a missed shift is then a normal No Show.

---

## 7. Known Quirks & Implementation Details

1. **Commission deduction is display-only.** `PayrollAdjustment.commission_deduction` is entered and shown on payroll, but never subtracted from `total_pay_mxn`; the "Comm. Earned" export column is hardcoded `'—'` ("coming soon").
2. **Two separate NR mechanisms.** The daily 12.5% deduction (adherence, writes `actual_hours`) and the weekly cap/48-h check (finance, recomputed from **raw** `login_seconds`) are independent — Finance does *not* reuse the daily-adjusted values. Don't "fix" one by making it read the other without understanding both.
3. **`recalculate_actual_hours` management command drifts from the live code**: it hardcodes `0.125` instead of reading `nr_ratio` from settings, and it does not exclude admin codings when summing coded time (the live `_refresh_actual_hours` does both correctly).
4. **Scheduled Staff includes assigned OT.** In the Staffing Calculator, every non-cancelled `OvertimeShift` counts as coverage regardless of role. This is why Net Gap subtracts only *open* postings — subtracting filled ones would double-count.
5. **Access control is middleware-only for most staff views.** Beyond `AgentAccessMiddleware` keeping portal users out, destructive staff views (agent delete, shift edits, role changes) have no supervisor-vs-other-staff distinction. The exceptions with real per-view gates: Finance (`finance_access_required`), OT posting/approvals (`_is_ot_approver`), staff-request actioning (assigned supervisor), and separations (manual role check).
6. **Login rules**: only supervisor, coordinator, cs, tester, sms_email admins and `role='agent'` users get working passwords; QA/trainer accounts are created with unusable passwords. `teams_password` and `Five9Profile.five9_password` are plain stored credentials (viewable in the UI), unrelated to Django auth.
7. **Session policy**: 4 h inactivity timeout, 16 h absolute, expire on browser close; AJAX gets `{'expired': true}` 401s, pages redirect to login with a banner.
8. **`Agent.five9_username`/`five9_password` are dead legacy fields** — superseded by `Five9Profile` (multi-account with `billable` and `is_primary` flags); only a data migration ever read them.
9. **"Best template" resolution is duplicated** in at least 4 places (shift_list, shift_week, `_best_shift_template`, adherence) with identical rules: overrides beat templates; among templates covering the date, latest `effective_from` wins (NULL = forever). Change one, change all.
10. **`_save_shift_template` deletes future templates** (`effective_from > effective_date`) when writing a permanent change — a schedule change wipes any already-scheduled later changes for that day.
11. **Silent exception swallowing** in `ApplyScheduledRoleChangesMiddleware` and `AgentAccessMiddleware` — failures to apply role changes or compute badges are invisible.
12. **Role changes apply on first request of the day** (cache-keyed), not on a guaranteed cron. The management command exists but is a backstop, not scheduled by the repo.
13. **Coding blocks cannot cross midnight** — overnight coded time must be split into two entries (enforced in the UI).
14. **Cell/status colors, badge patterns, and modals are consistent conventions**: Pending gray, Approved green, Rejected red, Done blue; red `nav-badge` bubbles; inline-styled modals. Match them for anything new (standing owner instruction: simple, clean, consistent, minimal clicks, no full-page reloads for small updates).
15. **`db.sqlite3` is committed to git** (local dev data); production data lives in Render Postgres. A stale `test_path.py` (21 KB) sits at repo root.
16. **Hardcoded supervisor sort priority** in the Shifts grid admin section: `{'Jesus Urbina': 0, 'Andrea Jones': 1}`.
17. Timezone: `America/Mexico_City` semantics via Django `timezone.localdate()`; weeks always run Monday–Sunday, and "week_start" params are snapped to Monday.

---

## 8. Partially Built / Planned

- **Commission earnings tracking** — explicitly marked "coming soon" on the payroll page; the deduction % field exists but has no payroll effect (see Quirk #1).
- **No re-activation flow for finalized separations** — reversal is a manual multi-step edit.
- **No automated tardy/absent status detection** — uploads zero hours only; statuses are manual. (A natural future feature, but currently by design.)
- `AdherenceRecord.notes` field exists but is unused — per-cell notes went to the separate `AdherenceNote` model instead.
- `agent_inactive` view imports `logout` without calling it — leftover from an abandoned approach.
- No TODO/FIXME markers exist anywhere in the codebase; the items above are the only known loose ends.

---

*End of handoff. For the request/OT subsystem the tests in `scheduling/tests.py` and `erlang/tests.py` double as executable specifications of the approval rules.*
