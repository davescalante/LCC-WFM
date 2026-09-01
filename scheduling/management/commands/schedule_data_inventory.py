"""Read-only data-volume inventory for the scheduling/adherence tables.

Counting only — it opens no write transaction and never modifies, deletes or
archives a row. Not wired into any URL or view; run it by hand:

    python manage.py schedule_data_inventory

Exists because the roster query's cost is a product of how many Shift, OT,
ShiftTemplate and AdherenceRecord rows each agent has accumulated, and local
SQLite says nothing about production volumes.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Max, Min
from django.utils import timezone

from adherence.models import AdherenceRecord, Coding, DailyAgentHours
from scheduling.models import (Agent, OvertimeShift, Shift, ShiftBlock,
                               ShiftTemplate, ShiftTemplateBlock)

# (model, date field or None) — the eight tables asked for, in order.
TABLES = [
    (Shift, 'date'),
    (ShiftBlock, None),
    (ShiftTemplate, 'effective_from'),
    (ShiftTemplateBlock, None),
    (OvertimeShift, 'date'),
    (AdherenceRecord, 'date'),
    (Coding, 'date'),
    (DailyAgentHours, None),
]


class Command(BaseCommand):
    help = 'Read-only row counts, date ranges and future-dated counts (no writes).'

    def _p(self, line=''):
        self.stdout.write(line)

    def handle(self, *args, **options):
        today = timezone.localdate()
        self._p(f'Read-only inventory — {today.isoformat()}  (no rows modified)')

        self._p()
        self._p('ROW COUNTS / DATE RANGE / FUTURE-DATED')
        self._p(f'{"table":<22}{"rows":>10}{"earliest":>14}{"latest":>14}{"future":>10}')
        for model, field in TABLES:
            name = model.__name__
            total = model.objects.count()
            if not field:
                self._p(f'{name:<22}{total:>10}{"(no date)":>14}{"":>14}{"":>10}')
                continue
            agg = model.objects.aggregate(mn=Min(field), mx=Max(field))
            future = model.objects.filter(**{f'{field}__gt': today}).count()
            mn = agg['mn'].isoformat() if agg['mn'] else '-'
            mx = agg['mx'].isoformat() if agg['mx'] else '-'
            self._p(f'{name:<22}{total:>10}{mn:>14}{mx:>14}{future:>10}')
        self._p('ShiftTemplate has no per-row date; effective_from is used above.')
        self._p('ShiftBlock/ShiftTemplateBlock/DailyAgentHours are dated via their parent.')

        # Far-future writes: shift_week's date_range branch has no upper bound, so
        # a mistyped end date can write years of Shift rows in one request.
        self._p()
        self._p('FAR-FUTURE SHIFT ROWS (beyond 8 weeks out)')
        horizon = today + timedelta(weeks=8)
        far = Shift.objects.filter(date__gt=horizon)
        self._p(f'Shift rows dated after {horizon.isoformat()}: {far.count()}')
        for row in (far.values('agent_id')
                    .annotate(n=Count('id'), last=Max('date')).order_by('-n')[:10]):
            self._p(f'  agent {row["agent_id"]}: {row["n"]} rows, latest {row["last"]}')

        # ShiftTemplate accumulation. 7 rows per agent is one healthy weekly
        # recurring schedule; each distinct effective date adds another 7.
        self._p()
        self._p('SHIFTTEMPLATE ROWS PER AGENT (7 = one clean weekly schedule)')
        per_agent = list(ShiftTemplate.objects.values('agent_id')
                         .annotate(n=Count('id')).order_by('-n'))
        if per_agent:
            counts = [r['n'] for r in per_agent]
            mid = sorted(counts)[len(counts) // 2]
            self._p(f'agents with templates: {len(per_agent)}   median: {mid}   '
                    f'max: {counts[0]}   total: {sum(counts)}')
            buckets = [('7 or fewer', 0), ('8-21', 0), ('22-49', 0), ('50-99', 0), ('100+', 0)]
            for n in counts:
                idx = 0 if n <= 7 else 1 if n <= 21 else 2 if n <= 49 else 3 if n <= 99 else 4
                buckets[idx] = (buckets[idx][0], buckets[idx][1] + 1)
            for label, n in buckets:
                self._p(f'  {label:<12}{n:>5} agents')
            self._p('top 10 by row count:')
            for row in per_agent[:10]:
                self._p(f'  agent {row["agent_id"]}: {row["n"]} rows')
        else:
            self._p('none')

        # OvertimeShift has no uniqueness guard on (agent, date) — migration 0018
        # dropped it to allow split OT — so duplicate rows can pile up unbounded.
        self._p()
        self._p('OVERTIMESHIFT ROWS PER AGENT/DAY (duplicate hotspots)')
        hot = list(OvertimeShift.objects.values('agent_id', 'date')
                   .annotate(n=Count('id')).filter(n__gt=4).order_by('-n')[:15])
        if hot:
            for row in hot:
                self._p(f'  agent {row["agent_id"]} {row["date"]}: {row["n"]} rows')
        else:
            self._p('  none above 4 rows on a single agent/day')

        # A rough sense of the fanout the old single-query roster had to materialise.
        self._p()
        self._p('CONTEXT')
        self._p(f'active agents: {Agent.objects.filter(status="active").count()}   '
                f'active + tracked + non-admin: '
                f'{Agent.objects.filter(status="active", track_attendance=True, is_official_admin=False).count()}')
        self._p('Nothing was written.')
