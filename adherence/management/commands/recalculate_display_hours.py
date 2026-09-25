"""One-time recalculation of stored adherence DISPLAY hours after the
primary-account-only rule (Step 1). Past weeks render from the stored
AdherenceRecord.actual_hours on the Adherence tab, My Adherence, the combined
Adherence export, adherence.views.payroll_export, Records -> Hours, Records ->
Attendance and agent detail -- none of those recompute live, so a week's
stored value does not self-correct at deploy. This command recomputes it,
using adherence.views._compute_display_hours -- the exact same arithmetic
function the upload/rematch/coding-refresh write paths call. No new math.

Deliberately a NEW file, not a change to recalculate_actual_hours (which
stays billable-based, hardcodes 0.125, and must never be run again after
this deploy -- running it would put extra-account time straight back).

Preview by default: writes nothing.

    python manage.py recalculate_display_hours
    python manage.py recalculate_display_hours --start 2026-08-24 --end 2026-09-27
    python manage.py recalculate_display_hours --agent 123

--apply writes the changed rows, ALL IN ONE TRANSACTION (all-or-nothing --
a failure partway through leaves nothing written), then one summary
Activity Log entry:

    python manage.py recalculate_display_hours --apply

A candidate (agent, date) is in scope only if ALL of:
  - within --start/--end (default 2026-08-24 to 2026-09-27)
  - an AdherenceRecord already exists for it with actual_hours NOT NULL
  - the agent is NOT an Official Admin (their display never reads this
    stored value -- see finance.views._apply_live_login_hours)
  - at least one DailyAgentHours row exists for that agent on that date

This mirrors adherence.views._reconcile_stale_actual_hours (UPDATE existing
non-null rows only) rather than _zero_missing_scheduled (which CREATES rows
from TODAY's schedule state -- replaying that over historical weeks would
invent attendance rows no upload ever produced). The "has a Daily Hours row"
requirement protects a supervisor's hand-typed actual_hours on a day with no
upload behind it (adherence.views.save_adherence_cell) -- there is nothing
for this command to recompute there.

Never touches DailyAgentHours, Coding, status, finance or nomina. Never
re-matches unmatched rows. Never creates an AdherenceRecord -- structurally,
since every write is an update to an already-selected row -- so it can never
create one for an Official Admin either.

Running --apply a second time changes nothing: every affected row is already
correct, so the diff is empty and no Activity Log entry is written.
"""
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from adherence.models import AdherenceRecord, DailyAgentHours, Coding
from adherence.views import _compute_display_hours
from adherence.management.commands.uncoded_extra_account_review import _safe_billing_settings
from scheduling.models import log_action
from wfm.utils import get_adherence_primary_resolver, get_week_start

DEFAULT_START = date(2026, 8, 24)
DEFAULT_END = date(2026, 9, 27)


def _display_name(agent):
    return agent.agent_name or agent.user.get_full_name() or agent.user.get_username()


def _hhmm(hours):
    """Decimal hours -> 'H:MM', rounded to the nearest minute -- the same
    granularity the Adherence cell displays."""
    total_minutes = round(float(hours) * 60)
    h, m = divmod(int(total_minutes), 60)
    return f'{h}:{m:02d}'


