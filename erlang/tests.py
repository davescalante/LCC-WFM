from django.test import TestCase
from .calculator import (
    erlang_c, service_level, agents_required,
    parse_aht, calculate_staffing,
)


class ErlangCTests(TestCase):
    def test_overloaded_returns_one(self):
        # When agents ≤ traffic intensity the queue can never clear
        self.assertEqual(erlang_c(agents=2, traffic_intensity=3.0), 1.0)
        self.assertEqual(erlang_c(agents=5, traffic_intensity=5.0), 1.0)

    def test_valid_probability_range(self):
        p = erlang_c(agents=10, traffic_intensity=5.0)
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)

    def test_more_agents_lower_probability(self):
        # More agents → lower probability of waiting
        p_few = erlang_c(agents=6, traffic_intensity=5.0)
        p_many = erlang_c(agents=20, traffic_intensity=5.0)
        self.assertGreater(p_few, p_many)


class ServiceLevelTests(TestCase):
    def test_overloaded_returns_zero(self):
        # Insufficient staffing → 0% SL
        result = service_level(agents=2, calls_per_hour=100, avg_handle_time=300, target_answer_time=20)
        self.assertEqual(result, 0.0)

    def test_high_staffing_near_100pct(self):
        result = service_level(agents=50, calls_per_hour=10, avg_handle_time=180, target_answer_time=20)
        self.assertGreater(result, 95.0)

    def test_result_capped_at_100(self):
        result = service_level(agents=100, calls_per_hour=5, avg_handle_time=60, target_answer_time=20)
        self.assertLessEqual(result, 100.0)
        self.assertGreaterEqual(result, 0.0)

    def test_more_agents_higher_sl(self):
        sl_low = service_level(8, 60, 300, 20)
        sl_high = service_level(20, 60, 300, 20)
        self.assertGreater(sl_high, sl_low)


class AgentsRequiredTests(TestCase):
    def test_returns_integer(self):
        n = agents_required(60, 300, 80.0, 20)
        self.assertIsInstance(n, int)

    def test_zero_calls_returns_one(self):
        self.assertEqual(agents_required(0, 300, 80.0, 20), 1)
        self.assertEqual(agents_required(60, 0, 80.0, 20), 1)

    def test_more_calls_requires_more_agents(self):
        low = agents_required(30, 300, 80.0, 20)
        high = agents_required(120, 300, 80.0, 20)
        self.assertLess(low, high)

    def test_higher_target_sl_requires_more_agents(self):
        n_80 = agents_required(60, 300, 80.0, 20)
        n_95 = agents_required(60, 300, 95.0, 20)
        self.assertLessEqual(n_80, n_95)

    def test_achieved_sl_meets_target(self):
        target = 80.0
        n = agents_required(60, 300, target, 20)
        achieved = service_level(n, 60, 300, 20)
        self.assertGreaterEqual(achieved, target)


class ParseAHTTests(TestCase):
    def test_hhmmss(self):
        self.assertEqual(parse_aht('0:07:30'), 450)

    def test_hhmmss_with_hours(self):
        self.assertEqual(parse_aht('1:00:00'), 3600)

    def test_hhmm_two_part(self):
        # Two-part strings are treated as H:M (not M:S)
        self.assertEqual(parse_aht('0:07'), 420)  # 7 minutes

    def test_strips_milliseconds(self):
        self.assertEqual(parse_aht('0:07:30.500'), 450)

    def test_empty_string_returns_zero(self):
        self.assertEqual(parse_aht(''), 0)

    def test_none_returns_zero(self):
        self.assertEqual(parse_aht(None), 0)

    def test_invalid_returns_zero(self):
        self.assertEqual(parse_aht('not-a-time'), 0)


class CalculateStaffingTests(TestCase):
    def _rows(self):
        return [{'day': 'Mon', 'hour': 9, 'avg_calls': 60}]

    def test_shrinkage_increases_headcount(self):
        base = calculate_staffing(self._rows(), 80.0, 20, 0, 300)
        with_shrink = calculate_staffing(self._rows(), 80.0, 20, 20, 300)
        self.assertGreaterEqual(with_shrink[0]['agents_shrinkage'], base[0]['agents_shrinkage'])

    def test_zero_calls_gives_one_agent(self):
        rows = [{'day': 'Mon', 'hour': 9, 'avg_calls': 0}]
        result = calculate_staffing(rows, 80.0, 20, 0, 300)
        self.assertEqual(result[0]['agents_required'], 1)

    def test_output_contains_required_keys(self):
        result = calculate_staffing(self._rows(), 80.0, 20, 0, 300)
        row = result[0]
        for key in ('agents_required', 'agents_shrinkage', 'service_level_achieved', 'hour_label'):
            self.assertIn(key, row)

    def test_passthrough_preserves_input_fields(self):
        rows = [{'day': 'Wednesday', 'hour': 14, 'avg_calls': 45, 'custom': 'x'}]
        result = calculate_staffing(rows, 80.0, 20, 0, 300)
        self.assertEqual(result[0]['day'], 'Wednesday')
        self.assertEqual(result[0]['custom'], 'x')


from datetime import date, time, timedelta

from django.contrib.auth.models import User
from django.urls import reverse

from scheduling.models import Agent, OpenOTShift, OTShiftClaimRequest
from .models import ErlangCallRow


def _staff(username, role_type='supervisor'):
    user = User.objects.create_user(username=username, password='pw')
    return Agent.objects.create(user=user, role='admin', role_type=role_type,
                                agent_name=username.title())


