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
