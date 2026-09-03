"""Read-only parity check for the OT incentive top-ups.

Runs the current OT summation from finance.views._get_billable_weekly_data and the
deduped summation that is meant to replace it against the same database, week by
week, and prints any week where the money differs. Read-only: it opens no write
transaction and never modifies a row. Not wired into any URL or view; run it by hand:

    python manage.py verify_ot_topups
    python manage.py verify_ot_topups --weeks 52

Why this exists. The engine loops every status='completed' OvertimeShift and does a
plain += of total_shift_hours() per incentive type; those totals become ph_topup_mxn
(x full rate) and ot_1_5_topup_mxn (x half rate) and land in total_pay_mxn. So a slot
recorded twice pays its premium twice. adherence.views._build_maps already collapses
exact duplicates on (start_time, end_time, status) per agent/day, and matching that
collapse here is the fix. The Sep 2 2026 inventory measured zero exposure -- every
existing duplicate sits at pending -- so the fix should move no number anywhere. This
command is what proves that on real data before the engine changes.

Scope note: the Finance payroll report recomputes live for every week, forever, so a
difference in ANY historical week would make that week's report show less than what
was actually paid. Run it once with a wide --weeks as well as the 12-week default.

Exits 1 if any week differs, so it can gate a deploy.
"""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand
from django.utils import timezone

from scheduling.models import Agent, OvertimeShift
from wfm.utils import get_week_start


def _topups_original(rows):
    """The current summation in _get_billable_weekly_data, kept verbatim.

    A plain += per incentive type over every completed row, with no dedupe, so a slot
    recorded twice contributes its hours twice. Order-independent -- these are plain
    sums -- so the pk ordering the caller applies makes no difference here.

    Returns (ot_regular_map, ot_1_5_map, ot_power_map), each agent_id -> Decimal hours.
    """
    ot_regular_map = {}
    ot_1_5_map = {}
    ot_power_map = {}
    for ot in rows:
        hrs = ot.total_shift_hours()
        if ot.incentive_type == 'none':
            ot_regular_map[ot.agent_id] = ot_regular_map.get(ot.agent_id, Decimal('0')) + hrs
        elif ot.incentive_type == 'time_and_a_half':
            ot_1_5_map[ot.agent_id] = ot_1_5_map.get(ot.agent_id, Decimal('0')) + hrs
        elif ot.incentive_type == 'power_hour':
            ot_power_map[ot.agent_id] = ot_power_map.get(ot.agent_id, Decimal('0')) + hrs
    return ot_regular_map, ot_1_5_map, ot_power_map


def _topups_deduped(rows):
    """The replacement summation: the same loop, collapsing exact-duplicate rows.

    Keyed on (start_time, end_time, status) inside a per-(agent_id, date) seen set --
    the identical key adherence.views._build_maps uses. Split OT is untouched by
    construction: two rows on one day at different hours have different keys and both
    still count. incentive_type is deliberately NOT in the key; including it would keep
    both rows of a slot recorded once as power_hour and once as time_and_a_half and pay
    both premiums, which is the exact overpay this closes. `rows` must arrive ordered by
    pk so that "the first row wins" is deterministic when a duplicated slot's rows
    disagree on incentive_type -- the same attribution schedule_data_inventory uses.

    Returns (ot_regular_map, ot_1_5_map, ot_power_map), each agent_id -> Decimal hours.
    """
    ot_regular_map = {}
    ot_1_5_map = {}
    ot_power_map = {}
    seen_by_agent_day = {}
    for ot in rows:
        seen = seen_by_agent_day.setdefault((ot.agent_id, ot.date), set())
        dedup_key = (ot.start_time, ot.end_time, ot.status)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        hrs = ot.total_shift_hours()
        if ot.incentive_type == 'none':
            ot_regular_map[ot.agent_id] = ot_regular_map.get(ot.agent_id, Decimal('0')) + hrs
        elif ot.incentive_type == 'time_and_a_half':
            ot_1_5_map[ot.agent_id] = ot_1_5_map.get(ot.agent_id, Decimal('0')) + hrs
        elif ot.incentive_type == 'power_hour':
            ot_power_map[ot.agent_id] = ot_power_map.get(ot.agent_id, Decimal('0')) + hrs
    return ot_regular_map, ot_1_5_map, ot_power_map