class StaffingCalculatorOTVisibilityTests(TestCase):
    def setUp(self):
        self.sup = _staff('sup')
        self.sup2 = _staff('sup2')
        self.qa = _staff('qa1', role_type='qa')
        today = date.today()
        self.week_start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        self.monday = self.week_start
        ErlangCallRow.objects.create(week_start=self.week_start, day='Monday', hour=16,
                                     total_calls=300, avg_calls=100)

    def _get_monday_row(self):
        self.client.login(username='sup', password='pw')
        resp = self.client.get(reverse('erlang_calculator') + f'?week_start={self.week_start.isoformat()}')
        self.client.logout()
        monday = next(d for d in resp.context['days'] if d['name'] == 'Monday')
        return resp, monday['rows'][0]

    def _post_open_shift(self):
        return OpenOTShift.objects.create(
            date=self.monday, start_time=time(16, 0), end_time=time(18, 0),
        )

    def test_gap_with_no_postings(self):
        resp, row = self._get_monday_row()
        self.assertEqual((row['ot_open'], row['ot_filled']), (0, 0))
        self.assertGreater(row['agents_shrinkage'], 0)
        self.assertEqual(row['net_state'], 'short')
        self.assertEqual(row['net_gap'], row['agents_shrinkage'])  # scheduled is 0
        self.assertContains(resp, '+ Post OT')  # approver sees the shortcut

    def test_open_posting_reduces_net_gap(self):
        self._post_open_shift()
        resp, row = self._get_monday_row()
        self.assertEqual(row['ot_open'], 1)
        self.assertEqual(row['net_gap'], row['agents_shrinkage'] - 1)

    def test_filled_posting_counts_once_via_scheduled(self):
        posting = self._post_open_shift()
        claim = OTShiftClaimRequest.objects.create(open_shift=posting, requester=self.qa)
        self.client.login(username='sup2', password='pw')
        self.client.post(reverse('ot_claim_approve', kwargs={'pk': claim.pk}))
        self.client.logout()

        resp, row = self._get_monday_row()
        self.assertEqual((row['ot_open'], row['ot_filled']), (0, 1))
        self.assertEqual(row['scheduled_staff'], 1)  # the assigned OT shift IS the coverage
        self.assertEqual(row['net_gap'], row['agents_shrinkage'] - 1)  # no double counting

        # Cancelling the assigned shift removes the filled coverage
        posting.refresh_from_db()
        posting.assigned_shift.status = 'cancelled'
        posting.assigned_shift.save(update_fields=['status'])
        resp, row = self._get_monday_row()
        self.assertEqual((row['ot_open'], row['ot_filled']), (0, 0))
        self.assertEqual(row['scheduled_staff'], 0)

    def test_non_approver_sees_columns_but_no_button(self):
        self.client.login(username='qa1', password='pw')
        resp = self.client.get(reverse('erlang_calculator') + f'?week_start={self.week_start.isoformat()}')
        self.assertContains(resp, 'Net Gap')
        self.assertNotContains(resp, '+ Post OT')

    def test_post_from_calculator_redirects_back(self):
        self.client.login(username='sup', password='pw')
        nxt = reverse('erlang_calculator') + f'?week_start={self.week_start.isoformat()}'
        resp = self.client.post(reverse('open_ot_create'), {
            'date': self.monday.isoformat(), 'start_time': '16:00', 'end_time': '17:00',
            'incentive_type': 'none', 'count': '2', 'next': nxt,
        })
        self.assertEqual(resp.url, nxt)
        self.assertEqual(OpenOTShift.objects.count(), 2)

    def test_ot_board_day_summary(self):
        posting = self._post_open_shift()          # open, unclaimed
        requested = self._post_open_shift()        # open with pending claim
        OTShiftClaimRequest.objects.create(open_shift=requested, requester=self.qa)
        filled = self._post_open_shift()
        filled.status = 'filled'
        filled.save(update_fields=['status'])

        self.client.login(username='sup', password='pw')
        resp = self.client.get(reverse('overtime_list') + f'?week_start={self.week_start.isoformat()}')
        self.assertContains(resp, '1 open &middot; 1 pending &middot; 1 filled')


from scheduling.models import Shift, ShiftTemplate, OvertimeShift
from .views import _build_scheduled_map


class ScheduledMapOverrideTests(TestCase):
    """A per-date Shift override must fully govern its date in the Staffing
    calculator — including an override that makes the agent OFF."""

    def setUp(self):
        self.agent = _staff('caller1', role_type='regular_agent')
        self.agent.role = 'agent'
        self.agent.save()
        today = date.today()
        self.week_start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        self.saturday = self.week_start + timedelta(days=5)
        ShiftTemplate.objects.create(
            agent=self.agent, day_of_week=5,  # Saturday
            start_time=time(16, 0), end_time=time(18, 0), is_off=False,
        )

    def _count(self, hour):
        scheduled, _, _ = _build_scheduled_map(self.week_start)
        return scheduled.get(('Saturday', hour), 0)

    def test_baseline_template_counts(self):
        self.assertEqual(self._count(16), 1)
        self.assertEqual(self._count(17), 1)
        self.assertEqual(self._count(18), 0)

    def test_one_time_day_off_override_removes_agent(self):
        Shift.objects.create(agent=self.agent, date=self.saturday,
                             start_time=time(0, 0), end_time=time(0, 0), is_off=True)
        self.assertEqual(self._count(16), 0)
        self.assertEqual(self._count(17), 0)

    def test_working_override_replaces_template_hours(self):
        Shift.objects.create(agent=self.agent, date=self.saturday,
                             start_time=time(10, 0), end_time=time(12, 0), is_off=False)
        self.assertEqual(self._count(10), 1)
        self.assertEqual(self._count(11), 1)
        self.assertEqual(self._count(16), 0)  # template no longer governs
        self.assertEqual(self._count(17), 0)

    def test_newer_off_template_suppresses_older_working_one(self):
        # The setUp template is open-ended (no effective dates); a newer OFF
        # template effective this week must win, same as on the Shifts tab.
        ShiftTemplate.objects.create(
            agent=self.agent, day_of_week=5, is_off=True,
            effective_from=self.week_start,
        )
        self.assertEqual(self._count(16), 0)
        self.assertEqual(self._count(17), 0)

    def test_ot_shift_still_counts_despite_day_off_override(self):
        Shift.objects.create(agent=self.agent, date=self.saturday,
                             start_time=time(0, 0), end_time=time(0, 0), is_off=True)
        OvertimeShift.objects.create(agent=self.agent, date=self.saturday,
                                     start_time=time(16, 0), end_time=time(18, 0))
        self.assertEqual(self._count(16), 1)
        self.assertEqual(self._count(17), 1)


