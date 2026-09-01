"""Read-only parity check for the Adherence roster helper.

Runs the original single-query implementation and the current
_get_adherence_agent_pks against the same database, week by week, and prints any
difference in the pk sets. Read-only: it opens no write transaction and never
modifies a row. Not wired into any URL or view; run it by hand:

    python manage.py verify_adherence_roster
    python manage.py verify_adherence_roster --weeks 12

The helper decides who appears on the Adherence tab and in the combined
Adherence export, so a changed pk set would silently drop agents from the tab
and from their adherence bonus. The unit tests pin this against a fixture; this
command pins it against real data.

Exits 1 if any week differs, so it can gate a deploy.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from adherence.views import _get_adherence_agent_pks
from scheduling.models import Agent
from wfm.utils import get_week_start


def _roster_original(week_dates, week_start):
    """The single-query implementation the split replaced, kept verbatim.

    Four OR'd conditions across four multi-valued relations, which gave each its
    own unrestricted LEFT JOIN and pushed every date predicate into the WHERE.
    Slow on production-sized data by design of the shape -- that is the point of
    the comparison -- so this command can take a while per week.
    """
    return set(Agent.objects.filter(
        Q(status='active', track_attendance=True, is_official_admin=False) |
        Q(status='inactive', separations__status='finalized',
          separations__remove_from_adherence_date__gt=week_start)
    ).filter(
        Q(shifts__date__in=week_dates) |
        Q(overtime_shifts__date__in=week_dates) |
        Q(shift_templates__isnull=False) |
        Q(adherence_records__date__in=week_dates)
    ).filter(
        Q(adherence_start_date__isnull=True) | Q(adherence_start_date__lte=week_start)
    ).values_list('pk', flat=True).distinct())


class Command(BaseCommand):
    help = 'Read-only: compare the old and new Adherence roster implementations (no writes).'

    def add_arguments(self, parser):
        parser.add_argument('--weeks', type=int, default=8,
                            help='How many weeks back to check, ending with the current week (default 8).')

    def handle(self, *args, **options):
        weeks = max(1, options['weeks'])
        current = get_week_start(timezone.localdate())
        self.stdout.write(f'Read-only roster parity check — {weeks} weeks ending {current.isoformat()}')
        self.stdout.write('(no rows modified)\n')

        mismatches = []
        for i in range(weeks - 1, -1, -1):
            ws = current - timedelta(weeks=i)
            wd = [ws + timedelta(days=j) for j in range(7)]

            old = _roster_original(wd, ws)
            # The helper caches per (week_start, supervisor_id); clear so this reads
            # the database rather than a warm entry from a previous week's call.
            cache.delete(f'adh_pks_{ws.isoformat()}_all')
            new = _get_adherence_agent_pks(wd, ws)

            only_old = sorted(old - new)
            only_new = sorted(new - old)
            if only_old or only_new:
                mismatches.append((ws, only_old, only_new))
                self.stdout.write(f'  {ws.isoformat()}  DIFFERS  old={len(old)} new={len(new)}')
                for pk in only_old:
                    self.stdout.write(f'      dropped by the new code: {self._describe(pk)}')
                for pk in only_new:
                    self.stdout.write(f'      added by the new code:   {self._describe(pk)}')
            else:
                self.stdout.write(f'  {ws.isoformat()}  match    ({len(old)} agents)')

        self.stdout.write('')
        if mismatches:
            self.stdout.write(self.style.ERROR(
                f'{len(mismatches)} of {weeks} weeks differ — do not deploy the split roster.'))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(
            f'All {weeks} weeks identical. Nothing was written.'))

    def _describe(self, pk):
        agent = Agent.objects.filter(pk=pk).select_related('user').first()
        if not agent:
            return f'agent {pk} (not found)'
        return f'agent {pk} — {agent.agent_name or agent.user.get_username()} (status={agent.status})'
