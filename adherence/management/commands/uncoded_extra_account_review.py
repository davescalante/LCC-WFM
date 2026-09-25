"""Read-only review of uncoded time on non-primary Five9 accounts.

Some agents run a second Five9 account purely so their regular call-taking is
counted in talk-time reports. Time on a non-primary account is never paid unless
someone codes it for that agent -- and the Adherence tab currently shows that
account's time on days the primary account has no login, which already led one
supervisor to skip coding a day (Mark Reyes, Fri Sep 18 2026 went unpaid). Fixing
the Adherence tab is a later phase; this command only reviews real production data
so the size of the problem is known first.

Opens no write transaction and never modifies a row. Not wired into any URL or
view; run it by hand:

    python manage.py uncoded_extra_account_review
    python manage.py uncoded_extra_account_review --start 2026-08-24 --end 2026-09-27

Reviews only the specific (agent, date) pairs where a non-primary Five9 account
actually has Daily Hours login time -- not every day in the range -- so an
ordinary short/tardy day that never touched a second account is never flagged.

Every figure it reuses comes from existing code, not new math:
  - Billable login time: the same per-row test finance.views._get_billable_weekly_data
    uses against wfm.utils.get_billable_username_map, applied to one day instead of a week.
  - Coded time: the same is_admin_coding / is_official_admin partition test
    _get_billable_weekly_data uses, applied to one day instead of a week.
  - Scheduled hours (including OT): adherence.views._build_rows' per-day cell
    ('sched_hrs') -- the exact figure shown in each Adherence grid cell. Summing a
    week's worth of these cells is already pinned identical to
    adherence.views._compute_effective_scheduled_hours's weekly total by
    finance.tests.BillingV2ExportTests.test_scheduled_hours_matches_build_rows_exactly.

A day is flagged if paid time (billable login + coded) is more than 5 minutes below
scheduled hours, or non-primary login time is more than 5 minutes above coded time.
"Paid time" here is a raw review figure only -- it applies no NR deduction and no cap,
and is NOT what the agent was actually paid.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand

from adherence.models import Coding, DailyAgentHours
from adherence.views import _build_maps, _build_rows
from finance.models import BillingSettings, BillingSettingsHistory
from scheduling.models import Agent, Five9Profile
from wfm.utils import get_billable_username_map, get_week_start

DEFAULT_START = date(2026, 8, 24)
DEFAULT_END = date(2026, 9, 27)
FLAG_THRESHOLD_SECS = 5 * 60


def _display_name(agent):
    return agent.agent_name or agent.user.get_full_name() or agent.user.get_username()


def _hhmm(seconds):
    minutes = round(seconds / 60)
    h, m = divmod(int(minutes), 60)
    return f'{h}:{m:02d}'


def _safe_billing_settings(week_start):
    """Same lookup as finance.models.BillingSettings.get_for_week, except the final
    singleton fallback is read-only. get_for_week falls back to BillingSettings.get(),
    which is get_or_create(pk=1) -- a real write if the row is ever missing, which this
    read-only command must never risk."""
    history = BillingSettingsHistory.objects.filter(
        week_start__lte=week_start
    ).order_by('-week_start', '-changed_at').first()
    if history:
        return history
    return BillingSettings.objects.filter(pk=1).first() or BillingSettings()


class Command(BaseCommand):
    help = 'Read-only: review uncoded time on non-primary Five9 accounts (no writes).'

    def add_arguments(self, parser):
        parser.add_argument('--start', type=date.fromisoformat, default=DEFAULT_START,
                            help=f'Start date, ISO format (default {DEFAULT_START.isoformat()}).')
        parser.add_argument('--end', type=date.fromisoformat, default=DEFAULT_END,
                            help=f'End date, ISO format (default {DEFAULT_END.isoformat()}).')

    def handle(self, *args, **options):
        start, end = options['start'], options['end']
        self.stdout.write(
            f'Read-only uncoded extra-account time review — {start.isoformat()} to {end.isoformat()}'
        )
        self.stdout.write('(no rows modified)\n')

        # ── Which Five9 usernames are primary / non-primary, per agent ────────
        primary_usernames = {}
        nonprimary_usernames = {}
        for p in Five9Profile.objects.all().values('agent_id', 'five9_username', 'is_primary'):
            uname = p['five9_username'].strip().lower()
            if not uname:
                continue
            bucket = primary_usernames if p['is_primary'] else nonprimary_usernames
            bucket.setdefault(p['agent_id'], set()).add(uname)

        # ── Which (agent, date) pairs actually have non-primary login time ────
        qualifying_days = {}  # agent_id -> set of dates
        for row in DailyAgentHours.objects.filter(
            upload__date__range=(start, end), agent__isnull=False, login_seconds__gt=0
        ).values('agent_id', 'five9_username', 'upload__date'):
            aid = row['agent_id']
            uname = row['five9_username'].strip().lower()
            if uname in nonprimary_usernames.get(aid, ()):
                qualifying_days.setdefault(aid, set()).add(row['upload__date'])

        reviewed_ids = {aid for aid in qualifying_days if primary_usernames.get(aid)}
        skipped_ids = {aid for aid in qualifying_days if not primary_usernames.get(aid)}

        if not reviewed_ids and not skipped_ids:
            self.stdout.write('No agent has non-primary Five9 login time in this range.')
            self.stdout.write(self.style.SUCCESS('Nothing was written.'))
            return

        agents_by_id = {
            a.pk: a for a in Agent.objects.filter(pk__in=reviewed_ids | skipped_ids)
            .select_related('user', 'supervisor__user')
        }

        # ── Billable login time: identical per-row test to _get_billable_weekly_data ──
        billable_map, _ = get_billable_username_map(list(reviewed_ids))
        billable_login_secs = {}   # (agent_id, date) -> seconds
        nonprimary_login_secs = {}  # (agent_id, date) -> seconds
        for row in DailyAgentHours.objects.filter(
            upload__date__range=(start, end), agent_id__in=reviewed_ids
        ).values('agent_id', 'five9_username', 'login_seconds', 'upload__date'):
            aid = row['agent_id']
            d = row['upload__date']
            uname = row['five9_username'].strip().lower()
            bnames = billable_map.get(aid)
            if bnames is None or uname in bnames:
                key = (aid, d)
                billable_login_secs[key] = billable_login_secs.get(key, 0) + row['login_seconds']
            if uname in nonprimary_usernames.get(aid, ()):
                key = (aid, d)
                nonprimary_login_secs[key] = nonprimary_login_secs.get(key, 0) + row['login_seconds']

        # ── Coded time: identical is_admin_coding / is_official_admin partition test ──
        admin_ids = {aid for aid in reviewed_ids if agents_by_id[aid].is_official_admin}
        coded_secs = {}  # (agent_id, date) -> seconds
        for c in Coding.objects.filter(agent_id__in=reviewed_ids, date__range=(start, end)):
            if c.is_admin_coding != (c.agent_id in admin_ids):
                continue
            key = (c.agent_id, c.date)
            coded_secs[key] = coded_secs.get(key, 0) + c.total_seconds_count()

        # ── Scheduled hours (incl. OT): adherence.views._build_rows' per-day cell,
        # one real Monday-Sunday calendar week at a time, batching every reviewed
        # agent with a flagged day that week into one call. ────────────────────
        by_week = {}  # week_start -> {agent_id: [dates]}
        for aid in reviewed_ids:
            for d in qualifying_days[aid]:
                ws = get_week_start(d)
                by_week.setdefault(ws, {}).setdefault(aid, []).append(d)

        sched_secs = {}  # (agent_id, date) -> seconds
        for ws, agent_dates in by_week.items():
            week_dates = [ws + timedelta(days=i) for i in range(7)]
            agents_list = [agents_by_id[aid] for aid in agent_dates]
            shift_map, record_map, coded_map, ot_map, extra_hrs_map, split_labels_map, tmpl_by_agent_dow = (
                _build_maps(agents_list, week_dates)
            )
            rows = _build_rows(
                agents_list, week_dates, shift_map, record_map, coded_map,
                ot_map=ot_map, extra_hrs_map=extra_hrs_map, split_labels_map=split_labels_map,
                tmpl_by_agent_dow=tmpl_by_agent_dow, billing_settings=_safe_billing_settings(ws),
            )
            rows_by_agent = {r['agent'].pk: r for r in rows}
            for aid, dates in agent_dates.items():
                cells_by_date = {c['date']: c for c in rows_by_agent[aid]['cells']}
                for d in dates:
                    hrs = cells_by_date[d]['sched_hrs'] or Decimal('0')
                    sched_secs[(aid, d)] = int((hrs * 3600).quantize(Decimal('1')))

        # ── Flag and report ─────────────────────────────────────────────────
        flagged_by_agent = {}
        reviewed_count = 0
        not_flagged_count = 0

        for aid in sorted(reviewed_ids, key=lambda i: _display_name(agents_by_id[i])):
            for d in sorted(qualifying_days[aid]):
                reviewed_count += 1
                key = (aid, d)
                sched = sched_secs.get(key, 0)
                billable = billable_login_secs.get(key, 0)
                coded = coded_secs.get(key, 0)
                nonprimary = nonprimary_login_secs.get(key, 0)
                paid = billable + coded

                reasons = []
                if (sched - paid) > FLAG_THRESHOLD_SECS:
                    reasons.append('paid time > 5 min below scheduled hours')
                if (nonprimary - coded) > FLAG_THRESHOLD_SECS:
                    reasons.append('non-primary login > 5 min above coded time')

                if reasons:
                    flagged_by_agent.setdefault(aid, []).append(
                        (d, sched, billable, coded, nonprimary, paid, reasons)
                    )
                else:
                    not_flagged_count += 1

        if flagged_by_agent:
            self.stdout.write('FLAGGED DAYS')
            for aid, entries in flagged_by_agent.items():
                agent = agents_by_id[aid]
                self.stdout.write(f'  {_display_name(agent)} ({agent.get_employer_display()})')
                for d, sched, billable, coded, nonprimary, paid, reasons in sorted(entries):
                    self.stdout.write(
                        f'      {d.isoformat()}  scheduled={_hhmm(sched)}  '
                        f'billable_login={_hhmm(billable)}  coded={_hhmm(coded)}  '
                        f'non_primary_login={_hhmm(nonprimary)}  paid_time={_hhmm(paid)}  '
                        f'— {"; ".join(reasons)}'
                    )
            self.stdout.write('')
        else:
            self.stdout.write('No flagged days.\n')

        if skipped_ids:
            self.stdout.write('No primary marked, skipped:')
            for aid in sorted(skipped_ids, key=lambda i: _display_name(agents_by_id[i])):
                agent = agents_by_id[aid]
                self.stdout.write(f'  {_display_name(agent)} ({agent.get_employer_display()})')
            self.stdout.write('')

        self.stdout.write(f'{reviewed_count} day(s) reviewed, {not_flagged_count} not flagged.')
        self.stdout.write(self.style.SUCCESS('Nothing was written.'))