from adherence.models import AdherenceRecord


class ScheduledMapAdherenceExclusionTests(TestCase):
    """Scheduled Staff must exclude an agent whose adherence status that day
    means they aren't actually on the floor (V/VTO/LOA/Holiday/IMSS/S), while
    never touching OT coverage or any pay/bonus/COS status set."""

    def setUp(self):
        self.agent = _staff('caller2', role_type='regular_agent')
        self.agent.role = 'agent'
        self.agent.save()
        today = date.today()
        self.week_start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        self.monday = self.week_start
        ShiftTemplate.objects.create(
            agent=self.agent, day_of_week=0,  # Monday
            start_time=time(9, 0), end_time=time(17, 0), is_off=False,
        )

    def _count(self, hour):
        scheduled, _, _ = _build_scheduled_map(self.week_start)
        return scheduled.get(('Monday', hour), 0)

    def _set_status(self, status):
        AdherenceRecord.objects.update_or_create(
            agent=self.agent, date=self.monday, defaults={'status': status},
        )

    def test_each_excluded_status_removes_agent_from_count(self):
        for status in ('V', 'VTO', 'LOA', 'Holiday', 'IMSS', 'S'):
            self._set_status(status)
            self.assertEqual(self._count(10), 0, f'{status} should exclude the agent')

    def test_partial_work_statuses_do_not_exclude(self):
        for status in ('P+VTO', 'T+VTO'):
            self._set_status(status)
            self.assertEqual(self._count(10), 1, f'{status} should NOT exclude the agent')

    def test_absent_ncns_and_tardy_do_not_exclude(self):
        for status in ('Absent', 'NCNS', 'T'):
            self._set_status(status)
            self.assertEqual(self._count(10), 1, f'{status} should NOT exclude the agent')

    def test_ot_still_counts_despite_excluded_adherence_status(self):
        self._set_status('V')
        OvertimeShift.objects.create(agent=self.agent, date=self.monday,
                                     start_time=time(20, 0), end_time=time(22, 0))
        self.assertEqual(self._count(10), 0)   # regular template hours excluded
        self.assertEqual(self._count(20), 1)   # OT hours still count

    def test_cancelled_ot_still_excluded_regardless_of_adherence(self):
        OvertimeShift.objects.create(agent=self.agent, date=self.monday,
                                     start_time=time(20, 0), end_time=time(22, 0),
                                     status='cancelled')
        self.assertEqual(self._count(20), 0)

    def test_no_adherence_record_leaves_agent_counted(self):
        # No AdherenceRecord at all for this date — must default to counted,
        # the same safe direction the rest of the codebase already fails to.
        self.assertEqual(self._count(10), 1)


class ScheduledMapStatusTagTests(TestCase):
    """Popover status-tag/summary data must never influence the scheduled
    count itself — the count is the number people rely on for planning."""

    def setUp(self):
        self.agent = _staff('caller3', role_type='regular_agent')
        self.agent.role = 'agent'
        self.agent.save()
        today = date.today()
        self.week_start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        self.monday = self.week_start
        ShiftTemplate.objects.create(
            agent=self.agent, day_of_week=0,  # Monday
            start_time=time(9, 0), end_time=time(17, 0), is_off=False,
        )

    def test_entry_carries_status_when_record_exists(self):
        AdherenceRecord.objects.create(agent=self.agent, date=self.monday, status='T')
        scheduled, agents_map, _ = _build_scheduled_map(self.week_start)
        entry = agents_map[('Monday', 10)][0]
        self.assertEqual(entry['status'], 'T')
        self.assertEqual(scheduled[('Monday', 10)], 1)  # count unaffected

    def test_entry_has_no_status_when_no_record(self):
        scheduled, agents_map, _ = _build_scheduled_map(self.week_start)
        entry = agents_map[('Monday', 10)][0]
        self.assertIsNone(entry['status'])
        self.assertEqual(scheduled[('Monday', 10)], 1)  # count unaffected

    def test_status_summary_counts_correctly_and_omits_present_and_blank(self):
        from erlang.views import _summarize_statuses
        entries = [
            {'name': 'A', 'status': 'Absent'},
            {'name': 'B', 'status': 'Absent'},
            {'name': 'C', 'status': 'T'},
            {'name': 'D', 'status': 'P'},
            {'name': 'E', 'status': None},
        ]
        self.assertEqual(_summarize_statuses(entries), [('Absent', 2), ('T', 1)])


from decimal import Decimal

from scheduling.models import EmploymentPeriod, ScheduledRoleChange


