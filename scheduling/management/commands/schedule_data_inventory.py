"""Read-only data-volume inventory for the scheduling/adherence tables.

Counting only — it opens no write transaction and never modifies, deletes or
archives a row. Not wired into any URL or view; run it by hand:

    python manage.py schedule_data_inventory

Exists because the roster query's cost is a product of how many Shift, OT,
ShiftTemplate and AdherenceRecord rows each agent has accumulated, and local
SQLite says nothing about production volumes.
"""
from collections import Counter
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Count, Max, Min
from django.utils import timezone

from adherence.models import AdherenceRecord, Coding, DailyAgentHours
from scheduling.models import (Agent, OpenOTShift, OvertimeShift, Shift,
                               ShiftBlock, ShiftTemplate, ShiftTemplateBlock)
from wfm.utils import get_week_start

# The incentive premium each type pays on top of the base OT hour, matching
# finance.views._get_billable_weekly_data: power_hour adds a full extra hour of
# pay (ot_pow * hourly_mxn), time_and_a_half adds half (ot_1_5 * hourly_mxn * 0.5).
# 'none' pays no premium at all, so a duplicate of it costs nothing.
OT_PREMIUM = {
    'power_hour': Decimal('1.0'),
    'time_and_a_half': Decimal('0.5'),
}

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

        self._ot_duplicates()

        # A rough sense of the fanout the old single-query roster had to materialise.
        self._p()
        self._p('CONTEXT')
        self._p(f'active agents: {Agent.objects.filter(status="active").count()}   '
                f'active + tracked + non-admin: '
                f'{Agent.objects.filter(status="active", track_attendance=True, is_official_admin=False).count()}')
        self._p('Nothing was written.')

    # ── OT duplicate detail ──────────────────────────────────────────────
    def _ot_duplicates(self):
        """Exact-duplicate OT rows, what they cost, and which path created them.

        The hotspot listing above groups by (agent, date) only, which conflates
        three different things: real duplication, legitimate split OT (several
        rows on one day at DIFFERENT times), and already-cancelled rows. This
        section separates them. Counting only — nothing is written.
        """
        # A duplicate is the SAME slot: same agent, date, start AND end. Cancelled
        # rows are excluded because every consumer already ignores them.
        rows = list(
            OvertimeShift.objects.exclude(status='cancelled')
            .select_related('agent').order_by('pk')
        )
        groups = {}
        for r in rows:
            groups.setdefault((r.agent_id, r.date, r.start_time, r.end_time), []).append(r)
        dups = {k: v for k, v in groups.items() if len(v) > 1}

        self._p()
        self._p('(1) EXACT-DUPLICATE OT ROWS — same agent + date + start + end, cancelled excluded')
        if not dups:
            self._p('  none — no agent/date/time slot carries more than one row')
            self._p()
            self._p('(2) MONEY-EXPOSED DUPLICATES')
            self._p('  none')
            self._p()
            self._p('(3) ORIGIN OF THE EXTRA ROWS')
            self._p('  none')
            return

        extra_rows = [r for grp in dups.values() for r in grp[1:]]
        self._p(f'  duplicated slots: {len(dups)}   extra rows: {len(extra_rows)}   '
                f'(a slot legitimately holds one row)')
        self._p('  top 15 slots by row count:')
        for key, grp in sorted(dups.items(), key=lambda kv: (-len(kv[1]), kv[0][1]))[:15]:
            aid, d, st, en = key
            st_mix = '  '.join(f'{k}={v}' for k, v in sorted(Counter(r.status for r in grp).items()))
            in_mix = '  '.join(f'{k}={v}' for k, v in sorted(Counter(r.incentive_type for r in grp).items()))
            self._p(f'  agent {aid} {d} {st:%H:%M}-{en:%H:%M}: '
                    f'{len(grp)} rows ({len(grp) - 1} extra)')
            self._p(f'      status: {st_mix}')
            self._p(f'      incentive: {in_mix}')

        # Money. finance.views._get_billable_weekly_data sums total_shift_hours()
        # over EVERY status='completed' row per agent-week with no dedupe, then
        # multiplies by the agent's hourly_rate and the incentive premium. So within
        # one duplicated slot the FIRST completed row is the legitimate payment and
        # every further completed row is overpay. Rows left pending or marked no_show
        # pay nothing, and so does incentive_type='none'.
        paid = []
        for grp in dups.values():
            completed = [r for r in grp if r.status == 'completed']
            for r in completed[1:]:
                premium = OT_PREMIUM.get(r.incentive_type)
                if premium is None:
                    continue
                rate = r.agent.hourly_rate or Decimal('0')
                mxn = (r.total_shift_hours() * rate * premium).quantize(Decimal('0.01'))
                paid.append((r, mxn))

        self._p()
        self._p("(2) MONEY-EXPOSED DUPLICATES — extra rows that are status='completed' "
                'AND incentivized')
        if not paid:
            self._p('  none — no duplicated slot has more than one completed incentivized row')
        else:
            total = sum(m for _, m in paid)
            self._p(f'  overpaying extra rows: {len(paid)}   total exposure: {total} MXN')
            self._p('  top 15 by amount:')
            for r, mxn in sorted(paid, key=lambda rm: -rm[1])[:15]:
                self._p(f'  agent {r.agent_id} {r.date} '
                        f'{r.start_time:%H:%M}-{r.end_time:%H:%M} '
                        f'{r.incentive_type}: {mxn} MXN '
                        f'({r.total_shift_hours()}h @ {r.agent.hourly_rate or 0})')

            self._p()
            self._p('    per week (is this recent or long-running?)')
            per_week = {}
            for r, mxn in paid:
                ws = get_week_start(r.date)
                n, amt = per_week.get(ws, (0, Decimal('0')))
                per_week[ws] = (n + 1, amt + mxn)
            self._p(f'    {"week of":<14}{"rows":>6}{"MXN":>14}')
            for ws in sorted(per_week):
                n, amt = per_week[ws]
                self._p(f'    {ws.isoformat():<14}{n:>6}{str(amt):>14}')

        # Origin. ot_claim_approve (agent self-service) always leaves the OpenOTShift
        # pointing at the row it created via assigned_shift; overtime_week never does.
        # That FK is the only durable record of which path wrote a row.
        from_posting = set(
            OpenOTShift.objects.filter(assigned_shift__isnull=False)
            .values_list('assigned_shift_id', flat=True)
        )
        self._p()
        self._p('(3) ORIGIN OF THE EXTRA ROWS — which write path created them')
        claim_extra = sum(1 for r in extra_rows if r.pk in from_posting)
        self._p(f'  with an open posting (ot_claim_approve — agent self-service): {claim_extra}')
        self._p(f'  without one          (overtime_week — coordinator OT editor): '
                f'{len(extra_rows) - claim_extra}')
        # Which row in a slot counts as "the extra" is arbitrary (first-by-pk is kept),
        # so the same split across every row in a duplicated slot is the safer read.
        all_dup_rows = [r for grp in dups.values() for r in grp]
        claim_all = sum(1 for r in all_dup_rows if r.pk in from_posting)
        self._p(f'  for reference, across ALL {len(all_dup_rows)} rows in duplicated slots: '
                f'{claim_all} from a posting, {len(all_dup_rows) - claim_all} not')