class Command(BaseCommand):
    help = ('Recalculate stored adherence display hours for a date range using the '
            'primary-account-only rule (preview by default; --apply to write).')

    def add_arguments(self, parser):
        parser.add_argument('--start', type=date.fromisoformat, default=DEFAULT_START,
                            help=f'Start date, ISO format (default {DEFAULT_START.isoformat()}).')
        parser.add_argument('--end', type=date.fromisoformat, default=DEFAULT_END,
                            help=f'End date, ISO format (default {DEFAULT_END.isoformat()}).')
        parser.add_argument('--apply', action='store_true',
                            help='Write the changed rows. Without this flag, preview only.')
        parser.add_argument('--agent', type=int, default=None, dest='agent_pk',
                            help='Narrow to one agent pk, for spot-checking before a full apply.')

    def handle(self, *args, **options):
        start, end = options['start'], options['end']
        apply_changes = options['apply']
        agent_pk = options.get('agent_pk')

        self.stdout.write(
            f'Adherence display-hours recalculation — {start.isoformat()} to {end.isoformat()}'
        )
        self.stdout.write(
            'APPLY MODE — writing changed rows.' if apply_changes else
            'PREVIEW — nothing will be written. Re-run with --apply to write.'
        )
        self.stdout.write('')

        plan = self._plan(start, end, agent_pk)

        if not plan:
            self.stdout.write('No agent-day would change.')
            self.stdout.write(self.style.SUCCESS('Nothing was written.'))
            return

        self.stdout.write(
            f"{'DATE':<12}{'AGENT':<24}{'CELL BEFORE → AFTER':<26}STORED BEFORE → AFTER"
        )
        for row in plan:
            cell_change = f"{row['cell_before']} → {row['cell_after']}"
            stored_change = f"{row['stored_before']} → {row['stored_after']}"
            self.stdout.write(
                f"{row['date'].isoformat():<12}{row['agent_name']:<24}{cell_change:<26}{stored_change}"
            )

        self.stdout.write('')
        self.stdout.write(f'{len(plan)} agent-day(s) would change.')

        if not apply_changes:
            self.stdout.write(self.style.SUCCESS('Nothing was written.'))
            return

        with transaction.atomic():
            now = timezone.now()
            to_update = []
            for row in plan:
                rec = row['record']
                rec.actual_hours = row['new_stored']
                rec.updated_at = now
                to_update.append(rec)
            AdherenceRecord.objects.bulk_update(to_update, ['actual_hours', 'updated_at'])
            log_action(
                None,
                'Recalculated adherence display hours',
                f'Step 2 primary-account recalculation {start.isoformat()} to {end.isoformat()} — '
                f'{len(plan)} agent-day(s) updated',
            )

        self.stdout.write(self.style.SUCCESS(
            f'{len(plan)} agent-day(s) updated. Activity Log entry written.'
        ))

    def _plan(self, start, end, agent_pk=None):
        """Read-only. Returns the list of agent-days whose stored actual_hours
        would change, sorted by (date, agent name). Preview and --apply both
        call exactly this, so the two runs can never disagree about what
        would change."""
        candidates = AdherenceRecord.objects.filter(
            date__gte=start, date__lte=end,
            actual_hours__isnull=False,
            agent__is_official_admin=False,
        ).select_related('agent__user')
        if agent_pk:
            candidates = candidates.filter(agent_id=agent_pk)
        candidates = list(candidates)
        if not candidates:
            return []

        agent_ids = {r.agent_id for r in candidates}
        dates = {r.date for r in candidates}

        # Only upload-owned days are candidates: at least one DailyAgentHours
        # row for that agent on that date. Protects a supervisor's hand-typed
        # actual_hours on a day with no upload behind it.
        dah_rows = list(DailyAgentHours.objects.filter(
            agent_id__in=agent_ids, upload__date__in=dates
        ).values('agent_id', 'upload__date', 'five9_username', 'login_seconds', 'not_ready_seconds'))
        days_with_rows = {(r['agent_id'], r['upload__date']) for r in dah_rows}

        counts_for_adherence = get_adherence_primary_resolver(agent_ids)

        login_secs_map = {}
        nr_secs_map = {}
        for r in dah_rows:
            key = (r['agent_id'], r['upload__date'])
            if counts_for_adherence(r['agent_id'], r['five9_username'], r['upload__date']):
                login_secs_map[key] = login_secs_map.get(key, 0) + r['login_seconds']
                nr_secs_map[key] = nr_secs_map.get(key, 0) + r['not_ready_seconds']

        # Same Coding query shape upload_daily_file uses (no is_admin_coding
        # filter) -- every candidate here is already restricted to non-admin
        # agents, so this matches _refresh_actual_hours's is_admin_coding=False
        # scan for any agent that has no stray admin coding.
        coded_secs_map = {}
        for c in Coding.objects.filter(agent_id__in=agent_ids, date__in=dates, is_admin_coding=False):
            key = (c.agent_id, c.date)
            coded_secs_map[key] = coded_secs_map.get(key, 0) + c.total_seconds_count()

        # Read-only historical nr_ratio, one lookup per distinct week -- never
        # BillingSettings.get_for_week's get_or_create(pk=1) fallback.
        settings_cache = {}

        plan = []
        for rec in candidates:
            key = (rec.agent_id, rec.date)
            if key not in days_with_rows:
                continue  # no Daily Hours row that day at all -- not in scope

            week_start = get_week_start(rec.date)
            if week_start not in settings_cache:
                settings_cache[week_start] = _safe_billing_settings(week_start)
            nr_ratio = settings_cache[week_start].nr_ratio

            login_secs = login_secs_map.get(key, 0)
            not_ready_secs = nr_secs_map.get(key, 0)
            coded_secs = coded_secs_map.get(key, 0)

            new_stored = _compute_display_hours(login_secs, not_ready_secs, coded_secs, nr_ratio)
            old_stored = rec.actual_hours
            if old_stored == new_stored:
                continue

            coded_hrs = Decimal(str(coded_secs)) / Decimal('3600')
            plan.append({
                'date': rec.date,
                'agent_name': _display_name(rec.agent),
                'record': rec,
                'stored_before': old_stored,
                'stored_after': new_stored,
                'new_stored': new_stored,
                'cell_before': _hhmm(old_stored + coded_hrs),
                'cell_after': _hhmm(new_stored + coded_hrs),
            })

        plan.sort(key=lambda r: (r['date'], r['agent_name']))
        return plan