class ScheduledMapQuitBajaExclusionTests(TestCase):
    """An agent marked Quit/Baja ahead of their formal separation stops counting
    as Scheduled Staff from that date FORWARD — including later weeks with no
    adherence data — without ever erasing a rehired agent who is really working.
    """

    def setUp(self):
        self.agent = _staff('caller5', role_type='regular_agent')
        self.agent.role = 'agent'
        self.agent.save()
        today = date.today()
        this_monday = today - timedelta(days=today.weekday())
        self.past_week = this_monday - timedelta(days=14)   # entirely in the past
        self.next_week = this_monday + timedelta(days=7)
        for dow in range(7):    # 09:00–17:00 every day, so any day can be checked
            ShiftTemplate.objects.create(
                agent=self.agent, day_of_week=dow,
                start_time=time(9, 0), end_time=time(17, 0), is_off=False,
            )
        self.period = EmploymentPeriod.objects.create(
            agent=self.agent, start_date=self.past_week - timedelta(days=365),
        )

    def _count(self, week_start, day_name, hour=10):
        scheduled, _, _ = _build_scheduled_map(week_start)
        return scheduled.get((day_name, hour), 0)

    def _mark(self, day, status='Quit'):
        AdherenceRecord.objects.update_or_create(
            agent=self.agent, date=day, defaults={'status': status},
        )

    def test_days_before_the_mark_count_and_the_mark_day_onward_does_not(self):
        self._mark(self.past_week + timedelta(days=2))   # Wednesday
        self.assertEqual(self._count(self.past_week, 'Monday'), 1)
        self.assertEqual(self._count(self.past_week, 'Tuesday'), 1)
        for day in ('Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'):
            self.assertEqual(self._count(self.past_week, day), 0, day)

    def test_mark_still_excludes_in_a_later_week(self):
        # The reappearance bug: a same-day-only rule would count them again here.
        self._mark(self.past_week + timedelta(days=2))
        self.assertEqual(self._count(self.next_week, 'Monday'), 0)

    def test_baja_behaves_exactly_like_quit(self):
        self._mark(self.past_week + timedelta(days=2), status='Baja')
        self.assertEqual(self._count(self.next_week, 'Monday'), 0)

    def test_sunday_mark_excludes_that_same_sunday(self):
        # Boundary: the mark lands on the LAST day of the viewed week, so the
        # look-back's upper bound must be inclusive of week_end.
        self._mark(self.next_week + timedelta(days=6))   # Sunday
        self.assertEqual(self._count(self.next_week, 'Saturday'), 1)
        self.assertEqual(self._count(self.next_week, 'Sunday'), 0)

    def test_pre_approved_future_status_does_not_rescue_the_mark(self):
        # A future V/LOA/Holiday is not evidence the agent is still here —
        # vacation approval writes a status onto every day of the range.
        self._mark(self.past_week + timedelta(days=2))
        self._mark(self.next_week + timedelta(days=4), status='V')
        self.assertEqual(self._count(self.next_week, 'Monday'), 0)

    def test_later_real_activity_restores_the_agent(self):
        self._mark(self.past_week + timedelta(days=2))
        self._mark(self.past_week + timedelta(days=3), status='P')
        self.assertEqual(self._count(self.next_week, 'Monday'), 1)

    def test_later_hours_with_no_status_restores_the_agent(self):
        self._mark(self.past_week + timedelta(days=2))
        AdherenceRecord.objects.create(
            agent=self.agent, date=self.past_week + timedelta(days=3),
            status='', actual_hours=8,
        )
        self.assertEqual(self._count(self.next_week, 'Monday'), 1)

    def test_removing_the_mark_restores_the_agent(self):
        marked_day = self.past_week + timedelta(days=2)
        self._mark(marked_day)
        self.assertEqual(self._count(self.next_week, 'Monday'), 0)
        AdherenceRecord.objects.filter(agent=self.agent, date=marked_day).delete()
        self.assertEqual(self._count(self.next_week, 'Monday'), 1)

    def test_no_open_employment_period_never_excludes(self):
        # Separated then rehired by just flipping the status back to active:
        # the old period is already closed and no new one was opened. Ambiguous,
        # so it must fail toward counting.
        self.period.end_date = self.past_week
        self.period.save()
        self._mark(self.past_week + timedelta(days=2))
        self.assertEqual(self._count(self.next_week, 'Monday'), 1)

    def test_mark_from_a_previous_employment_is_ignored(self):
        marked_day = self.past_week + timedelta(days=2)
        self._mark(marked_day)
        self.period.end_date = marked_day
        self.period.save()
        EmploymentPeriod.objects.create(
            agent=self.agent, start_date=marked_day + timedelta(days=1),
        )
        self._mark(marked_day + timedelta(days=3), status='P')
        self.assertEqual(self._count(self.next_week, 'Monday'), 1)

    def test_rehire_with_no_records_since_return_is_counted_in_a_future_week(self):
        # The failure that must not happen: nothing recorded since they came
        # back, and the week being viewed hasn't happened yet.
        marked_day = self.past_week + timedelta(days=2)
        self._mark(marked_day)
        self.period.end_date = marked_day
        self.period.save()
        EmploymentPeriod.objects.create(
            agent=self.agent, start_date=marked_day + timedelta(days=1),
        )
        self.assertEqual(self._count(self.next_week, 'Monday'), 1)

    def test_no_adherence_history_at_all_is_counted(self):
        self.assertEqual(self._count(self.next_week, 'Monday'), 1)

    def test_overtime_still_counts_for_a_quit_marked_agent(self):
        self._mark(self.past_week + timedelta(days=2))
        OvertimeShift.objects.create(agent=self.agent, date=self.next_week,
                                     start_time=time(20, 0), end_time=time(22, 0))
        self.assertEqual(self._count(self.next_week, 'Monday'), 0)    # scheduled hours
        self.assertEqual(self._count(self.next_week, 'Monday', 20), 1)  # OT still counts

    def test_excluded_agent_is_listed_once_in_the_popover(self):
        # A template plus a pending role-change schedule reach the same agent/day
        # twice; the count already deduped, the excluded list must too.
        self._mark(self.past_week + timedelta(days=2))
        ScheduledRoleChange.objects.create(
            agent=self.agent, new_role_type='regular_agent',
            effective_date=self.next_week, new_shift_days=[0],
            new_shift_start_time=time(9, 0), new_shift_end_time=time(17, 0),
        )
        _, _, excluded_map = _build_scheduled_map(self.next_week)
        entries = excluded_map[('Monday', 10)]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['reason'], 'Quit')

    # --- Multi-day mark runs -------------------------------------------------
    # Every test above marks a single day, where "earliest mark" and "latest
    # mark" are the same date. That is why the whole-week case shipped broken.

    DAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday',
                 'Friday', 'Saturday', 'Sunday')

    def _mark_range(self, first_day, days, status='Baja'):
        for i in range(days):
            self._mark(first_day + timedelta(days=i), status=status)

    def test_a_whole_week_marked_excludes_every_day_of_that_week(self):
        # The reported bug: marked Baja Mon–Sun, still counted Mon–Sat because
        # the cutoff was taken from the LAST marked day.
        self._mark_range(self.past_week, 7)
        for day in self.DAY_NAMES:
            self.assertEqual(self._count(self.past_week, day), 0, day)

    def test_a_whole_week_marked_still_excludes_the_following_week(self):
        # Forward stickiness must survive the fix, not just the marked days.
        self._mark_range(self.past_week, 7)
        self.assertEqual(self._count(self.past_week + timedelta(days=7), 'Monday'), 0)
        self.assertEqual(self._count(self.next_week, 'Monday'), 0)

    def test_a_mark_spanning_several_weeks_excludes_all_of_them(self):
        self._mark_range(self.past_week, 14)
        for offset in (0, 7):
            week = self.past_week + timedelta(days=offset)
            for day in self.DAY_NAMES:
                self.assertEqual(self._count(week, day), 0, f'{week} {day}')
        self.assertEqual(self._count(self.next_week, 'Monday'), 0)

    def test_no_counted_entry_ever_carries_a_separation_status_tag(self):
        # The popover symptom stated as an invariant: if a day is coded Quit or
        # Baja, the agent cannot be in the COUNTED list wearing that tag.
        self._mark_range(self.past_week, 7)
        _, agents_map, _ = _build_scheduled_map(self.past_week)
        tagged = [e for entries in agents_map.values() for e in entries
                  if e.get('status') in ('Quit', 'Baja')]
        self.assertEqual(tagged, [])

    def test_a_mixed_quit_and_baja_run_labels_each_day_with_its_own_status(self):
        self._mark_range(self.past_week, 2, status='Baja')            # Mon, Tue
        self._mark_range(self.past_week + timedelta(days=2), 5, status='Quit')
        _, _, excluded_map = _build_scheduled_map(self.past_week)
        reasons = [excluded_map[(day, 10)][0]['reason'] for day in self.DAY_NAMES]
        self.assertEqual(reasons, ['Baja', 'Baja', 'Quit', 'Quit',
                                   'Quit', 'Quit', 'Quit'])

    # --- Five9 zero-fill is absence, not activity ----------------------------
    # _zero_missing_scheduled writes actual_hours=0 with no status for every
    # active agent scheduled but missing from the daily file. Those rows mean
    # "did not show up", so they must not out-date a mark and restore the agent.

    def _blank_row(self, day, hours):
        AdherenceRecord.objects.update_or_create(
            agent=self.agent, date=day,
            defaults={'status': '', 'actual_hours': hours},
        )

    def test_a_zero_fill_row_does_not_restore_a_marked_agent(self):
        self._mark(self.past_week + timedelta(days=2))
        self._blank_row(self.past_week + timedelta(days=3), Decimal('0'))
        self.assertEqual(self._count(self.next_week, 'Monday'), 0)

    def test_small_real_hours_still_restore_the_agent(self):
        # Guards the > 0 boundary next to the existing actual_hours=8 case.
        self._mark(self.past_week + timedelta(days=2))
        self._blank_row(self.past_week + timedelta(days=3), Decimal('0.25'))
        self.assertEqual(self._count(self.next_week, 'Monday'), 1)

    def test_a_typed_status_with_zero_hours_still_restores_the_agent(self):
        # A human typed a status for that day, so the agent is still tracked.
        # This is what stops anyone collapsing the rule to actual_hours > 0.
        self._mark(self.past_week + timedelta(days=2))
        AdherenceRecord.objects.update_or_create(
            agent=self.agent, date=self.past_week + timedelta(days=3),
            defaults={'status': 'Absent', 'actual_hours': Decimal('0')},
        )
        self.assertEqual(self._count(self.next_week, 'Monday'), 1)

    def test_the_real_zero_fill_writer_does_not_restore_a_marked_agent(self):
        # Couples the writer to the reader: run the ACTUAL upload routine rather
        # than hand-rolling a row, so the two cannot drift apart again.
        from adherence.views import _zero_missing_scheduled
        self._mark(self.past_week + timedelta(days=2))
        _zero_missing_scheduled(self.past_week + timedelta(days=3), set())
        self.assertTrue(
            AdherenceRecord.objects.filter(
                agent=self.agent, date=self.past_week + timedelta(days=3),
                status='', actual_hours=Decimal('0'),
            ).exists(),
            'the zero-fill routine did not write the row this test depends on',
        )
        self.assertEqual(self._count(self.next_week, 'Monday'), 0)