def _topup_money(ot_1_5_hrs, ot_power_hrs, hourly_rate):
    """The engine's two top-up expressions, verbatim, including the quantize.

    ph_topup_mxn      = power-hour hours x full rate
    ot_1_5_topup_mxn  = 1.5x hours x rate x 0.5
    Depends only on agent.hourly_rate -- no BillingSettings, so there is no historical
    rate question here. Returns (ph_topup_mxn, ot_1_5_topup_mxn).
    """
    ph = (ot_power_hrs * hourly_rate).quantize(Decimal('0.01'), ROUND_HALF_UP)
    ot15 = (ot_1_5_hrs * hourly_rate * Decimal('0.5')).quantize(Decimal('0.01'), ROUND_HALF_UP)
    return ph, ot15


class Command(BaseCommand):
    help = 'Read-only: compare the current and deduped OT incentive top-ups (no writes).'

    def add_arguments(self, parser):
        parser.add_argument('--weeks', type=int, default=12,
                            help='How many weeks back to check, ending with the current week (default 12).')

    def handle(self, *args, **options):
        weeks = max(1, options['weeks'])
        current = get_week_start(timezone.localdate())
        self.stdout.write(f'Read-only OT top-up parity check — {weeks} weeks ending {current.isoformat()}')
        self.stdout.write('(no rows modified)\n')

        mismatches = []
        for i in range(weeks - 1, -1, -1):
            ws = current - timedelta(weeks=i)
            wd = [ws + timedelta(days=j) for j in range(7)]

            # Every agent with a completed OT row that week, not a report roster --
            # wider than any single screen, so drift that surfaces on only one report
            # cannot be missed. order_by('pk') is what makes the deduped side
            # deterministic; see _topups_deduped.
            rows = list(
                OvertimeShift.objects.filter(date__in=wd, status='completed')
                .select_related('agent').order_by('pk')
            )

            old_reg, old_1_5, old_pow = _topups_original(rows)
            new_reg, new_1_5, new_pow = _topups_deduped(rows)

            agent_ids = set(old_reg) | set(old_1_5) | set(old_pow)
            rates = {r.agent_id: (r.agent.hourly_rate or Decimal('0')) for r in rows}

            diffs = []
            for aid in sorted(agent_ids):
                rate = rates.get(aid, Decimal('0'))
                old_ph, old_ot15 = _topup_money(
                    old_1_5.get(aid, Decimal('0')), old_pow.get(aid, Decimal('0')), rate)
                new_ph, new_ot15 = _topup_money(
                    new_1_5.get(aid, Decimal('0')), new_pow.get(aid, Decimal('0')), rate)
                if old_ph != new_ph or old_ot15 != new_ot15:
                    diffs.append((aid, old_ph, new_ph, old_ot15, new_ot15))

            if diffs:
                mismatches.append((ws, diffs))
                self.stdout.write(f'  {ws.isoformat()}  DIFFERS  {len(diffs)} agent(s)')
                for aid, old_ph, new_ph, old_ot15, new_ot15 in diffs:
                    self.stdout.write(f'      {self._describe(aid)}')
                    if old_ph != new_ph:
                        self.stdout.write(f'          PH top-up:   {old_ph} -> {new_ph} MXN '
                                          f'(delta {new_ph - old_ph})')
                    if old_ot15 != new_ot15:
                        self.stdout.write(f'          1.5x top-up: {old_ot15} -> {new_ot15} MXN '
                                          f'(delta {new_ot15 - old_ot15})')
            else:
                self.stdout.write(f'  {ws.isoformat()}  match    '
                                  f'({len(agent_ids)} agent(s) with completed OT)')

        self.stdout.write('')
        if mismatches:
            total = sum(
                (new_ph - old_ph) + (new_ot15 - old_ot15)
                for _, diffs in mismatches
                for _, old_ph, new_ph, old_ot15, new_ot15 in diffs
            )
            self.stdout.write(self.style.ERROR(
                f'{len(mismatches)} of {weeks} weeks differ (total {total} MXN) — '
                f'do not deploy the OT dedupe until each difference is understood. '
                f'A week already paid out would then show less than what was paid. '
                f'Nothing was written.'))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(
            f'All {weeks} weeks identical. Nothing was written.'))

    def _describe(self, pk):
        agent = Agent.objects.filter(pk=pk).select_related('user').first()
        if not agent:
            return f'agent {pk} (not found)'
        return (f'agent {pk} — {agent.agent_name or agent.user.get_username()} '
                f'(rate={agent.hourly_rate or 0})')