# ── Skills Phase 3 groundwork: agent_id on every agents_map/excluded_map entry ──
# _build_scheduled_map's return tuple is unchanged (still 3 values); only the
# entry dicts gain a key, which is why every existing 3-value unpack above still
# works untouched.

class ScheduledMapAgentIdTests(TestCase):
    """The skill column narrows agents_map/excluded_map by agent — it needs to
    know WHICH agent each entry is, without a second query to find out."""

    def setUp(self):
        self.agent = _staff('sk_id1', role_type='regular_agent')
        self.agent.role = 'agent'
        self.agent.save()
        today = date.today()
        self.week_start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        self.monday = self.week_start
        ShiftTemplate.objects.create(
            agent=self.agent, day_of_week=0,
            start_time=time(9, 0), end_time=time(17, 0), is_off=False,
        )

    def test_scheduled_entry_carries_agent_id(self):
        _, agents_map, _ = _build_scheduled_map(self.week_start)
        entry = agents_map[('Monday', 10)][0]
        self.assertEqual(entry['agent_id'], self.agent.pk)

    def test_ot_entry_carries_agent_id(self):
        OvertimeShift.objects.create(agent=self.agent, date=self.monday,
                                     start_time=time(20, 0), end_time=time(22, 0))
        _, agents_map, _ = _build_scheduled_map(self.week_start)
        entry = agents_map[('Monday', 20)][0]
        self.assertEqual(entry['agent_id'], self.agent.pk)

    def test_excluded_entry_carries_agent_id(self):
        AdherenceRecord.objects.update_or_create(
            agent=self.agent, date=self.monday, defaults={'status': 'V'},
        )
        _, _, excluded_map = _build_scheduled_map(self.week_start)
        entry = excluded_map[('Monday', 10)][0]
        self.assertEqual(entry['agent_id'], self.agent.pk)


from scheduling.models import Skill


class StaffingSkillColumnTests(TestCase):
    """Skills Phase 3 — the skill coverage count on the Staffing tab.

    _build_skill_maps narrows _build_scheduled_map's OWN agents_map/excluded_map
    by agent_id — it re-expresses none of _build_scheduled_map's exclusion rules,
    so the count is a strict subset of Scheduled Staff by construction.
    """

    def setUp(self):
        self.bilingual = Skill.objects.create(name='Bilingual')
        self.intake = Skill.objects.create(name='Intake')

        today = date.today()
        self.week_start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        self.monday = self.week_start

        # both      — holds Bilingual AND Intake
        # only_one  — holds Bilingual only (must be excluded by an AND filter)
        # neither   — holds no skills
        self.both = self._scheduled('sk_both', [self.bilingual, self.intake])
        self.only_one = self._scheduled('sk_only_one', [self.bilingual])
        self.neither = self._scheduled('sk_neither', [])

    def _scheduled(self, username, skills):
        agent = _staff(username, role_type='regular_agent')
        agent.role = 'agent'
        agent.save()
        ShiftTemplate.objects.create(
            agent=agent, day_of_week=0,  # Monday
            start_time=time(9, 0), end_time=time(17, 0), is_off=False,
        )
        if skills:
            agent.skills.set(skills)
        return agent

    def _skill_count(self, hour, skill_ids):
        from scheduling.views import _agents_with_all_skills
        from erlang.views import _build_skill_maps
        _, agents_map, excluded_map = _build_scheduled_map(self.week_start)
        qualifying = _agents_with_all_skills(skill_ids)
        skill_scheduled, _, _ = _build_skill_maps(agents_map, excluded_map, qualifying)
        return skill_scheduled.get(('Monday', hour), 0)

    # ── AND semantics, the way an Excel filter narrows ──
    def test_and_semantics_requires_every_selected_skill(self):
        self.assertEqual(
            self._skill_count(10, [self.bilingual.pk, self.intake.pk]), 1
        )  # only sk_both holds both

    def test_one_skill_selected_counts_every_holder(self):
        self.assertEqual(self._skill_count(10, [self.bilingual.pk]), 2)  # both + only_one

    def test_zero_coverage_when_nobody_qualifies(self):
        lonely = Skill.objects.create(name='Lonely')
        self.assertEqual(self._skill_count(10, [lonely.pk]), 0)

    # ── Strict subset of Scheduled Staff, in both directions ──
    def test_count_never_exceeds_scheduled_staff(self):
        scheduled, agents_map, excluded_map = _build_scheduled_map(self.week_start)
        from scheduling.views import _agents_with_all_skills
        from erlang.views import _build_skill_maps
        qualifying = _agents_with_all_skills([self.bilingual.pk])
        skill_scheduled, _, _ = _build_skill_maps(agents_map, excluded_map, qualifying)
        self.assertLessEqual(
            skill_scheduled.get(('Monday', 10), 0), scheduled.get(('Monday', 10), 0)
        )

    def test_scheduled_staff_count_is_unaffected_by_the_skill_filter(self):
        # The Staffing roster itself must not move because a skill is selected.
        scheduled_before, _, _ = _build_scheduled_map(self.week_start)
        self._skill_count(10, [self.bilingual.pk, self.intake.pk])  # exercised, discarded
        scheduled_after, _, _ = _build_scheduled_map(self.week_start)
        self.assertEqual(scheduled_before, scheduled_after)

    # ── Reuses _build_scheduled_map's own exclusion rules, does not re-express them ──
    def test_excluded_adherence_status_removes_agent_from_skill_count_too(self):
        AdherenceRecord.objects.update_or_create(
            agent=self.both, date=self.monday, defaults={'status': 'V'},
        )
        self.assertEqual(self._skill_count(10, [self.bilingual.pk, self.intake.pk]), 0)

    def test_quit_marked_agent_stays_excluded_from_the_skill_count(self):
        from scheduling.models import EmploymentPeriod
        EmploymentPeriod.objects.create(
            agent=self.both, start_date=self.week_start - timedelta(days=365),
        )
        AdherenceRecord.objects.update_or_create(
            agent=self.both, date=self.monday + timedelta(days=1),
            defaults={'status': 'Quit'},
        )
        next_week = self.week_start + timedelta(days=7)
        skill_count = self._skill_count_for_week(next_week, 'Monday', 10,
                                                  [self.bilingual.pk, self.intake.pk])
        self.assertEqual(skill_count, 0)

    def _skill_count_for_week(self, week_start, day_name, hour, skill_ids):
        from scheduling.views import _agents_with_all_skills
        from erlang.views import _build_skill_maps
        _, agents_map, excluded_map = _build_scheduled_map(week_start)
        qualifying = _agents_with_all_skills(skill_ids)
        skill_scheduled, _, _ = _build_skill_maps(agents_map, excluded_map, qualifying)
        return skill_scheduled.get((day_name, hour), 0)

    # ── The deliberate OT divergence: Scheduled Staff counts any OT agent;
    #    the skill column counts an OT agent only if they hold the skill. ──
    def test_ot_agent_without_the_skill_counts_in_scheduled_staff_but_not_skill(self):
        ot_agent = _staff('sk_ot_noskill', role_type='regular_agent')
        ot_agent.role = 'agent'
        ot_agent.save()
        OvertimeShift.objects.create(agent=ot_agent, date=self.monday,
                                     start_time=time(20, 0), end_time=time(22, 0))
        scheduled, agents_map, excluded_map = _build_scheduled_map(self.week_start)
        self.assertEqual(scheduled.get(('Monday', 20), 0), 1)
        self.assertEqual(self._skill_count(20, [self.bilingual.pk]), 0)

    def test_ot_agent_with_the_skill_counts_in_both(self):
        ot_agent = self._scheduled('sk_ot_skilled', [self.bilingual])
        OvertimeShift.objects.create(agent=ot_agent, date=self.monday,
                                     start_time=time(20, 0), end_time=time(22, 0))
        scheduled, agents_map, excluded_map = _build_scheduled_map(self.week_start)
        self.assertEqual(scheduled.get(('Monday', 20), 0), 1)
        self.assertEqual(self._skill_count(20, [self.bilingual.pk]), 1)


from django.db import connection
from django.test.utils import CaptureQueriesContext


class StaffingSkillFilterViewTests(TestCase):
    """The Staffing tab's own Filters panel, at the request layer. Shape mirrors
    AdherenceSkillFilterTests (adherence/tests.py) — Staffing has no supervisor
    filter, and uses its own session key ('erlang_skill_filter') so a skill chosen
    here never narrows Adherence, and vice versa (scheduling.views._resolve_skill_filter).
    """

    def setUp(self):
        self.viewer = _staff('sk_viewer')
        self.client.login(username='sk_viewer', password='pw')

        self.bilingual = Skill.objects.create(name='Bilingual')
        self.intake = Skill.objects.create(name='Intake')
        self.retired = Skill.objects.create(name='Retired Skill', is_active=False)

        today = date.today()
        self.week_start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        self.monday = self.week_start
        ErlangCallRow.objects.create(week_start=self.week_start, day='Monday', hour=10,
                                     total_calls=100, avg_calls=100)

        self.both = self._scheduled('sk_both', [self.bilingual, self.intake])
        self.only_one = self._scheduled('sk_only_one', [self.bilingual])
        self.neither = self._scheduled('sk_neither', [])

    def _scheduled(self, username, skills):
        agent = _staff(username, role_type='regular_agent')
        agent.role = 'agent'
        agent.save()
        ShiftTemplate.objects.create(
            agent=agent, day_of_week=0,
            start_time=time(9, 0), end_time=time(17, 0), is_off=False,
        )
        if skills:
            agent.skills.set(skills)
        return agent

    def _monday_row(self, query=''):
        url = reverse('erlang_calculator') + f'?week_start={self.week_start.isoformat()}{query}'
        resp = self.client.get(url)
        monday = next(d for d in resp.context['days'] if d['name'] == 'Monday')
        return resp, monday['rows'][0]

    def test_no_skill_selected_has_no_skill_count(self):
        _, row = self._monday_row()
        self.assertNotIn('skill_count', row)

    def test_and_semantics_via_the_view(self):
        _, row = self._monday_row(f'&skills={self.bilingual.pk}&skills={self.intake.pk}')
        self.assertEqual(row['skill_count'], 1)

    def test_one_skill_counts_every_holder_via_the_view(self):
        _, row = self._monday_row(f'&skills={self.bilingual.pk}')
        self.assertEqual(row['skill_count'], 2)

    def test_retired_skill_in_session_stops_filtering(self):
        self.both.skills.add(self.retired)
        self._monday_row(f'&skills={self.retired.pk}')   # sets the session
        _, row = self._monday_row()                       # falls back to session
        self.assertNotIn('skill_count', row)

    def test_garbage_skill_value_is_ignored_not_fatal(self):
        resp, row = self._monday_row('&skills=notanumber')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('skill_count', row)

    def test_unknown_skill_pk_is_ignored(self):
        resp, row = self._monday_row('&skills=99999')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('skill_count', row)

    def test_filter_survives_week_navigation_with_no_param(self):
        self._monday_row(f'&skills={self.bilingual.pk}')   # sets the session
        next_week = self.week_start + timedelta(days=7)
        ErlangCallRow.objects.create(week_start=next_week, day='Monday', hour=10,
                                     total_calls=100, avg_calls=100)
        url = reverse('erlang_calculator') + f'?week_start={next_week.isoformat()}'
        resp = self.client.get(url)
        monday = next(d for d in resp.context['days'] if d['name'] == 'Monday')
        self.assertIn('skill_count', monday['rows'][0])

    def test_explicit_empty_param_clears_the_session_filter(self):
        self._monday_row(f'&skills={self.bilingual.pk}')
        _, row = self._monday_row('&skills=')
        self.assertNotIn('skill_count', row)

    def test_session_key_is_isolated_from_adherence(self):
        self._monday_row(f'&skills={self.bilingual.pk}')
        self.assertNotIn('adh_skill_filter', self.client.session)
        self.assertIn('erlang_skill_filter', self.client.session)


class StaffingSkillFilterQueryCountTests(TestCase):
    """Measured, not estimated: the skill filter's query cost must be flat in
    the number of skills selected and the number of agents on the roster, and
    zero when the filter isn't in use. Mirrors
    AdherenceSkillFilterTests' query-count tests (adherence/tests.py)."""

    def setUp(self):
        self.viewer = _staff('sk_qviewer')
        self.client.login(username='sk_qviewer', password='pw')
        self.bilingual = Skill.objects.create(name='Bilingual')
        self.intake = Skill.objects.create(name='Intake')
        today = date.today()
        self.week_start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        ErlangCallRow.objects.create(week_start=self.week_start, day='Monday', hour=10,
                                     total_calls=100, avg_calls=100)
        self.both = self._scheduled('sq_both', [self.bilingual, self.intake])

    def _scheduled(self, username, skills):
        agent = _staff(username, role_type='regular_agent')
        agent.role = 'agent'
        agent.save()
        ShiftTemplate.objects.create(
            agent=agent, day_of_week=0,
            start_time=time(9, 0), end_time=time(17, 0), is_off=False,
        )
        if skills:
            agent.skills.set(skills)
        return agent

    def _url(self, query=''):
        return reverse('erlang_calculator') + f'?week_start={self.week_start.isoformat()}{query}'

    def _query_count(self, query=''):
        self.client.get(self._url(query))     # warm up: session row, BillingSettings, etc.
        ctx = CaptureQueriesContext(connection)
        with ctx:
            self.client.get(self._url(query))
        return len(ctx)

    def test_filtering_adds_a_flat_two_queries_over_the_unfiltered_page(self):
        """The Filters panel's checkbox list is rendered every page load, filter
        active or not — that fixed cost (one query, materializing active_skills
        for the picker) is paid either way, exactly like Adherence's own full
        page view. Turning a filter ON must add exactly two more on top of that:
        one validating the requested ids, one resolving the AND into a pk set.
        That +2 delta — not an absolute count — is the invariant that must stay
        flat regardless of how many skills or agents are involved."""
        unfiltered = self._query_count()
        filtered = self._query_count(f'&skills={self.bilingual.pk}&skills={self.intake.pk}')
        self.assertEqual(filtered, unfiltered + 2)

    def test_query_count_does_not_grow_with_the_number_of_skills(self):
        third = Skill.objects.create(name='Third')
        self.both.skills.add(third)
        two = self._query_count(f'&skills={self.bilingual.pk}&skills={self.intake.pk}')
        three = self._query_count(
            f'&skills={self.bilingual.pk}&skills={self.intake.pk}&skills={third.pk}'
        )
        self.assertEqual(two, three)

    def test_query_count_does_not_grow_with_the_number_of_agents(self):
        small = self._query_count(f'&skills={self.bilingual.pk}&skills={self.intake.pk}')
        for i in range(10):
            self._scheduled(f'sq_bulk_{i}', [])
        big = self._query_count(f'&skills={self.bilingual.pk}&skills={self.intake.pk}')
        self.assertEqual(small, big)


class StaffingSkillColumnRenderTests(TestCase):
    """What actually reaches the browser: the column, its header, the red-zero
    highlight, and — the load-bearing check — that the new column never
    touches the .staffing-display class the day badge, summary bar and
    Variance all select on."""

    def setUp(self):
        self.viewer = _staff('sk_render_viewer')
        self.client.login(username='sk_render_viewer', password='pw')

        self.bilingual = Skill.objects.create(name='Bilingual')
        self.intake = Skill.objects.create(name='Intake')

        today = date.today()
        self.week_start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        ErlangCallRow.objects.create(week_start=self.week_start, day='Monday', hour=10,
                                     total_calls=500, avg_calls=500)
        ErlangCallRow.objects.create(week_start=self.week_start, day='Monday', hour=11,
                                     total_calls=500, avg_calls=500)

        # Covers hour 10 only (09:00-11:00) — hour 11 has zero skill coverage
        # even though the roster still has scheduled staff there via the
        # uncovered agent below.
        self.covered = self._scheduled('sk_r_covered', [self.bilingual],
                                        start=time(9, 0), end=time(11, 0))
        self.uncovered = self._scheduled('sk_r_uncovered', [],
                                          start=time(9, 0), end=time(12, 0))

    def _scheduled(self, username, skills, start, end):
        agent = _staff(username, role_type='regular_agent')
        agent.role = 'agent'
        agent.save()
        ShiftTemplate.objects.create(
            agent=agent, day_of_week=0, start_time=start, end_time=end, is_off=False,
        )
        if skills:
            agent.skills.set(skills)
        return agent

    def _get(self, query=''):
        url = reverse('erlang_calculator') + f'?week_start={self.week_start.isoformat()}{query}'
        return self.client.get(url)

    def test_column_absent_with_no_skill_selected(self):
        # #skill-popover and its JS always exist in the DOM (inert, same as
        # #filters-popover) — nothing to trigger them when the column carrying
        # their onclick doesn't exist. What must actually be absent is any
        # rendered CELL for the column: no per-hour id, no hover CSS for it.
        resp = self._get()
        self.assertNotContains(resp, 'skillcov-')
        self.assertNotContains(resp, '.skill-coverage-display:hover')

    def test_column_present_when_a_skill_is_selected(self):
        resp = self._get(f'&skills={self.bilingual.pk}')
        self.assertContains(resp, 'skill-coverage-display')
        self.assertContains(resp, 'id="skill-popover"')
        self.assertContains(resp, 'skillcov-Monday-10')

    def test_header_shows_the_single_selected_skill_name(self):
        resp = self._get(f'&skills={self.bilingual.pk}')
        self.assertContains(resp, 'Bilingual')

    def test_header_shortens_for_multiple_selected_skills(self):
        resp = self._get(f'&skills={self.bilingual.pk}&skills={self.intake.pk}')
        self.assertContains(resp, '+1')

    def test_zero_coverage_cell_is_colored_red(self):
        resp = self._get(f'&skills={self.bilingual.pk}')
        html = resp.content.decode()
        segment = html[html.index('id="skillcov-Monday-11"'):][:400]
        self.assertIn('#dc2626', segment)

    def test_covered_cell_is_not_colored_red(self):
        resp = self._get(f'&skills={self.bilingual.pk}')
        html = resp.content.decode()
        segment = html[html.index('id="skillcov-Monday-10"'):][:400]
        self.assertNotIn('#dc2626', segment)

    def test_staffing_display_count_is_unaffected_by_the_skill_filter(self):
        """The .staffing-display landmine: the day badge, summary bar and
        Variance all select on this class. Turning the skill filter on must
        not change how many elements carry it."""
        unfiltered_count = self._get().content.decode().count('staffing-display')
        filtered_count = self._get(f'&skills={self.bilingual.pk}').content.decode().count('staffing-display')
        self.assertEqual(unfiltered_count, filtered_count)
        self.assertGreater(unfiltered_count, 0)

    def test_no_element_carries_both_classes(self):
        html = self._get(f'&skills={self.bilingual.pk}').content.decode()
        self.assertNotIn('staffing-display skill-coverage-display', html)
        self.assertNotIn('skill-coverage-display staffing-display', html)

    def test_existing_staff_popover_data_is_unaffected_by_the_skill_filter(self):
        """SCHEDULED_AGENTS / EXCLUDED_AGENTS / STATUS_SUMMARY — the existing
        Scheduled Staff popover's own data — must be byte-identical whether or
        not the skill filter is active."""
        def extract(html, name):
            marker = f'const {name} = '
            start = html.index(marker) + len(marker)
            end = html.index(';\n', start)
            return html[start:end]

        unfiltered_html = self._get().content.decode()
        filtered_html = self._get(f'&skills={self.bilingual.pk}').content.decode()
        for name in ('SCHEDULED_AGENTS', 'EXCLUDED_AGENTS', 'STATUS_SUMMARY'):
            self.assertEqual(
                extract(unfiltered_html, name), extract(filtered_html, name),
                f'{name} changed when the skill filter was applied',
            )

    def test_skill_popover_data_lists_only_skill_holders(self):
        resp = self._get(f'&skills={self.bilingual.pk}')
        html = resp.content.decode()
        marker = 'const SKILL_AGENTS = '
        start = html.index(marker) + len(marker)
        end = html.index(';\n', start)
        payload = html[start:end]
        self.assertIn(self.covered.agent_name, payload)
        self.assertNotIn(self.uncovered.agent_name, payload)
