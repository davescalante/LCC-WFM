import io
import json
from decimal import Decimal
from datetime import date, timedelta, time
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User
from django.utils import timezone

from django.db import connection
from django.db.models import Q
from django.test.utils import CaptureQueriesContext
from scheduling.models import (
    Agent, AgentSeparation, Five9Profile, Five9PrimaryPeriod, Shift, ShiftTemplate,
    OvertimeShift, Skill, AuditLog,
)
from adherence.models import AdherenceRecord, DailyUpload, DailyAgentHours, Coding, AdherenceNote
from adherence.views import (
    _get_adherence_agent_pks, _net_ot_evening_hours,
    _adherence_filter_pills, _adherence_filter_url,
)
from finance.models import BillingSettings, BillingSettingsHistory
from types import SimpleNamespace
from django.test import SimpleTestCase


def _ot(sh, sm, eh, em):
    return SimpleNamespace(start_time=time(sh, sm), end_time=time(eh, em), is_off=False)


class NetOtEveningHoursTests(SimpleTestCase):
    """A day's OT hours count each clock-minute once: overlapping OT slots are unioned (not
    summed), and OT time already inside the regular shift doesn't double-count. These are the
    Scheduled-Hours cases reported on the Adherence tab."""

    def test_disjoint_ot_sums(self):
        ots = [_ot(9, 0, 11, 0), _ot(13, 0, 15, 0)]          # 2h + 2h, no overlap
        self.assertEqual(_net_ot_evening_hours(ots, None, None, False), Decimal('4'))

    def test_overlapping_ot_is_unioned(self):
        # 09:00–11:00 (2h) + 10:00–13:00 (3h) → union 09:00–13:00 = 4h, not 5h
        ots = [_ot(9, 0, 11, 0), _ot(10, 0, 13, 0)]
        self.assertEqual(_net_ot_evening_hours(ots, None, None, False), Decimal('4'))

    def test_ot_clipped_to_before_regular_shift(self):
        # regular shift starts 13:00; OT 10:00–14:00 → only 10:00–13:00 = 3h is new
        shift = SimpleNamespace(start_time=time(13, 0), end_time=time(21, 0), is_off=False)
        self.assertEqual(_net_ot_evening_hours([_ot(10, 0, 14, 0)], shift, None, False), Decimal('3'))

    def test_ot_fully_inside_shift_adds_nothing(self):
        shift = SimpleNamespace(start_time=time(13, 0), end_time=time(21, 0), is_off=False)
        self.assertEqual(_net_ot_evening_hours([_ot(14, 0, 16, 0)], shift, None, False), Decimal('0'))

    def test_overnight_ot_counts_pre_midnight_only(self):
        # OT 22:00–02:00 → the calendar-day (pre-midnight) portion is 22:00–24:00 = 2h
        self.assertEqual(_net_ot_evening_hours([_ot(22, 0, 2, 0)], None, None, False), Decimal('2'))

    def test_thousands_of_duplicate_ot_union_to_one_slot(self):
        # A runaway import/save can pile thousands of identical OT rows onto one day. They
        # must union to a single interval (identical hours), not sum — and must not go
        # quadratic, which is what timed out the Adherence group load (the 502).
        ots = [_ot(18, 0, 23, 0) for _ in range(5000)]          # 5000 identical evening slots
        self.assertEqual(_net_ot_evening_hours(ots, None, None, False), Decimal('5'))

    def test_duplicate_overnight_prev_and_evening_ot_no_blowup(self):
        # The exact production shape: thousands of duplicate OVERNIGHT OT yesterday plus
        # thousands of duplicate EVENING OT today. Net evening OT = today's 18:00–23:00 (5h);
        # yesterday's post-midnight 00:00–04:00 spillover doesn't overlap the evening. Before
        # the interval merge this was 3000×3000 subtractions per agent-day.
        today_ot = [_ot(18, 0, 23, 0) for _ in range(3000)]
        prev_ot = [_ot(20, 0, 4, 0) for _ in range(3000)]       # overnight → morning 00:00–04:00
        self.assertEqual(
            _net_ot_evening_hours(today_ot, None, None, False, prev_ot_shifts=prev_ot),
            Decimal('5'),
        )


# Fixed Monday — keeps tests deterministic and avoids weekday-boundary issues
_WEEK_START = date(2025, 1, 6)
_WEEK = [_WEEK_START + timedelta(days=i) for i in range(7)]


def _make_agent(username='testuser'):
    user = User.objects.create_user(username, password='x')
    return Agent.objects.create(
        user=user, role='agent', role_type='agent',
        agent_name=username, status='active', track_attendance=True,
    )


def _settings(**overrides):
    obj, _ = BillingSettings.objects.get_or_create(pk=1)
    for k, v in overrides.items():
        setattr(obj, k, v)
    obj.save()
    return obj


class BuildRowsNullTimeTemplateTests(TestCase):
    """A non-off ShiftTemplate saved with no start/end times must not crash the adherence
    rows render (regression for the 'Failed to load data' 500 — TypeError: None < None)."""

    def setUp(self):
        self.agent = _make_agent('nulltime_test')

    def test_null_time_non_off_template_does_not_crash(self):
        from adherence.views import _build_rows
        tmpl = ShiftTemplate(agent=self.agent, day_of_week=_WEEK[0].weekday(),
                             start_time=None, end_time=None, is_off=False)
        rows = _build_rows(
            agents=[self.agent], week_dates=_WEEK,
            shift_map={(self.agent.pk, _WEEK[0]): tmpl},
            record_map={}, coded_map={},
        )
        self.assertEqual(rows[0]['cells'][0]['sched_hrs'], Decimal('0'))
        self.assertEqual(rows[0]['sched_hours'], Decimal('0'))

    def test_null_time_template_with_ot_does_not_crash(self):
        from adherence.views import _build_rows
        from scheduling.models import OvertimeShift
        tmpl = ShiftTemplate(agent=self.agent, day_of_week=_WEEK[0].weekday(),
                             start_time=None, end_time=None, is_off=False)
        ot = OvertimeShift(agent=self.agent, date=_WEEK[0], start_time=time(9, 0), end_time=time(11, 0))
        rows = _build_rows(
            agents=[self.agent], week_dates=_WEEK,
            shift_map={(self.agent.pk, _WEEK[0]): tmpl},
            record_map={}, coded_map={},
            ot_map={(self.agent.pk, _WEEK[0]): [ot]},
        )
        # null-time regular shift contributes 0; the 2h OT still counts
        self.assertEqual(rows[0]['cells'][0]['sched_hrs'], Decimal('2'))


class BuildMapsOtDedupeTests(TestCase):
    """Duplicate OvertimeShift rows (same agent/day/time/status) — from a runaway import or
    repeated save — collapse to one per slot when the adherence maps are built, so the grid
    neither bloats nor drives the per-cell OT math into an O(N^2) timeout (the group-load 502)."""

    def setUp(self):
        self.agent = _make_agent('ot_dedupe_test')

    def test_duplicate_ot_rows_collapse_to_one(self):
        from adherence.views import _build_maps
        from scheduling.models import OvertimeShift
        OvertimeShift.objects.bulk_create([
            OvertimeShift(agent=self.agent, date=_WEEK[0],
                          start_time=time(18, 0), end_time=time(23, 0), status='completed')
            for _ in range(200)
        ])
        ot_map = _build_maps([self.agent], _WEEK)[3]
        self.assertEqual(len(ot_map[(self.agent.pk, _WEEK[0])]), 1)

    def test_distinct_slots_and_distinct_status_are_kept(self):
        from adherence.views import _build_maps
        from scheduling.models import OvertimeShift
        # different times → kept; same time but different status → kept (no_show must survive
        # so the bonus-disqualify check still fires)
        OvertimeShift.objects.create(agent=self.agent, date=_WEEK[0],
                                     start_time=time(18, 0), end_time=time(20, 0), status='completed')
        OvertimeShift.objects.create(agent=self.agent, date=_WEEK[0],
                                     start_time=time(21, 0), end_time=time(23, 0), status='completed')
        OvertimeShift.objects.create(agent=self.agent, date=_WEEK[0],
                                     start_time=time(18, 0), end_time=time(20, 0), status='no_show')
        ot_map = _build_maps([self.agent], _WEEK)[3]
        self.assertEqual(len(ot_map[(self.agent.pk, _WEEK[0])]), 3)


class BuildRowsBonusTests(TestCase):
    """_build_rows correctly determines bonus eligibility from status codes."""

    def setUp(self):
        self.agent = _make_agent('bonus_test')
        _settings()

    def _build(self, record_map, coded_map=None):
        from adherence.views import _build_rows
        return _build_rows(
            agents=[self.agent],
            week_dates=_WEEK,
            shift_map={},
            record_map=record_map or {},
            coded_map=coded_map or {},
        )

    def test_present_qualifies_bonus(self):
        r = AdherenceRecord(agent=self.agent, date=_WEEK[0], status='P', actual_hours=Decimal('8'))
        rows = self._build({(self.agent.pk, _WEEK[0]): r})
        self.assertEqual(rows[0]['bonus'], 'Yes')

    def test_absent_disqualifies_bonus(self):
        r = AdherenceRecord(agent=self.agent, date=_WEEK[0], status='Absent', actual_hours=None)
        rows = self._build({(self.agent.pk, _WEEK[0]): r})
        self.assertEqual(rows[0]['bonus'], 'No')

    def test_tardy_disqualifies_bonus(self):
        r = AdherenceRecord(agent=self.agent, date=_WEEK[0], status='T', actual_hours=Decimal('7.75'))
        rows = self._build({(self.agent.pk, _WEEK[0]): r})
        self.assertEqual(rows[0]['bonus'], 'No')

    def test_vto_qualifies_bonus(self):
        r = AdherenceRecord(agent=self.agent, date=_WEEK[0], status='VTO', actual_hours=None)
        rows = self._build({(self.agent.pk, _WEEK[0]): r})
        self.assertEqual(rows[0]['bonus'], 'Yes')

    def test_no_records_gives_dash(self):
        rows = self._build({})
        self.assertEqual(rows[0]['bonus'], '—')

    def test_mixed_week_disqualifies_on_any_bad_status(self):
        # P on Mon, Absent on Tue → bonus disqualified
        rec_map = {
            (self.agent.pk, _WEEK[0]): AdherenceRecord(agent=self.agent, date=_WEEK[0], status='P', actual_hours=Decimal('8')),
            (self.agent.pk, _WEEK[1]): AdherenceRecord(agent=self.agent, date=_WEEK[1], status='Absent', actual_hours=None),
        }
        rows = self._build(rec_map)
        self.assertEqual(rows[0]['bonus'], 'No')


class BuildRowsNRCapTests(TestCase):
    """_build_rows applies the weekly NR cap and deducts excess from final_adjusted."""

    def setUp(self):
        self.agent = _make_agent('nr_test')
        self.settings = _settings(nr_cap_regular_hours=Decimal('6.00'))
        # Adherence display's weekly NR sum counts only the primary account —
        # mark this fixture's one account primary so these tests keep
        # exercising the NR-cap math they're named for.
        Five9Profile.objects.create(agent=self.agent, five9_username='nr_test', is_primary=True, billable=True)

    def _add_nr(self, nr_seconds, login_seconds=None):
        upload = DailyUpload.objects.create(date=_WEEK[0], row_count=1)
        DailyAgentHours.objects.create(
            upload=upload, agent=self.agent,
            five9_username='nr_test',
            login_seconds=login_seconds if login_seconds is not None else nr_seconds,
            not_ready_seconds=nr_seconds,
        )

    def _build(self, actual_hours=Decimal('40')):
        from adherence.views import _build_rows
        record = AdherenceRecord.objects.create(
            agent=self.agent, date=_WEEK[0], status='P', actual_hours=actual_hours,
        )
        return _build_rows(
            agents=[self.agent],
            week_dates=_WEEK,
            shift_map={},
            record_map={(self.agent.pk, _WEEK[0]): record},
            coded_map={},
            billing_settings=self.settings,
        )

    def test_excess_nr_deducted(self):
        # 8 h NR, cap = 6 h → 2 h deducted
        self._add_nr(8 * 3600)
        row = self._build()[0]
        self.assertAlmostEqual(float(row['nr_cap_adj']), 2.0, places=3)
        self.assertAlmostEqual(float(row['final_adjusted']), float(row['adjusted_total']) - 2.0, places=3)

    def test_nr_within_cap_no_deduction(self):
        # 4 h NR, cap = 6 h → no deduction
        self._add_nr(4 * 3600)
        row = self._build()[0]
        self.assertAlmostEqual(float(row['nr_cap_adj']), 0.0, places=3)
        self.assertEqual(row['final_adjusted'], row['adjusted_total'])

    def test_final_adjusted_never_negative(self):
        # Extreme NR (more than actual hours) → final_adjusted floors at 0
        self._add_nr(100 * 3600)
        row = self._build(actual_hours=Decimal('5'))[0]
        self.assertGreaterEqual(float(row['final_adjusted']), 0.0)

    def test_hours_totals_accumulated(self):
        # actual_hours on the record is accumulated into adjusted_total
        self._add_nr(0)
        row = self._build(actual_hours=Decimal('8'))[0]
        self.assertAlmostEqual(float(row['actual_hours']), 8.0, places=3)


class BuildRowsVZeroingTests(TestCase):
    """'V' (Vacation) zeroes a scheduled day's hours exactly like VTO/LOA."""

    def setUp(self):
        self.agent = _make_agent('v_zero_test')
        self.settings = _settings()
        self.shift = Shift(
            agent=self.agent, date=_WEEK[0],
            start_time=time(9, 0), end_time=time(17, 0), is_off=False,
        )

    def _build(self, status, actual_hours=None):
        from adherence.views import _build_rows
        record = AdherenceRecord(
            agent=self.agent, date=_WEEK[0], status=status, actual_hours=actual_hours,
        )
        rows = _build_rows(
            agents=[self.agent],
            week_dates=_WEEK,
            shift_map={(self.agent.pk, _WEEK[0]): self.shift},
            record_map={(self.agent.pk, _WEEK[0]): record},
            coded_map={},
            billing_settings=self.settings,
        )
        return rows[0]

    def test_v_zeroes_scheduled_hours(self):
        row = self._build('V')
        self.assertEqual(row['cells'][0]['sched_hrs'], Decimal('0'))
        self.assertEqual(row['sched_hours'], Decimal('0'))

    def test_normal_working_day_unaffected(self):
        row = self._build('P', actual_hours=Decimal('8'))
        self.assertEqual(row['cells'][0]['sched_hrs'], Decimal('8'))
        self.assertEqual(row['sched_hours'], Decimal('8'))

    def test_v_still_qualifies_bonus(self):
        row = self._build('V')
        self.assertEqual(row['bonus'], 'Yes')


class CostOfScheduleVTests(TestCase):
    """Cost of Schedule already excludes V from sched/loss, identically to VTO —
    this locks in that (already-correct) behavior as a regression guard."""

    def test_v_day_excluded_from_cos_regardless_of_sched_hrs(self):
        from adherence.views import _calculate_cos
        # sched_hrs deliberately non-zero to prove the whitelist — not the hours
        # value — is what excludes the day.
        cells = [{'status': 'V', 'sched_hrs': Decimal('8'), 'display_hrs': Decimal('0')}]
        cells += [{'status': '', 'sched_hrs': Decimal('0'), 'display_hrs': Decimal('0')} for _ in range(6)]
        rows = [{'cells': cells}]
        day_data, cos_week = _calculate_cos(rows, _WEEK)
        self.assertEqual(day_data[0]['sched_hours'], 0.0)
        self.assertIsNone(day_data[0]['cos_pct'])
        self.assertEqual(cos_week['sched_hours'], 0.0)


class CostOfScheduleImssSuspensionFullLossTests(TestCase):
    """IMSS and Suspension ('S') must keep losing 100% of scheduled hours in
    Cost of Schedule — a regression guard, since the new Staffing Calculator
    exclusion (STAFFING_EXCLUDED_STATUSES) is a separate constant and must
    never change this adherence-app behavior."""

    def _cos_pct_for_status(self, status):
        from adherence.views import _calculate_cos
        cells = [{'status': status, 'sched_hrs': Decimal('8'), 'display_hrs': Decimal('0')}]
        cells += [{'status': '', 'sched_hrs': Decimal('0'), 'display_hrs': Decimal('0')} for _ in range(6)]
        rows = [{'cells': cells}]
        day_data, _ = _calculate_cos(rows, _WEEK)
        return day_data[0]['cos_pct']

    def test_imss_is_full_loss(self):
        self.assertEqual(self._cos_pct_for_status('IMSS'), 100.0)

    def test_suspension_is_full_loss(self):
        self.assertEqual(self._cos_pct_for_status('S'), 100.0)


class CodingsRosterExcludesOfficialAdminsTests(TestCase):
    """
    Part 3: Official Admins are excluded from the regular Codings tab's
    roster (they only appear on Admin Codings) — mirrors the is_official_admin
    exclusion already used by _get_adherence_agent_pks / payroll_export.
    """

    def setUp(self):
        # Staff login (not a portal-restricted 'agent' role) to view the tab.
        staff_user = User.objects.create_user('codingsviewer', password='x')
        self.staff = Agent.objects.create(
            user=staff_user, role='admin', role_type='supervisor',
            agent_name='Codings Viewer', status='active',
        )
        self.client.login(username='codingsviewer', password='x')

        regular_user = User.objects.create_user('regularagent', password='x')
        self.regular = Agent.objects.create(
            user=regular_user, role='agent', role_type='agent',
            agent_name='Regular Agent', status='active', track_attendance=True,
        )
        admin_user = User.objects.create_user('officialadmin', password='x')
        self.official = Agent.objects.create(
            user=admin_user, role='admin', role_type='supervisor',
            agent_name='Official Admin', status='active', is_official_admin=True,
        )

        # A regular (non-admin) coding for each, so both would show real hours
        # if included — proves exclusion is about the roster, not zero-filling.
        Coding.objects.create(
            agent=self.regular, date=_WEEK_START,
            start_time=time(9, 0), end_time=time(11, 0), is_admin_coding=False,
        )
        Coding.objects.create(
            agent=self.official, date=_WEEK_START,
            start_time=time(9, 0), end_time=time(11, 0), is_admin_coding=False,
        )

    def _get_rows(self):
        resp = self.client.get(reverse('codings_week') + f'?week_start={_WEEK_START.isoformat()}')
        self.assertEqual(resp.status_code, 200)
        return resp.context['rows']

    def test_official_admin_absent_from_roster(self):
        pks = [row['agent'].pk for row in self._get_rows()]
        self.assertNotIn(self.official.pk, pks)

    def test_regular_agent_still_present(self):
        pks = [row['agent'].pk for row in self._get_rows()]
        self.assertIn(self.regular.pk, pks)

    def test_remaining_agent_totals_unchanged(self):
        rows = self._get_rows()
        regular_row = next(r for r in rows if r['agent'].pk == self.regular.pk)
        # 2h coding, in seconds — unaffected by the official admin's exclusion.
        self.assertEqual(regular_row['total_seconds'], 2 * 3600)


class CodingsRosterIncludesRecentlySeparatedAgentsTests(TestCase):
    """A finalized separation must not erase an agent from past weeks' Codings roster."""

    def setUp(self):
        staff_user = User.objects.create_user('codingssepviewer', password='x')
        Agent.objects.create(
            user=staff_user, role='admin', role_type='supervisor',
            agent_name='Codings Viewer', status='active',
        )
        self.client.login(username='codingssepviewer', password='x')

        sep_user = User.objects.create_user('separatedagent', password='x')
        self.separated = Agent.objects.create(
            user=sep_user, role='agent', role_type='regular_agent',
            agent_name='Separated Agent', status='inactive', track_attendance=True,
        )
        AgentSeparation.objects.create(
            agent=self.separated, status='finalized', separation_type='quit',
            last_day_worked=_WEEK_START - timedelta(days=1),
            remove_from_adherence_date=_WEEK_START + timedelta(days=7),
        )

    def _pks_for(self, week_start):
        resp = self.client.get(reverse('codings_week') + f'?week_start={week_start.isoformat()}')
        self.assertEqual(resp.status_code, 200)
        return [row['agent'].pk for row in resp.context['rows']]

    def test_present_in_week_before_removal_date(self):
        self.assertIn(self.separated.pk, self._pks_for(_WEEK_START))

    def test_absent_in_week_of_removal_date(self):
        self.assertNotIn(self.separated.pk, self._pks_for(_WEEK_START + timedelta(days=7)))


class AdherenceStartDateFloorTests(TestCase):
    """
    adherence_start_date is an opt-in per-agent floor on _get_adherence_agent_pks:
    NULL is a no-op (today's behavior, unchanged for every existing agent); once set,
    it excludes any week whose Monday is before the floor and includes the floor's own
    week and every later one. It is a floor only — it never adds an agent the activity
    gate wouldn't otherwise include, it only ever removes one.

    _get_adherence_agent_pks caches its result for 300s per (week_start, supervisor_id),
    so every call below that could observe a previous call's cached result clears the
    cache first — otherwise a test could pass or fail for the wrong reason.
    """

    def _agent_with_template(self, username):
        agent = _make_agent(username)
        ShiftTemplate.objects.create(
            agent=agent, day_of_week=0, start_time=time(9, 0), end_time=time(17, 0),
        )
        return agent

    def test_null_floor_behaves_exactly_as_today(self):
        agent = self._agent_with_template('floornull1')
        far_past_week = _WEEK_START - timedelta(weeks=104)
        far_past_dates = [far_past_week + timedelta(days=i) for i in range(7)]

        cache.clear()
        pks = _get_adherence_agent_pks(far_past_dates, far_past_week)
        self.assertIn(agent.pk, pks)

    def test_floor_excludes_a_week_before_it(self):
        agent = self._agent_with_template('floorexcl1')
        agent.adherence_start_date = _WEEK_START
        agent.save()
        earlier_week = _WEEK_START - timedelta(weeks=1)
        earlier_dates = [earlier_week + timedelta(days=i) for i in range(7)]

        cache.clear()
        pks = _get_adherence_agent_pks(earlier_dates, earlier_week)
        self.assertNotIn(agent.pk, pks)

    def test_floor_includes_its_own_week(self):
        agent = self._agent_with_template('floorown1')
        agent.adherence_start_date = _WEEK_START
        agent.save()

        cache.clear()
        pks = _get_adherence_agent_pks(_WEEK, _WEEK_START)
        self.assertIn(agent.pk, pks)

    def test_floor_includes_a_later_week(self):
        agent = self._agent_with_template('floorlater1')
        agent.adherence_start_date = _WEEK_START
        agent.save()
        later_week = _WEEK_START + timedelta(weeks=1)
        later_dates = [later_week + timedelta(days=i) for i in range(7)]

        cache.clear()
        pks = _get_adherence_agent_pks(later_dates, later_week)
        self.assertIn(agent.pk, pks)


class AdminEditScopeTests(TestCase):
    """
    Part 5: server-side team-scoped edit enforcement for Official Admin data.
    A can_access_admin_tabs holder may save a status/note only for themselves
    or an Official Admin they supervise; anyone else targeting an
    out-of-team Official Admin is rejected server-side (403), even via a
    direct POST bypassing the UI. Super admins are unrestricted. Regular
    (non-Official-Admin) targets are unaffected — a full regression guard.
    """

    def setUp(self):
        self.boss = Agent.objects.create(
            user=User.objects.create_user('editboss', password='x'),
            role='admin', role_type='supervisor', agent_name='Boss Admin',
            status='active', is_super_admin=True,
        )
        self.vrenely = Agent.objects.create(
            user=User.objects.create_user('editvrenely', password='x'),
            role='admin', role_type='supervisor', agent_name='Vrenely Salido',
            status='active', can_access_admin_tabs=True, is_official_admin=True,
        )
        self.supervised = Agent.objects.create(
            user=User.objects.create_user('editsupervised', password='x'),
            role='admin', role_type='supervisor', agent_name='Supervised Admin',
            status='active', is_official_admin=True, supervisor=self.vrenely,
        )
        other_supervisor = Agent.objects.create(
            user=User.objects.create_user('editothersup', password='x'),
            role='admin', role_type='supervisor', agent_name='Other Supervisor',
            status='active',
        )
        self.other_admin = Agent.objects.create(
            user=User.objects.create_user('editotheradmin', password='x'),
            role='admin', role_type='supervisor', agent_name='Other Team Admin',
            status='active', is_official_admin=True, supervisor=other_supervisor,
        )
        # A regular (non-Official-Admin) agent — regression target.
        self.regular = Agent.objects.create(
            user=User.objects.create_user('editregular', password='x'),
            role='agent', role_type='agent', agent_name='Regular Agent',
            status='active', track_attendance=True,
        )

    # ── save_adherence_cell ───────────────────────────────────────────────

    def test_supervisor_can_save_status_for_supervised_admin(self):
        self.client.login(username='editvrenely', password='x')
        resp = self.client.post(
            reverse('save_adherence_cell'),
            data=json.dumps({'agent_id': self.supervised.pk, 'date': _WEEK_START.isoformat(), 'status': 'P'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            AdherenceRecord.objects.filter(agent=self.supervised, date=_WEEK_START, status='P').exists()
        )

    def test_supervisor_denied_saving_status_for_out_of_team_admin(self):
        self.client.login(username='editvrenely', password='x')
        resp = self.client.post(
            reverse('save_adherence_cell'),
            data=json.dumps({'agent_id': self.other_admin.pk, 'date': _WEEK_START.isoformat(), 'status': 'P'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(
            AdherenceRecord.objects.filter(agent=self.other_admin, date=_WEEK_START, status='P').exists()
        )

    def test_super_admin_can_save_status_for_any_official_admin(self):
        self.client.login(username='editboss', password='x')
        resp = self.client.post(
            reverse('save_adherence_cell'),
            data=json.dumps({'agent_id': self.other_admin.pk, 'date': _WEEK_START.isoformat(), 'status': 'P'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)

    def test_regular_agent_status_save_unchanged_for_non_holder(self):
        # Regression: a staff user with no admin-tabs permission at all could
        # already save a status for a regular (non-admin) agent before this
        # change — that must still work exactly the same.
        self.client.login(username='editothersup', password='x')
        resp = self.client.post(
            reverse('save_adherence_cell'),
            data=json.dumps({'agent_id': self.regular.pk, 'date': _WEEK_START.isoformat(), 'status': 'P'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)

    # ── adherence_notes ───────────────────────────────────────────────────

    def test_supervisor_can_add_note_for_supervised_admin(self):
        self.client.login(username='editvrenely', password='x')
        resp = self.client.post(reverse('adherence_notes'), data={
            'agent': self.supervised.pk, 'date': _WEEK_START.isoformat(), 'body': 'ok',
        })
        self.assertEqual(resp.status_code, 200)

    def test_supervisor_denied_adding_note_for_out_of_team_admin(self):
        self.client.login(username='editvrenely', password='x')
        resp = self.client.post(reverse('adherence_notes'), data={
            'agent': self.other_admin.pk, 'date': _WEEK_START.isoformat(), 'body': 'nope',
        })
        self.assertEqual(resp.status_code, 403)

    def test_supervisor_denied_reading_notes_for_out_of_team_admin(self):
        # GET is locked down too — can't fetch another team's notes by
        # guessing an agent_id, even though they can't see that row.
        self.client.login(username='editvrenely', password='x')
        resp = self.client.get(reverse('adherence_notes'), data={
            'agent': self.other_admin.pk, 'date': _WEEK_START.isoformat(),
        })
        self.assertEqual(resp.status_code, 403)

    def test_super_admin_can_read_and_add_notes_for_any_official_admin(self):
        self.client.login(username='editboss', password='x')
        resp_get = self.client.get(reverse('adherence_notes'), data={
            'agent': self.other_admin.pk, 'date': _WEEK_START.isoformat(),
        })
        resp_post = self.client.post(reverse('adherence_notes'), data={
            'agent': self.other_admin.pk, 'date': _WEEK_START.isoformat(), 'body': 'ok',
        })
        self.assertEqual(resp_get.status_code, 200)
        self.assertEqual(resp_post.status_code, 200)

    def test_regular_agent_notes_unchanged_for_non_holder(self):
        self.client.login(username='editothersup', password='x')
        resp_get = self.client.get(reverse('adherence_notes'), data={
            'agent': self.regular.pk, 'date': _WEEK_START.isoformat(),
        })
        resp_post = self.client.post(reverse('adherence_notes'), data={
            'agent': self.regular.pk, 'date': _WEEK_START.isoformat(), 'body': 'fine',
        })
        self.assertEqual(resp_get.status_code, 200)
        self.assertEqual(resp_post.status_code, 200)

    # ── edit_adherence_note / delete_adherence_note ─────────────────────────

    def test_supervisor_can_edit_note_for_supervised_admin(self):
        note = AdherenceNote.objects.create(agent=self.supervised, date=_WEEK_START, body='orig')
        self.client.login(username='editvrenely', password='x')
        resp = self.client.post(reverse('edit_adherence_note'), data={
            'note_id': note.pk, 'body': 'updated',
        })
        self.assertEqual(resp.status_code, 200)
        note.refresh_from_db()
        self.assertEqual(note.body, 'updated')

    def test_supervisor_denied_editing_note_for_out_of_team_admin(self):
        note = AdherenceNote.objects.create(agent=self.other_admin, date=_WEEK_START, body='orig')
        self.client.login(username='editvrenely', password='x')
        resp = self.client.post(reverse('edit_adherence_note'), data={
            'note_id': note.pk, 'body': 'hacked',
        })
        self.assertEqual(resp.status_code, 403)
        note.refresh_from_db()
        self.assertEqual(note.body, 'orig')

    def test_super_admin_can_edit_note_for_any_official_admin(self):
        note = AdherenceNote.objects.create(agent=self.other_admin, date=_WEEK_START, body='orig')
        self.client.login(username='editboss', password='x')
        resp = self.client.post(reverse('edit_adherence_note'), data={
            'note_id': note.pk, 'body': 'updated by boss',
        })
        self.assertEqual(resp.status_code, 200)
        note.refresh_from_db()
        self.assertEqual(note.body, 'updated by boss')

    def test_regular_agent_note_edit_unchanged_for_non_holder(self):
        note = AdherenceNote.objects.create(agent=self.regular, date=_WEEK_START, body='orig')
        self.client.login(username='editothersup', password='x')
        resp = self.client.post(reverse('edit_adherence_note'), data={
            'note_id': note.pk, 'body': 'updated',
        })
        self.assertEqual(resp.status_code, 200)
        note.refresh_from_db()
        self.assertEqual(note.body, 'updated')

    def test_plain_staff_denied_editing_note_for_official_admin_not_supervised(self):
        # editothersup has no is_super_admin / can_access_admin_tabs / superuser
        # flag, and does not supervise self.supervised (that's vrenely's report).
        note = AdherenceNote.objects.create(agent=self.supervised, date=_WEEK_START, body='orig')
        self.client.login(username='editothersup', password='x')
        resp = self.client.post(reverse('edit_adherence_note'), data={
            'note_id': note.pk, 'body': 'hacked',
        })
        self.assertEqual(resp.status_code, 403)
        note.refresh_from_db()
        self.assertEqual(note.body, 'orig')

    def test_supervisor_can_delete_note_for_supervised_admin(self):
        note = AdherenceNote.objects.create(agent=self.supervised, date=_WEEK_START, body='orig')
        self.client.login(username='editvrenely', password='x')
        resp = self.client.post(reverse('delete_adherence_note'), data={'note_id': note.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AdherenceNote.objects.filter(pk=note.pk).exists())

    def test_supervisor_denied_deleting_note_for_out_of_team_admin(self):
        note = AdherenceNote.objects.create(agent=self.other_admin, date=_WEEK_START, body='orig')
        self.client.login(username='editvrenely', password='x')
        resp = self.client.post(reverse('delete_adherence_note'), data={'note_id': note.pk})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(AdherenceNote.objects.filter(pk=note.pk).exists())

    def test_super_admin_can_delete_note_for_any_official_admin(self):
        note = AdherenceNote.objects.create(agent=self.other_admin, date=_WEEK_START, body='orig')
        self.client.login(username='editboss', password='x')
        resp = self.client.post(reverse('delete_adherence_note'), data={'note_id': note.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AdherenceNote.objects.filter(pk=note.pk).exists())

    def test_regular_agent_note_delete_unchanged_for_non_holder(self):
        note = AdherenceNote.objects.create(agent=self.regular, date=_WEEK_START, body='orig')
        self.client.login(username='editothersup', password='x')
        resp = self.client.post(reverse('delete_adherence_note'), data={'note_id': note.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AdherenceNote.objects.filter(pk=note.pk).exists())

    def test_plain_staff_denied_deleting_note_for_official_admin_not_supervised(self):
        note = AdherenceNote.objects.create(agent=self.supervised, date=_WEEK_START, body='orig')
        self.client.login(username='editothersup', password='x')
        resp = self.client.post(reverse('delete_adherence_note'), data={'note_id': note.pk})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(AdherenceNote.objects.filter(pk=note.pk).exists())


class DailyUploadStaleActualHoursTests(TestCase):
    """
    Regression coverage for the stale actual_hours bug: after a Daily Hours
    upload is deleted or replaced by a file that no longer contains a given
    agent, that agent's previously-written actual_hours must be zeroed
    without touching status or creating rows for agents that had none.

    Tests 1 and 2 use a lowercase billable Five9Profile username
    deliberately: upload_daily_file's own billable-match filter (a separate,
    pre-existing, out-of-scope gap — see test 3) compares raw
    Five9Profile.five9_username against the always-lowercased
    DailyAgentHours.five9_username, so a mixed-case *billable* username
    would silently skip the actual_hours write entirely, for a reason
    unrelated to what these two tests guard.
    """

    def setUp(self):
        _settings()
        staff_user = User.objects.create_user('dhstaff', password='x')
        Agent.objects.create(
            user=staff_user, role='admin', role_type='supervisor',
            agent_name='DH Staff', status='active',
        )
        self.client.login(username='dhstaff', password='x')
        self.agent_x = _make_agent('agentx')

    def _csv(self, username, login='08:00:00', not_ready='00:30:00'):
        content = f"AGENT,LOGIN TIME,NOT READY TIME\n{username},{login},{not_ready}\n"
        return SimpleUploadedFile('daily.csv', content.encode('utf-8'), content_type='text/csv')

    def _upload(self, date_str, username):
        resp = self.client.post(reverse('upload_daily_file'), {
            'date': date_str, 'file': self._csv(username),
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'], resp.json())
        return resp

    def test_replacement_without_agent_zeroes_actual_hours_keeps_status(self):
        Five9Profile.objects.create(
            agent=self.agent_x, five9_username='jguerrero', billable=True, is_primary=True,
        )
        d = date(2026, 8, 1)
        AdherenceRecord.objects.create(agent=self.agent_x, date=d, status='Quit')

        self._upload(d.isoformat(), 'jguerrero')
        rec = AdherenceRecord.objects.get(agent=self.agent_x, date=d)
        self.assertGreater(rec.actual_hours, Decimal('0'))
        self.assertEqual(rec.status, 'Quit')

        # Replace the same date's file with one that no longer contains X.
        other = _make_agent('agenty')
        Five9Profile.objects.create(agent=other, five9_username='otheruser', billable=True, is_primary=True)
        self._upload(d.isoformat(), 'otheruser')

        rec.refresh_from_db()
        self.assertEqual(rec.actual_hours, Decimal('0'))
        self.assertEqual(rec.status, 'Quit')

    def test_delete_with_no_replacement_zeroes_actual_hours(self):
        Five9Profile.objects.create(
            agent=self.agent_x, five9_username='jguerrero', billable=True, is_primary=True,
        )
        d = date(2026, 8, 2)
        AdherenceRecord.objects.create(agent=self.agent_x, date=d, status='Quit')

        self._upload(d.isoformat(), 'jguerrero')
        rec = AdherenceRecord.objects.get(agent=self.agent_x, date=d)
        self.assertGreater(rec.actual_hours, Decimal('0'))

        before_count = AdherenceRecord.objects.filter(date=d).count()
        resp = self.client.post(
            reverse('delete_daily_upload'),
            data=json.dumps({'date': d.isoformat()}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)

        rec.refresh_from_db()
        self.assertEqual(rec.actual_hours, Decimal('0'))
        self.assertEqual(rec.status, 'Quit')
        self.assertEqual(AdherenceRecord.objects.filter(date=d).count(), before_count)

    def test_mixed_case_billable_username_not_zeroed(self):
        """
        Regression guard for the normalization fix in
        _reconcile_stale_actual_hours: if the .strip().lower() call on
        either side of its billable-username comparison is ever removed, a
        billable Five9Profile username with uppercase characters will fail
        to match the always-lowercased DailyAgentHours row, the agent will
        be (wrongly) excluded from valid_agent_ids, and this test will
        start failing because their real, current hours get zeroed.

        Built directly via the ORM (not through upload_daily_file) to
        represent the state a correct write produces, isolating this from
        the separate, pre-existing billable-match gap in the write loop
        itself (see the class docstring and tests above).
        """
        d = date(2026, 8, 3)
        Five9Profile.objects.create(
            agent=self.agent_x, five9_username='JGuerrero', billable=True, is_primary=True,
        )
        upload = DailyUpload.objects.create(date=d, filename='daily.csv', row_count=1)
        DailyAgentHours.objects.create(
            upload=upload, agent=self.agent_x, five9_username='jguerrero',
            login_seconds=8 * 3600, not_ready_seconds=1800,
        )
        AdherenceRecord.objects.create(agent=self.agent_x, date=d, actual_hours=Decimal('8'))

        from adherence.views import _reconcile_stale_actual_hours
        _reconcile_stale_actual_hours(d)

        rec = AdherenceRecord.objects.get(agent=self.agent_x, date=d)
        self.assertEqual(rec.actual_hours, Decimal('8'))


# ── Adherence roster helper: _get_adherence_agent_pks ─────────────────────────
#
# The helper decides who appears on the Adherence tab and in the combined
# Adherence export. If its pk set changes, agents silently vanish from the tab,
# stop being coded, and lose their adherence bonus with nothing warning anyone.
# So the set is pinned two ways: an oracle test holding the original single-query
# implementation verbatim, and per-branch tests for each half of the predicate.


def _roster_oracle(week_dates, week_start):
    """The original single-query implementation, kept verbatim as a test oracle.

    _get_adherence_agent_pks was split into several small queries because this
    one — four OR'd conditions across four unrestricted LEFT JOINs — made the
    database materialise the cartesian product of every shift, OT, template and
    adherence record per agent. The split must return an identical pk set, so
    this stays here as the thing to compare against.
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


class AdherenceRosterOracleParityTests(TestCase):
    """The split implementation returns exactly the set the original query returned,
    over a fixture that exercises every branch of both halves of the predicate."""

    def setUp(self):
        cache.clear()
        prev_week = _WEEK_START - timedelta(days=7)

        # Eligibility branch 1: active + tracked + not an official admin.
        self.by_shift = _make_agent('par_shift')
        Shift.objects.create(agent=self.by_shift, date=_WEEK_START,
                             start_time=time(9, 0), end_time=time(17, 0), is_off=False)

        self.by_ot = _make_agent('par_ot')
        OvertimeShift.objects.create(agent=self.by_ot, date=_WEEK_START + timedelta(days=2),
                                     start_time=time(17, 0), end_time=time(19, 0))

        self.by_template = _make_agent('par_template')
        ShiftTemplate.objects.create(agent=self.by_template, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))

        self.by_record = _make_agent('par_record')
        AdherenceRecord.objects.create(agent=self.by_record, date=_WEEK_START, status='P')

        # Fails the activity gate entirely.
        self.no_activity = _make_agent('par_none')

        # Has activity, but all of it outside the week (template branch excepted).
        self.outside_week = _make_agent('par_outside')
        Shift.objects.create(agent=self.outside_week, date=prev_week,
                             start_time=time(9, 0), end_time=time(17, 0), is_off=False)
        OvertimeShift.objects.create(agent=self.outside_week, date=prev_week,
                                     start_time=time(17, 0), end_time=time(19, 0))
        AdherenceRecord.objects.create(agent=self.outside_week, date=prev_week, status='P')

        # Fails eligibility three different ways, each with activity present.
        self.untracked = _make_agent('par_untracked')
        self.untracked.track_attendance = False
        self.untracked.save()
        ShiftTemplate.objects.create(agent=self.untracked, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))

        self.official_admin = _make_agent('par_admin')
        self.official_admin.is_official_admin = True
        self.official_admin.save()
        ShiftTemplate.objects.create(agent=self.official_admin, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))

        self.inactive_no_sep = _make_agent('par_inactive')
        self.inactive_no_sep.status = 'inactive'
        self.inactive_no_sep.save()
        ShiftTemplate.objects.create(agent=self.inactive_no_sep, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))

        # Eligibility branch 2: separated, still inside the pay window.
        self.separated_in = _make_agent('par_sep_in')
        self.separated_in.status = 'inactive'
        self.separated_in.save()
        AgentSeparation.objects.create(
            agent=self.separated_in, status='finalized', separation_type='quit',
            last_day_worked=_WEEK_START - timedelta(days=1),
            remove_from_adherence_date=_WEEK_START + timedelta(days=7),
        )
        ShiftTemplate.objects.create(agent=self.separated_in, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))

        # Separated and past the pay window.
        self.separated_out = _make_agent('par_sep_out')
        self.separated_out.status = 'inactive'
        self.separated_out.save()
        AgentSeparation.objects.create(
            agent=self.separated_out, status='finalized', separation_type='quit',
            last_day_worked=prev_week,
            remove_from_adherence_date=_WEEK_START - timedelta(days=1),
        )
        ShiftTemplate.objects.create(agent=self.separated_out, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))

        # Two separation rows: one inside the window, one outside. The conditions must
        # keep matching the same row, and the agent must appear exactly once.
        self.two_seps = _make_agent('par_two_seps')
        self.two_seps.status = 'inactive'
        self.two_seps.save()
        AgentSeparation.objects.create(
            agent=self.two_seps, status='finalized', separation_type='quit',
            last_day_worked=prev_week - timedelta(days=30),
            remove_from_adherence_date=_WEEK_START - timedelta(days=14),
        )
        AgentSeparation.objects.create(
            agent=self.two_seps, status='finalized', separation_type='terminated',
            last_day_worked=_WEEK_START - timedelta(days=1),
            remove_from_adherence_date=_WEEK_START + timedelta(days=7),
        )
        ShiftTemplate.objects.create(agent=self.two_seps, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))

        # adherence_start_date floor, both sides.
        self.floored_out = _make_agent('par_floor_out')
        self.floored_out.adherence_start_date = _WEEK_START + timedelta(days=7)
        self.floored_out.save()
        ShiftTemplate.objects.create(agent=self.floored_out, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))

        self.floored_in = _make_agent('par_floor_in')
        self.floored_in.adherence_start_date = _WEEK_START
        self.floored_in.save()
        ShiftTemplate.objects.create(agent=self.floored_in, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))

    def test_matches_oracle_for_the_week(self):
        cache.clear()
        self.assertEqual(_get_adherence_agent_pks(_WEEK, _WEEK_START),
                         _roster_oracle(_WEEK, _WEEK_START))

    def test_matches_oracle_across_eight_weeks(self):
        for offset in range(-4, 4):
            ws = _WEEK_START + timedelta(weeks=offset)
            wd = [ws + timedelta(days=i) for i in range(7)]
            cache.clear()
            self.assertEqual(_get_adherence_agent_pks(wd, ws), _roster_oracle(wd, ws),
                             msg=f'roster diverged from the oracle for week {ws}')

    def test_returns_a_set_of_pks(self):
        cache.clear()
        pks = _get_adherence_agent_pks(_WEEK, _WEEK_START)
        self.assertIsInstance(pks, set)
        self.assertTrue(all(isinstance(p, int) for p in pks))


class AdherenceRosterBranchTests(TestCase):
    """Each branch of the predicate, asserted directly rather than via the oracle,
    so a change of behaviour is named rather than just reported as a difference."""

    def setUp(self):
        cache.clear()

    def _pks(self, week_dates=None, week_start=None):
        cache.clear()
        return _get_adherence_agent_pks(week_dates or _WEEK, week_start or _WEEK_START)

    # ── Activity gate: each branch admits an agent on its own ──
    def test_shift_in_week_alone_admits(self):
        a = _make_agent('br_shift')
        Shift.objects.create(agent=a, date=_WEEK_START + timedelta(days=3),
                             start_time=time(9, 0), end_time=time(17, 0), is_off=False)
        self.assertIn(a.pk, self._pks())

    def test_ot_in_week_alone_admits(self):
        a = _make_agent('br_ot')
        OvertimeShift.objects.create(agent=a, date=_WEEK_START + timedelta(days=4),
                                     start_time=time(17, 0), end_time=time(19, 0))
        self.assertIn(a.pk, self._pks())

    def test_template_alone_admits(self):
        a = _make_agent('br_tmpl')
        ShiftTemplate.objects.create(agent=a, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))
        self.assertIn(a.pk, self._pks())

    def test_adherence_record_in_week_alone_admits(self):
        a = _make_agent('br_rec')
        AdherenceRecord.objects.create(agent=a, date=_WEEK_START + timedelta(days=1), status='P')
        self.assertIn(a.pk, self._pks())

    def test_no_activity_at_all_excluded(self):
        a = _make_agent('br_noact')
        self.assertNotIn(a.pk, self._pks())

    def test_dated_activity_outside_the_week_excluded(self):
        a = _make_agent('br_outside')
        prev = _WEEK_START - timedelta(days=7)
        Shift.objects.create(agent=a, date=prev, start_time=time(9, 0),
                             end_time=time(17, 0), is_off=False)
        OvertimeShift.objects.create(agent=a, date=prev, start_time=time(17, 0),
                                     end_time=time(19, 0))
        AdherenceRecord.objects.create(agent=a, date=prev, status='P')
        self.assertNotIn(a.pk, self._pks())

    def test_template_branch_stays_unscoped_by_date(self):
        """A template with a far-future effective_from still admits the agent to a
        past week. Deliberate existing behaviour — adherence_start_date is the only
        floor on it — and the reason this branch carries no date filter."""
        a = _make_agent('br_tmpl_future')
        ShiftTemplate.objects.create(agent=a, day_of_week=0, start_time=time(9, 0),
                                     end_time=time(17, 0),
                                     effective_from=_WEEK_START + timedelta(weeks=52))
        past = _WEEK_START - timedelta(weeks=52)
        past_dates = [past + timedelta(days=i) for i in range(7)]
        self.assertIn(a.pk, self._pks(past_dates, past))

    # ── Eligibility: active / tracked / non-admin ──
    def test_active_tracked_non_admin_included(self):
        a = _make_agent('br_ok')
        ShiftTemplate.objects.create(agent=a, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))
        self.assertIn(a.pk, self._pks())

    def test_untracked_excluded(self):
        a = _make_agent('br_untracked')
        a.track_attendance = False
        a.save()
        ShiftTemplate.objects.create(agent=a, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))
        self.assertNotIn(a.pk, self._pks())

    def test_official_admin_excluded(self):
        a = _make_agent('br_admin')
        a.is_official_admin = True
        a.save()
        ShiftTemplate.objects.create(agent=a, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))
        self.assertNotIn(a.pk, self._pks())

    def test_inactive_without_separation_excluded(self):
        a = _make_agent('br_inactive')
        a.status = 'inactive'
        a.save()
        ShiftTemplate.objects.create(agent=a, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))
        self.assertNotIn(a.pk, self._pks())

    # ── Eligibility: separated but still inside the pay window ──
    def _separated(self, username, remove_date, status='finalized'):
        a = _make_agent(username)
        a.status = 'inactive'
        a.save()
        AgentSeparation.objects.create(
            agent=a, status=status, separation_type='quit',
            last_day_worked=_WEEK_START - timedelta(days=1),
            remove_from_adherence_date=remove_date,
        )
        ShiftTemplate.objects.create(agent=a, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))
        return a

    def test_separated_inside_pay_window_included(self):
        a = self._separated('br_sep_in', _WEEK_START + timedelta(days=7))
        self.assertIn(a.pk, self._pks())

    def test_separated_on_removal_date_excluded(self):
        """remove_from_adherence_date is strictly greater-than, so the removal week itself
        is out."""
        a = self._separated('br_sep_on', _WEEK_START)
        self.assertNotIn(a.pk, self._pks())

    def test_separation_not_finalized_excluded(self):
        a = self._separated('br_sep_pending', _WEEK_START + timedelta(days=7), status='pending')
        self.assertNotIn(a.pk, self._pks())

    def test_two_separations_conditions_match_the_same_row(self):
        """One separation row is finalized but out of window; the other is in window but
        not finalized. Neither row satisfies both conditions, so the agent is excluded —
        this is what splitting the conditions across filter() calls would get wrong."""
        a = _make_agent('br_two_seps_mixed')
        a.status = 'inactive'
        a.save()
        AgentSeparation.objects.create(
            agent=a, status='finalized', separation_type='quit',
            last_day_worked=_WEEK_START - timedelta(days=30),
            remove_from_adherence_date=_WEEK_START - timedelta(days=14),
        )
        AgentSeparation.objects.create(
            agent=a, status='pending', separation_type='terminated',
            last_day_worked=_WEEK_START - timedelta(days=1),
            remove_from_adherence_date=_WEEK_START + timedelta(days=7),
        )
        ShiftTemplate.objects.create(agent=a, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))
        self.assertNotIn(a.pk, self._pks())

    def test_two_separations_one_qualifying_included_once(self):
        a = _make_agent('br_two_seps_ok')
        a.status = 'inactive'
        a.save()
        AgentSeparation.objects.create(
            agent=a, status='finalized', separation_type='quit',
            last_day_worked=_WEEK_START - timedelta(days=30),
            remove_from_adherence_date=_WEEK_START - timedelta(days=14),
        )
        AgentSeparation.objects.create(
            agent=a, status='finalized', separation_type='terminated',
            last_day_worked=_WEEK_START - timedelta(days=1),
            remove_from_adherence_date=_WEEK_START + timedelta(days=7),
        )
        ShiftTemplate.objects.create(agent=a, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))
        pks = self._pks()
        self.assertIn(a.pk, pks)
        self.assertEqual(len([p for p in pks if p == a.pk]), 1)

    # ── Activity must belong to the agent it admits ──
    def test_another_agents_activity_does_not_admit(self):
        """The activity queries are scoped to eligible agents; an eligible agent with no
        activity of their own must not be admitted by someone else's rows."""
        bare = _make_agent('br_bare')
        other = _make_agent('br_other')
        Shift.objects.create(agent=other, date=_WEEK_START, start_time=time(9, 0),
                             end_time=time(17, 0), is_off=False)
        pks = self._pks()
        self.assertIn(other.pk, pks)
        self.assertNotIn(bare.pk, pks)


class AdherenceSkillFilterTests(TestCase):
    """Skills Phase 2 — the Adherence tab's skill filter.

    Display-layer narrowing only: it runs after the roster is resolved, in the
    same place _apply_supervisor_filter narrows, and never reaches into
    _get_adherence_agent_pks.
    """

    def setUp(self):
        staff_user = User.objects.create_user('skillfilterstaff', password='x')
        self.staff = Agent.objects.create(
            user=staff_user, role='admin', role_type='supervisor',
            agent_name='Skill Filter Staff', status='active',
        )
        self.client.login(username='skillfilterstaff', password='x')

        sup_user = User.objects.create_user('skillfiltersup', password='x')
        self.supervisor = Agent.objects.create(
            user=sup_user, role='admin', role_type='supervisor',
            agent_name='Skill Filter Sup', status='active',
        )

        self.bilingual = Skill.objects.create(name='Bilingual')
        self.intake = Skill.objects.create(name='Intake')
        self.retired = Skill.objects.create(name='Retired Skill', is_active=False)

        # both      — holds Bilingual AND Intake
        # only_one  — holds Bilingual only (must be excluded by an AND filter)
        # neither   — holds no skills
        self.both = self._rostered('sf_both', [self.bilingual, self.intake])
        self.only_one = self._rostered('sf_only_one', [self.bilingual])
        self.neither = self._rostered('sf_neither', [])

    def _rostered(self, username, skills, supervisor=None):
        """An agent the Adherence roster will admit (ShiftTemplate = activity gate)."""
        agent = _make_agent(username)
        if supervisor is not None:
            agent.supervisor = supervisor
            agent.save()
        ShiftTemplate.objects.create(agent=agent, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))
        if skills:
            agent.skills.set(skills)
        return agent

    def _rows_html(self, query=''):
        url = reverse('adherence_rows_fragment') + f'?week_start={_WEEK_START.isoformat()}{query}'
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertNotIn('error', payload, msg=payload.get('detail', ''))
        return payload['tbody_html']

    def _shown(self, html):
        """Names of the test's own roster agents present in the rendered rows. Scoped to
        the 'sf_' prefix so a supervisor's name in the Supervisor column isn't counted."""
        return {
            a.agent_name for a in Agent.objects.filter(agent_name__startswith='sf_')
            if a.agent_name in html
        }

    # ── AND semantics, the way an Excel filter narrows ──
    def test_two_skills_selected_requires_holding_both(self):
        html = self._rows_html(f'&skills={self.bilingual.pk}&skills={self.intake.pk}')
        shown = self._shown(html)
        self.assertIn('sf_both', shown)
        self.assertNotIn('sf_only_one', shown)   # holds one of two → excluded
        self.assertNotIn('sf_neither', shown)

    def test_one_skill_selected_shows_every_holder(self):
        shown = self._shown(self._rows_html(f'&skills={self.bilingual.pk}'))
        self.assertIn('sf_both', shown)
        self.assertIn('sf_only_one', shown)
        self.assertNotIn('sf_neither', shown)

    def test_no_skill_filter_shows_the_whole_roster(self):
        shown = self._shown(self._rows_html())
        self.assertIn('sf_both', shown)
        self.assertIn('sf_only_one', shown)
        self.assertIn('sf_neither', shown)

    # ── Combines with the existing supervisor filter ──
    def test_skill_and_supervisor_narrow_to_the_intersection(self):
        mine = self._rostered('sf_sup_skilled', [self.bilingual], supervisor=self.supervisor)
        self._rostered('sf_sup_unskilled', [], supervisor=self.supervisor)
        shown = self._shown(self._rows_html(
            f'&supervisor={self.supervisor.pk}&skills={self.bilingual.pk}'
        ))
        self.assertEqual(shown, {'sf_sup_skilled'})
        self.assertIn(mine.agent_name, shown)

    # ── Retired skills ──
    def test_retired_skill_in_the_session_stops_filtering(self):
        """A skill retired while it sits in someone's session must not keep filtering —
        that would be an empty grid with no pill and no visible cause."""
        self.both.skills.add(self.retired)
        shown = self._shown(self._rows_html(f'&skills={self.retired.pk}'))
        self.assertIn('sf_neither', shown)   # filter dropped → full roster
        self.assertIn('sf_both', shown)

    def test_retired_skill_is_not_offered_as_an_option(self):
        resp = self.client.get(
            reverse('adherence_dashboard') + f'?week_start={_WEEK_START.isoformat()}'
        )
        self.assertEqual(resp.status_code, 200)
        offered = {s.name for s in resp.context['active_skills']}
        self.assertIn('Bilingual', offered)
        self.assertNotIn('Retired Skill', offered)

    # ── State travel: session fallback and the per-group requests ──
    def test_filter_survives_week_navigation_with_no_param(self):
        """Week nav links carry only week_start; the filter rides the session, exactly
        as the supervisor filter already does."""
        self._rows_html(f'&skills={self.bilingual.pk}')      # sets the session
        next_week = (_WEEK_START + timedelta(days=7)).isoformat()
        resp = self.client.get(
            reverse('adherence_rows_fragment') + f'?week_start={next_week}'
        )
        shown = self._shown(resp.json()['tbody_html'])
        self.assertNotIn('sf_neither', shown)

    def test_explicit_empty_param_clears_the_session_filter(self):
        self._rows_html(f'&skills={self.bilingual.pk}')
        shown = self._shown(self._rows_html('&skills='))
        self.assertIn('sf_neither', shown)

    def test_filter_applies_on_a_group_request(self):
        self._rostered('sf_grp_skilled', [self.bilingual], supervisor=self.supervisor)
        self._rostered('sf_grp_unskilled', [], supervisor=self.supervisor)
        shown = self._shown(self._rows_html(
            f'&group={self.supervisor.pk}&skills={self.bilingual.pk}'
        ))
        self.assertEqual(shown, {'sf_grp_skilled'})

    def test_filter_applies_on_the_no_supervisor_group_request(self):
        shown = self._shown(self._rows_html(f'&group=__none__&skills={self.bilingual.pk}'))
        self.assertIn('sf_both', shown)
        self.assertNotIn('sf_neither', shown)

    # ── Never 500: a bad value would mark every group failed on screen ──
    def test_garbage_skill_value_is_ignored_not_fatal(self):
        shown = self._shown(self._rows_html('&skills=notanumber'))
        self.assertIn('sf_neither', shown)

    def test_unknown_skill_pk_is_ignored(self):
        shown = self._shown(self._rows_html('&skills=99999'))
        self.assertIn('sf_neither', shown)

    # ── The roster query itself is untouched ──
    def test_roster_pks_identical_with_and_without_a_skill_filter(self):
        before = _get_adherence_agent_pks(_WEEK, _WEEK_START)
        self._rows_html(f'&skills={self.bilingual.pk}&skills={self.intake.pk}')
        after = _get_adherence_agent_pks(_WEEK, _WEEK_START)
        self.assertEqual(before, after)
        self.assertIn(self.neither.pk, after)   # still in the roster, just not displayed

    # ── Query count is flat in the number of skills and the number of agents ──
    # Each comparison holds the *rendered* set identical, so the only variable is the
    # filter itself — row count drives unrelated per-row query work.
    def _query_count(self, query=''):
        # Warm up first: the opening request of a session also writes the session row and
        # loads that week's BillingSettings, which would otherwise read as filter cost.
        self._rows_html(query)
        ctx = CaptureQueriesContext(connection)
        with ctx:
            html = self._rows_html(query)
        return len(ctx), self._shown(html)

    def test_query_count_does_not_grow_with_the_number_of_skills(self):
        third = Skill.objects.create(name='Third')
        self.both.skills.add(third)
        # Both filters resolve to exactly {sf_both}: only_one lacks Intake.
        two_count, two_shown = self._query_count(
            f'&skills={self.bilingual.pk}&skills={self.intake.pk}'
        )
        three_count, three_shown = self._query_count(
            f'&skills={self.bilingual.pk}&skills={self.intake.pk}&skills={third.pk}'
        )
        self.assertEqual(two_shown, {'sf_both'})
        self.assertEqual(three_shown, {'sf_both'})
        self.assertEqual(three_count, two_count)

    def test_query_count_does_not_grow_with_the_number_of_agents(self):
        small_count, small_shown = self._query_count(
            f'&skills={self.bilingual.pk}&skills={self.intake.pk}'
        )
        # 10 more agents on the roster, none of them matching → same rendered set.
        for i in range(10):
            self._rostered(f'sf_bulk_{i}', [])
        big_count, big_shown = self._query_count(
            f'&skills={self.bilingual.pk}&skills={self.intake.pk}'
        )
        self.assertEqual(small_shown, big_shown)
        self.assertEqual(big_count, small_count)

    def test_unfiltered_tab_runs_no_skill_queries_at_all(self):
        """The unfiltered grid is the common case and runs once per supervisor group —
        it must not pay for the filter it isn't using."""
        self._rows_html()                       # warm up
        ctx = CaptureQueriesContext(connection)
        with ctx:
            self._rows_html()
        skill_table = Skill._meta.db_table
        self.assertEqual([q for q in ctx.captured_queries if skill_table in q['sql']], [])

    def test_skill_filter_adds_exactly_one_query(self):
        """Filtering on a skill every roster agent holds renders the same rows as no
        filter at all, so the whole delta is the filter's own pk lookup."""
        everyone = Skill.objects.create(name='Everyone')
        for agent in (self.both, self.only_one, self.neither):
            agent.skills.add(everyone)
        plain_count, plain_shown = self._query_count()
        filtered_count, filtered_shown = self._query_count(f'&skills={everyone.pk}')
        self.assertEqual(filtered_shown, plain_shown)
        # +1 narrowing lookup, +1 validating the requested ids against active skills.
        self.assertEqual(filtered_count, plain_count + 2)


class AdherenceEmptyStateTests(TestCase):
    """An empty grid must name the filter as the reason. The server renders this for a
    full-table request; per-group requests suppress it (an empty group is normal) and the
    all-groups-empty case is caught in the progressive loader instead."""

    def setUp(self):
        staff_user = User.objects.create_user('emptystatestaff', password='x')
        Agent.objects.create(
            user=staff_user, role='admin', role_type='supervisor',
            agent_name='Empty State Staff', status='active',
        )
        self.client.login(username='emptystatestaff', password='x')
        self.held_by_nobody = Skill.objects.create(name='Held By Nobody')
        agent = _make_agent('es_agent')
        ShiftTemplate.objects.create(agent=agent, day_of_week=0,
                                     start_time=time(9, 0), end_time=time(17, 0))

    def _tbody(self, query):
        url = reverse('adherence_rows_fragment') + f'?week_start={_WEEK_START.isoformat()}{query}'
        return self.client.get(url).json()['tbody_html']

    def test_no_match_says_so_rather_than_no_agents_found(self):
        html = self._tbody(f'&skills={self.held_by_nobody.pk}')
        self.assertIn('No agents match the current filters.', html)
        self.assertNotIn('No active agents found.', html)

    def test_unfiltered_empty_roster_keeps_the_original_wording(self):
        Agent.objects.filter(agent_name='es_agent').delete()
        html = self._tbody('')
        self.assertIn('No active agents found.', html)
        self.assertNotIn('No agents match', html)

    def test_group_request_stays_silent_so_empty_groups_render_nothing(self):
        """An empty group must append nothing at all — that is what lets a group that
        FAILED (which renders an explicit 'couldn't load' marker row) stay
        distinguishable from one the filter simply emptied."""
        html = self._tbody(f'&group=__none__&skills={self.held_by_nobody.pk}')
        self.assertNotIn('No agents match', html)
        self.assertNotIn('No active agents found', html)
        self.assertEqual(html.strip(), '')

    def test_page_renders_the_panel_pills_and_match_count_slot(self):
        resp = self.client.get(
            reverse('adherence_dashboard')
            + f'?week_start={_WEEK_START.isoformat()}&skills={self.held_by_nobody.pk}'
        )
        body = resp.content.decode()
        self.assertIn('id="filters-popover"', body)
        self.assertIn('Skill: Held By Nobody', body)          # the pill
        self.assertIn('id="adh-match-count"', body)           # the count slot
        self.assertIn('const _ADH_FILTERS_ACTIVE = true', body)

    def test_no_filter_means_no_pills_and_no_active_flag(self):
        resp = self.client.get(
            reverse('adherence_dashboard') + f'?week_start={_WEEK_START.isoformat()}&skills='
        )
        body = resp.content.decode()
        self.assertIn('const _ADH_FILTERS_ACTIVE = false', body)
        self.assertNotIn('id="adh-match-count"', body)

    def test_selected_skill_renders_checked_in_the_panel(self):
        resp = self.client.get(
            reverse('adherence_dashboard')
            + f'?week_start={_WEEK_START.isoformat()}&skills={self.held_by_nobody.pk}'
        )
        body = resp.content.decode()
        self.assertRegex(
            body, rf'name="skills" value="{self.held_by_nobody.pk}"[^>]*\n?[^>]*checked'
        )


class AdherenceFilterPillTests(TestCase):
    """The pill links the Filters panel renders. A remove link that drops the last skill
    must emit the explicit `skills=` sentinel — without it the URL carries no skills param,
    the view falls back to the session, and the 'removed' filter silently stays on."""

    def setUp(self):
        self.bilingual = Skill.objects.create(name='Bilingual')
        self.intake = Skill.objects.create(name='Intake')

    def _pills(self, skill_ids, supervisor_id=''):
        return _adherence_filter_pills(
            _WEEK_START, supervisor_id, skill_ids, Skill.objects.filter(is_active=True)
        )

    def test_removing_the_only_pill_emits_the_clear_sentinel(self):
        pill = self._pills([self.bilingual.pk])[0]
        self.assertEqual(pill['value'], 'Bilingual')
        self.assertIn('skills=', pill['remove_url'])
        self.assertNotIn(f'skills={self.bilingual.pk}', pill['remove_url'])

    def test_removing_one_of_two_keeps_the_other(self):
        pills = self._pills([self.bilingual.pk, self.intake.pk])
        bilingual_pill = next(p for p in pills if p['value'] == 'Bilingual')
        self.assertIn(f'skills={self.intake.pk}', bilingual_pill['remove_url'])
        self.assertNotIn(f'skills={self.bilingual.pk}', bilingual_pill['remove_url'])

    def test_remove_url_preserves_week_and_supervisor(self):
        pill = self._pills([self.bilingual.pk], supervisor_id='7')[0]
        self.assertIn(f'week_start={_WEEK_START.isoformat()}', pill['remove_url'])
        self.assertIn('supervisor=7', pill['remove_url'])

    def test_non_numeric_supervisor_is_left_out_of_the_url(self):
        pill = self._pills([self.bilingual.pk], supervisor_id='../evil')[0]
        self.assertNotIn('evil', pill['remove_url'])

    def test_clear_all_url_drops_every_skill_but_keeps_the_week(self):
        url = _adherence_filter_url(_WEEK_START, '', [])
        self.assertIn(f'week_start={_WEEK_START.isoformat()}', url)
        self.assertTrue(url.endswith('skills='))


class UncodedExtraAccountReviewTests(TestCase):
    """Read-only review of uncoded time on non-primary Five9 accounts. Pins the
    three production answer-key outcomes for Mark Reyes' week (Sep 14-20 2026),
    that a day with no non-primary login is never reviewed at all (narrower than
    reviewing every day in the range), that an agent with no primary account is
    skipped rather than guessed at, and that the command writes nothing —
    including BillingSettings, which a naive BillingSettings.get_for_week() call
    could otherwise create via get_or_create."""

    def _run(self, start, end):
        out = io.StringIO()
        from django.core.management import call_command
        # call_command applies argparse's `type=` conversion only to positional/
        # string args -- keyword args pass straight into options, so these must
        # already be date objects (what --start/--end would parse into).
        call_command('uncoded_extra_account_review', stdout=out, start=start, end=end)
        return out.getvalue()

    def _upload(self, d):
        return DailyUpload.objects.get_or_create(date=d, defaults={'filename': 'd.csv', 'row_count': 1})[0]

    def _hours(self, d, agent, username, login_seconds):
        DailyAgentHours.objects.create(
            upload=self._upload(d), agent=agent, five9_username=username,
            login_seconds=login_seconds, not_ready_seconds=0,
        )

    def _make_mark(self):
        agent = _make_agent('mark_test')
        Five9Profile.objects.create(agent=agent, five9_username='marreyes_test', is_primary=True, billable=True)
        Five9Profile.objects.create(agent=agent, five9_username='markreyes_test', is_primary=False, billable=False)
        return agent

    def test_answer_key_mark_reyes_week(self):
        agent = self._make_mark()
        mon = date(2026, 9, 14)
        days = [mon + timedelta(days=i) for i in range(7)]  # Mon .. Sun

        # Mon, Tue, Wed, Thu, Sat: 3:00 on the second account, coded to within a
        # second, no scheduled hours that day — must NOT be flagged.
        for i in (0, 1, 2, 3, 5):
            d = days[i]
            self._hours(d, agent, 'markreyes_test', 3 * 3600)
            Coding.objects.create(agent=agent, date=d, start_time=time(9, 0), end_time=time(12, 0),
                                  is_admin_coding=False)

        # Fri Sep 18: 9:00 scheduled OT, 0 billable login, 0 coded, 8:59 on the
        # second account — must be flagged.
        fri = days[4]
        OvertimeShift.objects.create(agent=agent, date=fri, start_time=time(9, 0), end_time=time(18, 0))
        self._hours(fri, agent, 'markreyes_test', 8 * 3600 + 59 * 60)

        # Sun Sep 20: 4:00 scheduled OT, 3:56 on the second account, 3:56 coded —
        # a 4-minute gap, under the 5-minute threshold — must NOT be flagged.
        sun = days[6]
        OvertimeShift.objects.create(agent=agent, date=sun, start_time=time(9, 0), end_time=time(13, 0))
        self._hours(sun, agent, 'markreyes_test', 3 * 3600 + 56 * 60)
        Coding.objects.create(agent=agent, date=sun, start_time=time(9, 0), end_time=time(12, 56),
                              is_admin_coding=False)

        out = self._run(mon, sun)

        self.assertIn('FLAGGED DAYS', out)
        flagged_section = out.split('FLAGGED DAYS', 1)[1].split('No primary marked', 1)[0]
        self.assertIn(fri.isoformat(), flagged_section)
        self.assertIn('scheduled=9:00', flagged_section)
        self.assertIn('billable_login=0:00', flagged_section)
        self.assertIn('coded=0:00', flagged_section)
        self.assertIn('non_primary_login=8:59', flagged_section)
        for d in (days[0], days[1], days[2], days[3], days[5], sun):
            self.assertNotIn(d.isoformat(), flagged_section)
        self.assertIn('7 day(s) reviewed, 6 not flagged.', out)

    def test_no_primary_marked_is_skipped_not_guessed(self):
        agent = _make_agent('skip_test')
        Five9Profile.objects.create(agent=agent, five9_username='skip_second', is_primary=False, billable=False)
        d = date(2026, 9, 14)
        self._hours(d, agent, 'skip_second', 3600)
        out = self._run(d, d)
        self.assertIn('No primary marked, skipped:', out)
        self.assertIn('skip_test', out)
        self.assertIn('0 day(s) reviewed, 0 not flagged.', out)

    def test_day_without_non_primary_login_is_never_reviewed(self):
        """Only the specific days with non-primary login get reviewed — not every
        day in the range. Without this narrowing, Tuesday's ordinary short day on
        the primary account alone would be wrongly pulled in and flagged."""
        agent = _make_agent('narrow_test')
        Five9Profile.objects.create(agent=agent, five9_username='narrow_primary', is_primary=True, billable=True)
        Five9Profile.objects.create(agent=agent, five9_username='narrow_second', is_primary=False, billable=False)
        mon = date(2026, 9, 14)
        tue = mon + timedelta(days=1)

        self._hours(mon, agent, 'narrow_second', 3600)  # Monday: non-primary login -> reviewed

        # Tuesday: only primary-account login, well short of a full scheduled
        # shift — would flag rule (1) if it were (wrongly) reviewed.
        Shift.objects.create(agent=agent, date=tue, start_time=time(9, 0), end_time=time(17, 0))
        self._hours(tue, agent, 'narrow_primary', 2 * 3600)

        out = self._run(mon, tue + timedelta(days=3))

        self.assertIn(mon.isoformat(), out)
        self.assertNotIn(tue.isoformat(), out)
        self.assertIn('1 day(s) reviewed,', out)

    def test_writes_nothing(self):
        agent = self._make_mark()
        d = date(2026, 9, 18)
        self._hours(d, agent, 'markreyes_test', 3600)

        def _counts():
            return (
                Agent.objects.count(), Five9Profile.objects.count(),
                DailyUpload.objects.count(), DailyAgentHours.objects.count(),
                Coding.objects.count(), OvertimeShift.objects.count(),
                Shift.objects.count(), AdherenceRecord.objects.count(),
                BillingSettings.objects.count(), BillingSettingsHistory.objects.count(),
            )

        before = _counts()
        self._run(d, d)
        self.assertEqual(_counts(), before)


class PrimaryAccountDisplayHoursTests(TestCase):
    """Adherence DISPLAY hours count only Five9 login/not-ready time from the
    agent's primary Five9 account — no fallback to any other account. Mirrors
    the production Mark Reyes fixture (week of Sep 14 2026): a primary billable
    account plus a non-primary, non-billable 'extra' account. Every fixture
    puts the non-primary row LAST so a pre-fix failure is a stable wrong
    number, not an order-dependent coincidence."""

    def setUp(self):
        _settings()

    def _make_two_account_agent(self, username):
        agent = _make_agent(username)
        Five9Profile.objects.create(
            agent=agent, five9_username=f'{username}_primary', is_primary=True, billable=True,
        )
        Five9Profile.objects.create(
            agent=agent, five9_username=f'{username}_extra', is_primary=False, billable=False,
        )
        return agent

    def _upload(self, d):
        return DailyUpload.objects.get_or_create(date=d, defaults={'filename': 'd.csv', 'row_count': 1})[0]

    def _hours(self, d, agent, username, login_seconds, not_ready_seconds=0):
        DailyAgentHours.objects.create(
            upload=self._upload(d), agent=agent, five9_username=username,
            login_seconds=login_seconds, not_ready_seconds=not_ready_seconds,
        )

    def test_monday_primary_plus_extra_shows_only_primary_plus_coding(self):
        """Primary 8h login + extra 3h (ignored) + 3h coding. The Adherence
        cell is stored actual_hours (login-only, after NR deduction) plus that
        day's coded hours — 8 + 3 = 11h, never 14h from the extra account
        leaking in."""
        agent = self._make_two_account_agent('mark')
        d = date(2026, 9, 14)
        self._hours(d, agent, 'mark_primary', 8 * 3600)
        self._hours(d, agent, 'mark_extra', 3 * 3600)
        Coding.objects.create(agent=agent, date=d, start_time=time(9, 0), end_time=time(12, 0),
                              is_admin_coding=False)
        from adherence.views import _refresh_actual_hours
        _refresh_actual_hours(agent.pk, d)
        rec = AdherenceRecord.objects.get(agent=agent, date=d)
        self.assertEqual(rec.actual_hours, Decimal('8'))
        cell = rec.actual_hours + Decimal('3')  # that day's coded hours, added by _build_rows
        self.assertEqual(cell, Decimal('11'))

    def test_friday_extra_account_only_shows_zero_login(self):
        """9:00 OT worked entirely on the extra account, never coded, must show
        0:00 — the exact Mark Reyes Fri Sep 18 production case."""
        agent = self._make_two_account_agent('mark')
        d = date(2026, 9, 18)
        self._hours(d, agent, 'mark_extra', 8 * 3600 + 59 * 60)
        from adherence.views import _refresh_actual_hours
        _refresh_actual_hours(agent.pk, d)
        rec = AdherenceRecord.objects.filter(agent=agent, date=d).first()
        self.assertIsNone(rec)  # no primary row that day -> nothing to write; upload path zeroes it

    def test_sunday_extra_plus_matching_coding_shows_coding_only(self):
        """Extra-account login 3:56 plus a matching 3:56 coding must show 3:56,
        not the doubled 7:52 the old fallback produced."""
        agent = self._make_two_account_agent('mark')
        d = date(2026, 9, 20)
        self._hours(d, agent, 'mark_extra', 3 * 3600 + 56 * 60)
        Coding.objects.create(agent=agent, date=d, start_time=time(9, 0), end_time=time(12, 56),
                              is_admin_coding=False)
        from adherence.views import _refresh_actual_hours
        _refresh_actual_hours(agent.pk, d)
        rec = AdherenceRecord.objects.filter(agent=agent, date=d).first()
        self.assertIsNone(rec)  # no primary row -> no write from _refresh_actual_hours

    def test_agent_with_no_primary_marked_gets_zero_login(self):
        agent = _make_agent('noprimary')
        Five9Profile.objects.create(agent=agent, five9_username='np_a', is_primary=False, billable=True)
        Five9Profile.objects.create(agent=agent, five9_username='np_b', is_primary=False, billable=True)
        d = date(2026, 9, 14)
        self._hours(d, agent, 'np_b', 8 * 3600)
        from adherence.views import _refresh_actual_hours
        _refresh_actual_hours(agent.pk, d)
        self.assertIsNone(AdherenceRecord.objects.filter(agent=agent, date=d).first())

    def test_two_primary_rows_are_summed_not_last_wins(self):
        agent = _make_agent('twoprimary')
        Five9Profile.objects.create(agent=agent, five9_username='tp_a', is_primary=True, billable=True)
        Five9Profile.objects.create(agent=agent, five9_username='tp_b', is_primary=True, billable=True)
        d = date(2026, 9, 14)
        self._hours(d, agent, 'tp_a', 4 * 3600)
        self._hours(d, agent, 'tp_b', 3 * 3600)
        from adherence.views import _refresh_actual_hours
        _refresh_actual_hours(agent.pk, d)
        rec = AdherenceRecord.objects.get(agent=agent, date=d)
        self.assertEqual(rec.actual_hours, Decimal('7'))

    def test_not_ready_deduction_uses_only_primary_not_ready_seconds(self):
        """The extra account's not-ready seconds must never bleed into the
        deduction applied to the primary's login."""
        agent = self._make_two_account_agent('nrtest')
        d = date(2026, 9, 14)
        self._hours(d, agent, 'nrtest_primary', 8 * 3600, not_ready_seconds=0)
        self._hours(d, agent, 'nrtest_extra', 2 * 3600, not_ready_seconds=2 * 3600)
        from adherence.views import _refresh_actual_hours
        _refresh_actual_hours(agent.pk, d)
        rec = AdherenceRecord.objects.get(agent=agent, date=d)
        self.assertEqual(rec.actual_hours, Decimal('8'))

    def test_upload_view_end_to_end_matches_stored_value(self):
        """Drives the real upload_daily_file view with a two-row CSV, non-primary
        row last, mirroring DailyUploadStaleActualHoursTests' style."""
        staff_user = User.objects.create_user('uploadstaff', password='x')
        Agent.objects.create(user=staff_user, role='admin', role_type='supervisor',
                             agent_name='Upload Staff', status='active')
        self.client.login(username='uploadstaff', password='x')
        agent = self._make_two_account_agent('e2e')
        d = date(2026, 9, 14)
        content = (
            "AGENT,LOGIN TIME,NOT READY TIME\n"
            "e2e_primary,08:00:00,00:00:00\n"
            "e2e_extra,03:00:00,00:00:00\n"
        )
        csv_file = SimpleUploadedFile('daily.csv', content.encode('utf-8'), content_type='text/csv')
        resp = self.client.post(reverse('upload_daily_file'), {'date': d.isoformat(), 'file': csv_file})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'], resp.json())
        rec = AdherenceRecord.objects.get(agent=agent, date=d)
        self.assertEqual(rec.actual_hours, Decimal('8'))


class SupervisorNonBillablePrimaryTests(TestCase):
    """A primary account that is NOT billable, plus a second billable account —
    the three-supervisor production shape (Jesus Urbina, Jose Aranda, Misael
    Martinez). Adherence display must follow the primary; the money engine
    must be completely unaffected."""

    def setUp(self):
        self.settings = _settings()

    def _make_agent_with_hours(self, primary_billable, second_billable, primary_seconds, second_seconds):
        agent = _make_agent('sup_test')
        Five9Profile.objects.create(
            agent=agent, five9_username='sup_primary', is_primary=True, billable=primary_billable,
        )
        Five9Profile.objects.create(
            agent=agent, five9_username='sup_second', is_primary=False, billable=second_billable,
        )
        d = _WEEK[0]
        upload = DailyUpload.objects.get_or_create(date=d, defaults={'filename': 'd.csv', 'row_count': 1})[0]
        DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username='sup_primary',
                                       login_seconds=primary_seconds, not_ready_seconds=0)
        DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username='sup_second',
                                       login_seconds=second_seconds, not_ready_seconds=0)
        return agent, d

    def test_adherence_shows_only_the_primary_account_login(self):
        agent, d = self._make_agent_with_hours(
            primary_billable=False, second_billable=True,
            primary_seconds=20 * 60, second_seconds=8 * 3600,
        )
        from adherence.views import _refresh_actual_hours
        _refresh_actual_hours(agent.pk, d)
        rec = AdherenceRecord.objects.get(agent=agent, date=d)
        self.assertEqual(rec.actual_hours, Decimal(str(round(20 * 60 / 3600, 6))))

    def test_billable_weekly_data_is_unmoved(self):
        from finance.views import _get_billable_weekly_data
        agent, d = self._make_agent_with_hours(
            primary_billable=False, second_billable=True,
            primary_seconds=20 * 60, second_seconds=8 * 3600,
        )
        data = _get_billable_weekly_data([agent], _WEEK, self.settings)
        self.assertEqual(data[agent.pk]['actual_hrs'], Decimal('8'))

    def test_billable_weekly_data_ignores_is_primary_entirely(self):
        from finance.views import _get_billable_weekly_data
        agent, d = self._make_agent_with_hours(
            primary_billable=False, second_billable=True,
            primary_seconds=20 * 60, second_seconds=8 * 3600,
        )
        before = _get_billable_weekly_data([agent], _WEEK, self.settings)
        before_snapshot = {k: v for k, v in before[agent.pk].items() if k != 'agent'}

        # Swap which account is primary, holding billable fixed.
        Five9Profile.objects.filter(agent=agent, five9_username='sup_primary').update(is_primary=False)
        Five9Profile.objects.filter(agent=agent, five9_username='sup_second').update(is_primary=True)

        after = _get_billable_weekly_data([agent], _WEEK, self.settings)
        after_snapshot = {k: v for k, v in after[agent.pk].items() if k != 'agent'}
        self.assertEqual(before_snapshot, after_snapshot)


class PrimaryAccountDailyHoursRenderTests(TestCase):
    """The Daily Hours page: every uploaded row stays visible; a matched
    non-primary row keeps Login Time but dashes every other column."""

    def setUp(self):
        _settings()
        staff_user = User.objects.create_user('dhrenderstaff', password='x')
        Agent.objects.create(user=staff_user, role='admin', role_type='supervisor',
                             agent_name='DH Render Staff', status='active')
        self.client.login(username='dhrenderstaff', password='x')

    def _rows_for(self, d):
        resp = self.client.get(reverse('daily_hours'), {'week_start': _get_week_start_of(d).isoformat()})
        self.assertEqual(resp.status_code, 200)
        for slot in resp.context['day_slots']:
            if slot['date'] == d:
                return slot['rows']
        return []

    def test_non_primary_row_dashes_every_column_but_login(self):
        agent = _make_agent('renderx')
        Five9Profile.objects.create(agent=agent, five9_username='renderx_primary', is_primary=True, billable=True)
        Five9Profile.objects.create(agent=agent, five9_username='renderx_extra', is_primary=False, billable=False)
        d = _WEEK[0]
        upload = DailyUpload.objects.create(date=d, filename='d.csv', row_count=1)
        DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username='renderx_primary',
                                       login_seconds=8 * 3600, not_ready_seconds=0)
        DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username='renderx_extra',
                                       login_seconds=3 * 3600, not_ready_seconds=0)

        rows = self._rows_for(d)
        primary_row = next(r for r in rows if r['dah'].five9_username == 'renderx_primary')
        extra_row = next(r for r in rows if r['dah'].five9_username == 'renderx_extra')

        self.assertIsNotNone(primary_row['total_seconds'])
        self.assertIsNone(extra_row['not_ready_seconds'])
        self.assertIsNone(extra_row['coded_seconds'])
        self.assertIsNone(extra_row['total_seconds'])
        self.assertIsNone(extra_row['allowance_seconds'])
        self.assertIsNone(extra_row['excess_seconds'])
        self.assertIsNone(extra_row['final_seconds'])
        # Login Time (the raw model field) stays visible on the extra row.
        self.assertEqual(extra_row['dah'].login_seconds, 3 * 3600)

    def test_agent_with_no_primary_shows_zero_login_on_every_row(self):
        agent = _make_agent('nopriref')
        Five9Profile.objects.create(agent=agent, five9_username='npref_a', is_primary=False, billable=True)
        d = _WEEK[0]
        upload = DailyUpload.objects.create(date=d, filename='d.csv', row_count=1)
        DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username='npref_a',
                                       login_seconds=8 * 3600, not_ready_seconds=0)
        rows = self._rows_for(d)
        row = next(r for r in rows if r['dah'].five9_username == 'npref_a')
        self.assertIsNone(row['total_seconds'])

    def test_seconds_to_hhmmss_renders_none_as_dash(self):
        from adherence.templatetags.adherence_filters import seconds_to_hhmmss
        self.assertEqual(seconds_to_hhmmss(None), '—')
        self.assertEqual(seconds_to_hhmmss(0), '')
        self.assertEqual(seconds_to_hhmmss(3600), '01:00:00')


def _get_week_start_of(d):
    return d - timedelta(days=d.weekday())


class AutoPrimaryOnSaveTests(TestCase):
    """A lone Five9 account is always marked primary on save, through the one
    existing Five9 save path (_save_five9_profiles), so an agent never ends up
    with a single account and no primary — which would show zero adherence
    login for no operational reason. Calls _save_five9_profiles directly
    (the single real write path, shared by agent_create and agent_edit)
    with a minimal fake request, rather than driving the full Edit User form
    — which has many unrelated required fields that would make this test
    fragile to changes elsewhere on that form."""

    def _fake_request(self, post_data):
        # A super admin, because removing a saved account is super-admin-only.
        # This class is about the auto-primary rule, not about permissions —
        # those have their own coverage in Five9AccountPermissionTests.
        return SimpleNamespace(POST=post_data, has_finance_access=True)

    def test_single_account_is_auto_marked_primary_on_save(self):
        from scheduling.views import _save_five9_profiles
        agent = _make_agent('autoprimary1')
        profile = Five9Profile.objects.create(agent=agent, five9_username='ap1', is_primary=False, billable=True)
        request = self._fake_request({
            'five9_primary': '',
            f'five9_{profile.pk}_username': 'ap1',
            f'five9_{profile.pk}_label': '',
            f'five9_{profile.pk}_billable': 'on',
        })
        _save_five9_profiles(request, agent)
        profile.refresh_from_db()
        self.assertTrue(profile.is_primary)

    def test_auto_primary_does_not_override_an_explicit_choice(self):
        from scheduling.views import _save_five9_profiles
        agent = _make_agent('autoprimary2')
        p1 = Five9Profile.objects.create(agent=agent, five9_username='ap2a', is_primary=False, billable=True)
        p2 = Five9Profile.objects.create(agent=agent, five9_username='ap2b', is_primary=False, billable=True)
        request = self._fake_request({
            'five9_primary': str(p2.pk),
            f'five9_{p1.pk}_username': 'ap2a', f'five9_{p1.pk}_label': '', f'five9_{p1.pk}_billable': 'on',
            f'five9_{p2.pk}_username': 'ap2b', f'five9_{p2.pk}_label': '', f'five9_{p2.pk}_billable': 'on',
        })
        _save_five9_profiles(request, agent)
        p1.refresh_from_db()
        p2.refresh_from_db()
        self.assertFalse(p1.is_primary)
        self.assertTrue(p2.is_primary)

    def test_auto_primary_does_not_fire_with_two_accounts_and_none_chosen(self):
        from scheduling.views import _save_five9_profiles
        agent = _make_agent('autoprimary3')
        p1 = Five9Profile.objects.create(agent=agent, five9_username='ap3a', is_primary=False, billable=True)
        p2 = Five9Profile.objects.create(agent=agent, five9_username='ap3b', is_primary=False, billable=True)
        request = self._fake_request({
            'five9_primary': '',
            f'five9_{p1.pk}_username': 'ap3a', f'five9_{p1.pk}_label': '', f'five9_{p1.pk}_billable': 'on',
            f'five9_{p2.pk}_username': 'ap3b', f'five9_{p2.pk}_label': '', f'five9_{p2.pk}_billable': 'on',
        })
        _save_five9_profiles(request, agent)
        p1.refresh_from_db()
        p2.refresh_from_db()
        self.assertFalse(p1.is_primary)
        self.assertFalse(p2.is_primary)

    def test_auto_primary_fires_when_going_from_two_accounts_to_one_via_delete(self):
        from scheduling.views import _save_five9_profiles
        agent = _make_agent('autoprimary4')
        p1 = Five9Profile.objects.create(agent=agent, five9_username='ap4a', is_primary=False, billable=True)
        p2 = Five9Profile.objects.create(agent=agent, five9_username='ap4b', is_primary=False, billable=True)
        request = self._fake_request({
            'five9_primary': '',
            f'five9_{p1.pk}_delete': 'on',
            f'five9_{p2.pk}_username': 'ap4b', f'five9_{p2.pk}_label': '', f'five9_{p2.pk}_billable': 'on',
        })
        _save_five9_profiles(request, agent)
        p2.refresh_from_db()
        self.assertTrue(p2.is_primary)
        self.assertFalse(Five9Profile.objects.filter(pk=p1.pk).exists())


class PrimaryAccountQueryCountTests(TestCase):
    """The primary resolver is bulk — one query up front regardless of roster
    size — never a query per agent."""

    def setUp(self):
        _settings()
        staff_user = User.objects.create_user('qcstaff', password='x')
        Agent.objects.create(user=staff_user, role='admin', role_type='supervisor',
                             agent_name='QC Staff', status='active')
        self.client.login(username='qcstaff', password='x')

    def _make_two_account_agent(self, n):
        agent = _make_agent(f'qc_agent_{n}')
        Five9Profile.objects.create(agent=agent, five9_username=f'qc_{n}_primary', is_primary=True, billable=True)
        Five9Profile.objects.create(agent=agent, five9_username=f'qc_{n}_extra', is_primary=False, billable=False)
        d = _WEEK[0]
        upload, _ = DailyUpload.objects.get_or_create(date=d, defaults={'filename': 'd.csv', 'row_count': 1})
        DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username=f'qc_{n}_primary',
                                       login_seconds=8 * 3600, not_ready_seconds=0)
        DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username=f'qc_{n}_extra',
                                       login_seconds=3 * 3600, not_ready_seconds=0)
        return agent

    def test_daily_hours_query_count_flat_as_row_count_grows(self):
        for i in range(2):
            self._make_two_account_agent(i)
        self.client.get(reverse('daily_hours'), {'week_start': _WEEK[0].isoformat()})  # warm up
        ctx = CaptureQueriesContext(connection)
        with ctx:
            self.client.get(reverse('daily_hours'), {'week_start': _WEEK[0].isoformat()})
        small_count = len(ctx)

        for i in range(2, 12):
            self._make_two_account_agent(i)
        ctx2 = CaptureQueriesContext(connection)
        with ctx2:
            self.client.get(reverse('daily_hours'), {'week_start': _WEEK[0].isoformat()})
        big_count = len(ctx2)

        self.assertEqual(small_count, big_count,
                         'the primary-username resolver must stay one bulk query, not per-agent')

    def _make_roster_agent(self, n):
        agent = self._make_two_account_agent(n)
        ShiftTemplate.objects.create(agent=agent, day_of_week=_WEEK[0].weekday(),
                                     start_time=time(9, 0), end_time=time(17, 0), is_off=False)
        return agent

    def test_adherence_rows_query_count_flat_as_agent_count_grows(self):
        for i in range(2):
            self._make_roster_agent(i)
        url = reverse('adherence_rows_fragment') + f'?week_start={_WEEK[0].isoformat()}'
        self.client.get(url)  # warm up
        ctx = CaptureQueriesContext(connection)
        with ctx:
            self.client.get(url)
        small_count = len(ctx)

        for i in range(2, 12):
            self._make_roster_agent(i)
        ctx2 = CaptureQueriesContext(connection)
        with ctx2:
            self.client.get(url)
        big_count = len(ctx2)

        self.assertEqual(small_count, big_count,
                         'the primary-username resolver must stay one bulk query, not per-agent')

    def _make_admin_roster_agent(self, n):
        user = User.objects.create_user(f'qc_admin_{n}', password='x')
        agent = Agent.objects.create(
            user=user, role='admin', role_type='qa', agent_name=f'qc_admin_{n}',
            status='active', is_official_admin=True,
        )
        Five9Profile.objects.create(agent=agent, five9_username=f'qc_admin_{n}_primary',
                                    is_primary=True, billable=True)
        Five9Profile.objects.create(agent=agent, five9_username=f'qc_admin_{n}_extra',
                                    is_primary=False, billable=False)
        d = _WEEK[0]
        upload, _ = DailyUpload.objects.get_or_create(date=d, defaults={'filename': 'd.csv', 'row_count': 1})
        DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username=f'qc_admin_{n}_primary',
                                       login_seconds=8 * 3600, not_ready_seconds=0)
        DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username=f'qc_admin_{n}_extra',
                                       login_seconds=3 * 3600, not_ready_seconds=0)
        return agent

    def test_admin_adherence_query_count_flat_as_agent_count_grows(self):
        for i in range(2):
            self._make_admin_roster_agent(i)
        url = reverse('admin_adherence') + f'?week_start={_WEEK[0].isoformat()}'
        self.client.get(url)  # warm up
        ctx = CaptureQueriesContext(connection)
        with ctx:
            self.client.get(url)
        small_count = len(ctx)

        for i in range(2, 12):
            self._make_admin_roster_agent(i)
        ctx2 = CaptureQueriesContext(connection)
        with ctx2:
            self.client.get(url)
        big_count = len(ctx2)

        self.assertEqual(small_count, big_count,
                         'the primary-username resolver must stay one bulk query, not per-agent')


class BillableWeeklyDataPrimaryIndependenceTests(TestCase):
    """_get_billable_weekly_data must be completely indifferent to is_primary —
    only `billable` drives money. Table-driven over every is_primary
    permutation for a two-account fixture."""

    def setUp(self):
        self.settings = _settings()

    def _snapshot(self, agent, data):
        return {k: v for k, v in data[agent.pk].items() if k != 'agent'}

    def test_output_identical_across_every_is_primary_permutation(self):
        from finance.views import _get_billable_weekly_data
        for i, combo in enumerate([(True, False), (False, True), (True, True), (False, False)]):
            agent = _make_agent(f'perm_{combo[0]}_{combo[1]}')
            Five9Profile.objects.create(agent=agent, five9_username=f'perm_{i}_a',
                                        is_primary=combo[0], billable=True)
            Five9Profile.objects.create(agent=agent, five9_username=f'perm_{i}_b',
                                        is_primary=combo[1], billable=False)
            upload, _ = DailyUpload.objects.get_or_create(date=_WEEK[0], defaults={'filename': 'd.csv', 'row_count': 1})
            DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username=f'perm_{i}_a',
                                           login_seconds=8 * 3600, not_ready_seconds=0)
            DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username=f'perm_{i}_b',
                                           login_seconds=3 * 3600, not_ready_seconds=0)

            data = _get_billable_weekly_data([agent], _WEEK, self.settings)
            self.assertEqual(data[agent.pk]['actual_hrs'], Decimal('8'),
                             f'is_primary permutation {combo} changed billable money figures')


class RecalculateDisplayHoursCommandTests(TestCase):
    """Step 2: the one-time recalculation command. Simulates the production
    Mark Reyes week (Sep 14-20 2026) with stale actual_hours values written
    the OLD way (falling back to the non-primary account) — exactly the
    state Step 2 must repair. The command never touches DailyAgentHours,
    Coding, status, finance or nomina, and can never create a row."""

    def setUp(self):
        _settings()

    def _run(self, start, end, apply=False, agent_pk=None):
        out = io.StringIO()
        from django.core.management import call_command
        kwargs = {'stdout': out, 'start': start, 'end': end}
        if apply:
            kwargs['apply'] = True
        if agent_pk is not None:
            kwargs['agent_pk'] = agent_pk
        call_command('recalculate_display_hours', **kwargs)
        return out.getvalue()

    def _upload(self, d):
        return DailyUpload.objects.get_or_create(date=d, defaults={'filename': 'd.csv', 'row_count': 1})[0]

    def _hours(self, d, agent, username, login_seconds):
        DailyAgentHours.objects.create(
            upload=self._upload(d), agent=agent, five9_username=username,
            login_seconds=login_seconds, not_ready_seconds=0,
        )

    def _make_mark(self):
        agent = _make_agent('mark_s2')
        Five9Profile.objects.create(agent=agent, five9_username='marreyes_s2', is_primary=True, billable=True)
        Five9Profile.objects.create(agent=agent, five9_username='markreyes_s2', is_primary=False, billable=False)
        return agent

    def _seed_mark_week(self):
        """Mon/Fri/Sun of the production week, with stale actual_hours values
        written the OLD (fallback) way — the state Step 2 must repair."""
        agent = self._make_mark()
        mon, fri, sun = date(2026, 9, 14), date(2026, 9, 18), date(2026, 9, 20)

        # Mon: primary login 7:59, coded 3:00 -> old and new stored both 7.983333h (unchanged).
        self._hours(mon, agent, 'marreyes_s2', 7 * 3600 + 59 * 60)
        Coding.objects.create(agent=agent, date=mon, start_time=time(9, 0), end_time=time(12, 0),
                              is_admin_coding=False)
        AdherenceRecord.objects.create(agent=agent, date=mon, status='P', actual_hours=Decimal('7.983333'))

        # Fri: only the extra account has login (8:59), no coding -- the old
        # fallback stored the extra account's login; must correct to 0.
        self._hours(fri, agent, 'markreyes_s2', 8 * 3600 + 59 * 60)
        AdherenceRecord.objects.create(agent=agent, date=fri, status='P', actual_hours=Decimal('8.983333'))

        # Sun: only the extra account has login (3:56) plus a matching 3:56
        # coding -- old fallback stored the extra login; must correct to 0
        # (the coding still shows in the cell separately).
        self._hours(sun, agent, 'markreyes_s2', 3 * 3600 + 56 * 60)
        Coding.objects.create(agent=agent, date=sun, start_time=time(9, 0), end_time=time(12, 56),
                              is_admin_coding=False)
        AdherenceRecord.objects.create(agent=agent, date=sun, status='P', actual_hours=Decimal('3.933333'))

        return agent, mon, fri, sun

    def test_preview_writes_nothing(self):
        agent, mon, fri, sun = self._seed_mark_week()

        def _counts():
            return (
                AdherenceRecord.objects.count(),
                tuple(AdherenceRecord.objects.order_by('pk').values_list('actual_hours', 'updated_at')),
                DailyAgentHours.objects.count(), Coding.objects.count(),
                BillingSettings.objects.count(), BillingSettingsHistory.objects.count(),
                Agent.objects.count(),
            )

        before = _counts()
        self._run(mon, sun)
        self.assertEqual(_counts(), before)

    def test_preview_prints_only_changing_rows(self):
        agent, mon, fri, sun = self._seed_mark_week()
        # Widen the range by a day on each side so the header's own start/end
        # dates never coincide with Monday's date, keeping this check honest.
        out = self._run(mon - timedelta(days=1), sun + timedelta(days=1))
        rows_section = out.split('STORED BEFORE → AFTER', 1)[1]
        self.assertIn(fri.isoformat(), rows_section)
        self.assertIn(sun.isoformat(), rows_section)
        self.assertNotIn(mon.isoformat(), rows_section)   # unchanged -> not printed
        self.assertIn('2 agent-day(s) would change.', out)
        self.assertIn('Nothing was written.', out)

    def test_preview_reports_the_answer_key_cell_values(self):
        agent, mon, fri, sun = self._seed_mark_week()
        out = self._run(mon, sun)
        self.assertIn('8:59 → 0:00', out)
        self.assertIn('7:52 → 3:56', out)

    def test_apply_updates_only_the_changed_rows(self):
        agent, mon, fri, sun = self._seed_mark_week()
        mon_rec = AdherenceRecord.objects.get(agent=agent, date=mon)
        mon_before = (mon_rec.actual_hours, mon_rec.updated_at)

        self._run(mon, sun, apply=True)

        mon_rec.refresh_from_db()
        self.assertEqual((mon_rec.actual_hours, mon_rec.updated_at), mon_before)

        fri_rec = AdherenceRecord.objects.get(agent=agent, date=fri)
        sun_rec = AdherenceRecord.objects.get(agent=agent, date=sun)
        self.assertEqual(fri_rec.actual_hours, Decimal('0'))
        self.assertEqual(sun_rec.actual_hours, Decimal('0'))

    def test_second_apply_is_a_no_op(self):
        self._seed_mark_week()
        self._run(date(2026, 9, 14), date(2026, 9, 20), apply=True)
        after_first = tuple(AdherenceRecord.objects.order_by('pk').values_list('actual_hours', 'updated_at'))
        log_count_after_first = AuditLog.objects.count()

        out = self._run(date(2026, 9, 14), date(2026, 9, 20), apply=True)

        self.assertIn('No agent-day would change.', out)
        after_second = tuple(AdherenceRecord.objects.order_by('pk').values_list('actual_hours', 'updated_at'))
        self.assertEqual(after_first, after_second)
        self.assertEqual(AuditLog.objects.count(), log_count_after_first)

    def test_apply_writes_exactly_one_activity_log_entry(self):
        self._seed_mark_week()
        before = AuditLog.objects.count()
        self._run(date(2026, 9, 14), date(2026, 9, 20), apply=True)
        self.assertEqual(AuditLog.objects.count(), before + 1)
        entry = AuditLog.objects.latest('pk')
        self.assertEqual(entry.action, 'Recalculated adherence display hours')
        self.assertIn('2026-09-14', entry.detail)
        self.assertIn('2 agent-day(s) updated', entry.detail)
        self.assertIsNone(entry.user)

    def test_no_adherence_record_is_created(self):
        agent = self._make_mark()
        d = date(2026, 9, 14)
        # Scheduled that day, has a Daily Hours row, but no AdherenceRecord at all.
        ShiftTemplate.objects.create(agent=agent, day_of_week=d.weekday(),
                                     start_time=time(9, 0), end_time=time(17, 0), is_off=False)
        self._hours(d, agent, 'marreyes_s2', 8 * 3600)
        before = AdherenceRecord.objects.count()
        self._run(d, d, apply=True)
        self.assertEqual(AdherenceRecord.objects.count(), before)
        self.assertFalse(AdherenceRecord.objects.filter(agent=agent, date=d).exists())

    def test_official_admin_record_is_never_created_or_touched(self):
        user = User.objects.create_user('s2admin', password='x')
        admin = Agent.objects.create(
            user=user, role='admin', role_type='qa', agent_name='s2admin',
            status='active', is_official_admin=True,
        )
        Five9Profile.objects.create(agent=admin, five9_username='s2admin_primary', is_primary=True, billable=True)
        Five9Profile.objects.create(agent=admin, five9_username='s2admin_extra', is_primary=False, billable=False)
        d = date(2026, 9, 14)
        self._hours(d, admin, 's2admin_extra', 8 * 3600)  # stale-shaped data
        rec = AdherenceRecord.objects.create(agent=admin, date=d, status='P', actual_hours=Decimal('8'))

        self._run(d, d, apply=True)

        rec.refresh_from_db()
        self.assertEqual(rec.actual_hours, Decimal('8'))  # untouched

    def test_unmatched_daily_hours_row_stays_unmatched(self):
        agent = self._make_mark()
        d = date(2026, 9, 14)
        self._hours(d, agent, 'marreyes_s2', 8 * 3600)
        AdherenceRecord.objects.create(agent=agent, date=d, status='P', actual_hours=Decimal('5'))
        unmatched = DailyAgentHours.objects.create(
            upload=self._upload(d), agent=None, five9_username='ghost_user',
            login_seconds=3600, not_ready_seconds=0,
        )
        self._run(d, d, apply=True)
        unmatched.refresh_from_db()
        self.assertIsNone(unmatched.agent_id)

    def test_hand_entered_hours_on_a_day_with_no_upload_row_are_untouched(self):
        agent = self._make_mark()
        d = date(2026, 9, 14)
        AdherenceRecord.objects.create(agent=agent, date=d, status='P', actual_hours=Decimal('5'))
        self._run(d, d, apply=True)
        rec = AdherenceRecord.objects.get(agent=agent, date=d)
        self.assertEqual(rec.actual_hours, Decimal('5'))

    def test_out_of_range_dates_are_not_touched(self):
        agent, mon, fri, sun = self._seed_mark_week()
        before = fri - timedelta(days=1)
        AdherenceRecord.objects.create(agent=agent, date=before, status='P', actual_hours=Decimal('9'))
        self._hours(before, agent, 'markreyes_s2', 9 * 3600)

        self._run(fri, sun, apply=True)

        rec = AdherenceRecord.objects.get(agent=agent, date=before)
        self.assertEqual(rec.actual_hours, Decimal('9'))

    def test_value_that_rises_is_also_corrected(self):
        """The old fallback picked whichever row happened to match first; a
        duplicated/misattributed history can leave a stored value LOWER than
        the correct primary-only figure. The command must raise it too, not
        just zero things out."""
        agent = self._make_mark()
        d = date(2026, 9, 14)
        self._hours(d, agent, 'marreyes_s2', 8 * 3600)         # primary: 8h
        AdherenceRecord.objects.create(agent=agent, date=d, status='P', actual_hours=Decimal('3'))  # stale, too low
        self._run(d, d, apply=True)
        rec = AdherenceRecord.objects.get(agent=agent, date=d)
        self.assertEqual(rec.actual_hours, Decimal('8'))

    def test_apply_failure_leaves_nothing_written_and_no_log_entry(self):
        """--apply runs in ONE transaction, all-or-nothing: a failure anywhere
        in the write rolls back every row, not just the ones after it."""
        self._seed_mark_week()
        before = tuple(AdherenceRecord.objects.order_by('pk').values_list('actual_hours', 'updated_at'))
        log_count_before = AuditLog.objects.count()

        from unittest.mock import patch
        with patch('adherence.management.commands.recalculate_display_hours.log_action',
                   side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self._run(date(2026, 9, 14), date(2026, 9, 20), apply=True)

        after = tuple(AdherenceRecord.objects.order_by('pk').values_list('actual_hours', 'updated_at'))
        self.assertEqual(after, before)
        self.assertEqual(AuditLog.objects.count(), log_count_before)



class PrimaryPeriodDateAwarenessTests(TestCase):
    """Switching an agent's primary Five9 account must change which account counts
    from the switch date forward ONLY. Every earlier day keeps counting whichever
    account was primary at the time.

    Before the primary-period history, get_adherence_primary_resolver read today's
    is_primary flag for every date it was asked about, so a single switch silently
    recomputed the agent's whole adherence past — the reason CLAUDE.md and
    HANDOFF.md §8 both said not to switch anyone's primary account."""

    def _fake_request(self, post_data):
        return SimpleNamespace(POST=post_data)

    def _switch_primary_to(self, agent, keep, new):
        """Drive the one real write path (_save_five9_profiles) the way the Edit
        User form does: every row resubmitted, the radio now on `new`."""
        from scheduling.views import _save_five9_profiles
        post = {'five9_primary': str(new.pk)}
        for p in (keep, new):
            post[f'five9_{p.pk}_username'] = p.five9_username
            post[f'five9_{p.pk}_label'] = p.label
            post[f'five9_{p.pk}_billable'] = 'on' if p.billable else ''
        _save_five9_profiles(self._fake_request(post), agent)

    def test_switching_primary_leaves_earlier_days_on_the_old_account(self):
        from wfm.utils import get_adherence_primary_resolver
        agent = _make_agent('dateaware1')
        old = Five9Profile.objects.create(agent=agent, five9_username='da_old',
                                          is_primary=True, billable=True)
        new = Five9Profile.objects.create(agent=agent, five9_username='da_new',
                                          is_primary=False, billable=True)
        today = timezone.localdate()
        past = today - timedelta(days=10)

        self._switch_primary_to(agent, old, new)

        counts = get_adherence_primary_resolver([agent.pk])
        self.assertTrue(counts(agent.pk, 'da_old', past),
                        'the account that was primary on that past day must still count')
        self.assertFalse(counts(agent.pk, 'da_new', past),
                         'the new account must not retroactively count days before the switch')

    def test_switching_primary_takes_effect_from_today(self):
        from wfm.utils import get_adherence_primary_resolver
        agent = _make_agent('dateaware2')
        old = Five9Profile.objects.create(agent=agent, five9_username='db_old',
                                          is_primary=True, billable=True)
        new = Five9Profile.objects.create(agent=agent, five9_username='db_new',
                                          is_primary=False, billable=True)
        today = timezone.localdate()

        self._switch_primary_to(agent, old, new)

        counts = get_adherence_primary_resolver([agent.pk])
        self.assertTrue(counts(agent.pk, 'db_new', today))
        self.assertFalse(counts(agent.pk, 'db_old', today))

    def test_switch_recalculates_today_but_leaves_earlier_stored_hours_alone(self):
        """The switch must recompute exactly the days it affects. A day before the
        switch keeps the stored value it already had; the switch day is recomputed
        against the new account."""
        _settings(nr_ratio=Decimal('0.125'))
        agent = _make_agent('dateaware3')
        old = Five9Profile.objects.create(agent=agent, five9_username='dc_old',
                                          is_primary=True, billable=True)
        new = Five9Profile.objects.create(agent=agent, five9_username='dc_new',
                                          is_primary=False, billable=True)
        today = timezone.localdate()
        past = today - timedelta(days=10)

        for d in (past, today):
            upload, _ = DailyUpload.objects.get_or_create(
                date=d, defaults={'filename': 'x.csv', 'row_count': 2})
            DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username='dc_old',
                                           login_seconds=8 * 3600, not_ready_seconds=0)
            DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username='dc_new',
                                           login_seconds=5 * 3600, not_ready_seconds=0)
            AdherenceRecord.objects.create(agent=agent, date=d, status='P',
                                           actual_hours=Decimal('8'))

        self._switch_primary_to(agent, old, new)

        self.assertEqual(AdherenceRecord.objects.get(agent=agent, date=past).actual_hours,
                         Decimal('8'), 'a day before the switch must keep its stored hours')
        self.assertEqual(AdherenceRecord.objects.get(agent=agent, date=today).actual_hours,
                         Decimal('5'), 'the switch day must be recomputed on the new account')


class Five9PrimaryHistoryResolverTests(TestCase):
    """The resolution rule itself: for any date, the NEWEST entry covering it
    (highest pk) decides which account was primary. Entries are append-only, so
    every correction is a newer entry rather than an edit."""

    def setUp(self):
        self.agent = _make_agent('histagent')
        self.a = Five9Profile.objects.create(agent=self.agent, five9_username='hist_a',
                                             is_primary=True, billable=True)
        self.b = Five9Profile.objects.create(agent=self.agent, five9_username='hist_b',
                                             is_primary=False, billable=True)
        self.c = Five9Profile.objects.create(agent=self.agent, five9_username='hist_c',
                                             is_primary=False, billable=True)
        self.d1 = date(2026, 3, 1)

    def _period(self, profile, start, end, kind='switch'):
        return Five9PrimaryPeriod.objects.create(
            agent=self.agent, profile=profile, five9_username=profile.five9_username,
            start_date=start, end_date=end, kind=kind,
        )

    def _counts(self):
        from wfm.utils import get_adherence_primary_resolver
        return get_adherence_primary_resolver([self.agent.pk])

    def _winner_on(self, d):
        counts = self._counts()
        names = [n for n in ('hist_a', 'hist_b', 'hist_c') if counts(self.agent.pk, n, d)]
        self.assertLessEqual(len(names), 1, 'at most one account can be primary on a day')
        return names[0] if names else None

    def test_bootstrap_uses_todays_flag_when_there_is_no_history(self):
        for offset in (-400, -1, 0, 30):
            self.assertEqual(self._winner_on(self.d1 + timedelta(days=offset)), 'hist_a')

    def test_switch_splits_the_timeline_at_the_start_date(self):
        self._period(self.a, None, None, 'initial')
        self._period(self.b, self.d1, None)
        self.assertEqual(self._winner_on(self.d1 - timedelta(days=1)), 'hist_a')
        self.assertEqual(self._winner_on(self.d1), 'hist_b')
        self.assertEqual(self._winner_on(self.d1 + timedelta(days=90)), 'hist_b')

    def test_switch_then_switch_back(self):
        self._period(self.a, None, None, 'initial')
        self._period(self.b, self.d1, None)
        back = self.d1 + timedelta(days=10)
        self._period(self.a, back, None)
        self.assertEqual(self._winner_on(self.d1 - timedelta(days=1)), 'hist_a')
        self.assertEqual(self._winner_on(self.d1), 'hist_b')
        self.assertEqual(self._winner_on(back - timedelta(days=1)), 'hist_b')
        self.assertEqual(self._winner_on(back), 'hist_a')

    def test_past_period_inside_a_longer_primary(self):
        """After the past period's end date, the older entry applies again."""
        self._period(self.a, None, None, 'initial')
        start = self.d1
        end = self.d1 + timedelta(days=4)
        self._period(self.b, start, end, 'past_period')
        self.assertEqual(self._winner_on(start - timedelta(days=1)), 'hist_a')
        self.assertEqual(self._winner_on(start), 'hist_b')
        self.assertEqual(self._winner_on(end), 'hist_b')
        self.assertEqual(self._winner_on(end + timedelta(days=1)), 'hist_a')

    def test_past_period_entered_after_a_later_switch_already_exists(self):
        """A past period is newest, so it wins inside its own range — but it must
        not disturb the later switch that already governs today."""
        self._period(self.a, None, None, 'initial')
        switch_day = self.d1 + timedelta(days=20)
        self._period(self.b, switch_day, None)
        gap_start = self.d1 + timedelta(days=5)
        gap_end = self.d1 + timedelta(days=7)
        self._period(self.c, gap_start, gap_end, 'past_period')

        self.assertEqual(self._winner_on(gap_start - timedelta(days=1)), 'hist_a')
        self.assertEqual(self._winner_on(gap_start), 'hist_c')
        self.assertEqual(self._winner_on(gap_end), 'hist_c')
        self.assertEqual(self._winner_on(gap_end + timedelta(days=1)), 'hist_a')
        self.assertEqual(self._winner_on(switch_day), 'hist_b')
        self.assertEqual(self._winner_on(switch_day + timedelta(days=365)), 'hist_b')

    def test_always_supersedes_every_earlier_entry(self):
        self._period(self.a, None, None, 'initial')
        self._period(self.b, self.d1, None)
        self._period(self.c, self.d1 + timedelta(days=3), self.d1 + timedelta(days=6), 'past_period')
        self._period(self.a, None, None, 'always')
        for offset in (-500, -1, 0, 3, 6, 7, 400):
            self.assertEqual(self._winner_on(self.d1 + timedelta(days=offset)), 'hist_a',
                             'an "always" entry is newest and covers every day')

    def test_newest_wins_on_an_exact_overlap(self):
        self._period(self.a, self.d1, None, 'switch')
        self._period(self.b, self.d1, None, 'switch')
        self.assertEqual(self._winner_on(self.d1), 'hist_b')

    def test_a_date_no_entry_covers_has_no_primary(self):
        self._period(self.a, self.d1, None, 'switch')
        self.assertIsNone(self._winner_on(self.d1 - timedelta(days=1)))

    def test_rename_follows_the_entry(self):
        from scheduling.five9_primary import refresh_username_snapshots
        self._period(self.a, None, None, 'initial')
        self.a.five9_username = 'hist_a_renamed'
        self.a.save()
        refresh_username_snapshots(self.a)
        counts = self._counts()
        self.assertTrue(counts(self.agent.pk, 'hist_a_renamed', self.d1))
        self.assertFalse(counts(self.agent.pk, 'hist_a', self.d1))

    def test_deleted_account_entries_still_count_their_past_days(self):
        self._period(self.a, None, None, 'initial')
        self._period(self.b, self.d1, None, 'switch')
        self.a.delete()
        counts = self._counts()
        self.assertTrue(counts(self.agent.pk, 'hist_a', self.d1 - timedelta(days=1)),
                        'a deleted account must keep counting the days it was primary')
        self.assertTrue(counts(self.agent.pk, 'hist_b', self.d1))

    def test_resolver_stays_two_queries_regardless_of_agent_count(self):
        self._period(self.a, None, None, 'initial')
        from wfm.utils import get_adherence_primary_resolver
        others = []
        for i in range(12):
            other = _make_agent(f'histq_{i}')
            Five9Profile.objects.create(agent=other, five9_username=f'histq_{i}',
                                        is_primary=True, billable=True)
            others.append(other.pk)

        ctx = CaptureQueriesContext(connection)
        with ctx:
            counts = get_adherence_primary_resolver([self.agent.pk] + others)
            for pk in [self.agent.pk] + others:
                for offset in range(30):
                    counts(pk, 'hist_a', self.d1 + timedelta(days=offset))
        self.assertLessEqual(len(ctx), 2,
                             'the resolver must stay bulk — never a query per agent or per day')


class Five9PrimarySeedMigrationTests(TestCase):
    """The seed must reproduce today's resolver answers exactly. Runs the real
    migration function against the real models."""

    def _seed(self):
        import importlib
        from django.apps import apps as global_apps
        mod = importlib.import_module('scheduling.migrations.0055_five9primaryperiod')
        mod.seed_primary_periods(global_apps, None)

    def _answers(self, agents, usernames, dates):
        from wfm.utils import get_adherence_primary_resolver
        counts = get_adherence_primary_resolver([a.pk for a in agents])
        return {(a.pk, u, d): counts(a.pk, u, d)
                for a in agents for u in usernames for d in dates}

    def test_seed_reproduces_todays_answers_for_every_agent_and_date(self):
        with_primary = _make_agent('seed_with')
        Five9Profile.objects.create(agent=with_primary, five9_username='seed_p',
                                    is_primary=True, billable=True)
        Five9Profile.objects.create(agent=with_primary, five9_username='seed_x',
                                    is_primary=False, billable=False)
        without = _make_agent('seed_without')
        Five9Profile.objects.create(agent=without, five9_username='seed_none',
                                    is_primary=False, billable=True)
        no_accounts = _make_agent('seed_bare')

        agents = [with_primary, without, no_accounts]
        usernames = ['seed_p', 'seed_x', 'seed_none', 'nobody']
        dates = [date(2025, 1, 1), date(2026, 6, 15), timezone.localdate(),
                 timezone.localdate() + timedelta(days=30)]

        before = self._answers(agents, usernames, dates)
        Five9PrimaryPeriod.objects.all().delete()
        self._seed()
        after = self._answers(agents, usernames, dates)

        self.assertEqual(before, after)
        self.assertTrue(Five9PrimaryPeriod.objects.filter(agent=with_primary).exists())
        self.assertFalse(Five9PrimaryPeriod.objects.filter(agent=without).exists(),
                         'an agent with no primary account gets no entry')
        self.assertFalse(Five9PrimaryPeriod.objects.filter(agent=no_accounts).exists())

    def test_seed_writes_no_activity_log_entries(self):
        agent = _make_agent('seed_quiet')
        Five9Profile.objects.create(agent=agent, five9_username='seed_q', is_primary=True, billable=True)
        Five9PrimaryPeriod.objects.all().delete()
        before = AuditLog.objects.count()
        self._seed()
        self.assertEqual(AuditLog.objects.count(), before,
                         'the seed changes nothing observable, so it logs nothing')

    def test_two_accounts_marked_primary_collapse_to_the_lowest_id(self):
        """Stated behavior change, in this one shape only: both used to count.
        five9_account_setup_report found 0 agents like this in production."""
        agent = _make_agent('seed_dupe')
        first = Five9Profile.objects.create(agent=agent, five9_username='dupe_1',
                                            is_primary=True, billable=True)
        second = Five9Profile.objects.create(agent=agent, five9_username='dupe_2',
                                             is_primary=True, billable=True)
        Five9PrimaryPeriod.objects.all().delete()
        self._seed()

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(first.is_primary)
        self.assertFalse(second.is_primary, 'the flag must agree with the single seeded entry')
        self.assertEqual(Five9PrimaryPeriod.objects.filter(agent=agent).count(), 1)


class Five9PrimarySyncAndRecalcTests(TestCase):
    """is_primary always equals whatever the history says is primary today, and a
    saved entry recalculates exactly the days it covers — no more, no fewer."""

    def setUp(self):
        _settings(nr_ratio=Decimal('0.125'))
        self.today = timezone.localdate()

    def _fake_request(self, post_data):
        # A super admin, because removing a saved account is super-admin-only.
        # This class is about the is_primary sync and the recalculation; the
        # refusal below must fire for the missing-replacement reason, not for a
        # permission reason, or it would pass without testing anything.
        return SimpleNamespace(POST=post_data, has_finance_access=True)

    def _post_all(self, agent, primary=None, delete=()):
        post = {'five9_primary': str(primary.pk) if primary else ''}
        for p in agent.five9_profiles.all():
            if p.pk in delete:
                post[f'five9_{p.pk}_delete'] = 'on'
                continue
            post[f'five9_{p.pk}_username'] = p.five9_username
            post[f'five9_{p.pk}_label'] = p.label
            post[f'five9_{p.pk}_billable'] = 'on' if p.billable else ''
        return post

    def _save(self, agent, primary=None, delete=()):
        from scheduling.views import _save_five9_profiles
        _save_five9_profiles(self._fake_request(self._post_all(agent, primary, delete)), agent)

    def _two_account_agent(self, name):
        agent = _make_agent(name)
        a = Five9Profile.objects.create(agent=agent, five9_username=f'{name}_a',
                                        is_primary=True, billable=True)
        b = Five9Profile.objects.create(agent=agent, five9_username=f'{name}_b',
                                        is_primary=False, billable=True)
        return agent, a, b

    def test_is_primary_equals_todays_winner_after_a_switch(self):
        from scheduling.five9_primary import current_primary_profile
        agent, a, b = self._two_account_agent('sync1')
        self._save(agent, primary=b)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_primary)
        self.assertTrue(b.is_primary)
        self.assertEqual(current_primary_profile(agent).pk, b.pk)

    def test_is_primary_equals_todays_winner_after_an_always_entry(self):
        from scheduling.five9_primary import record_primary_period
        agent, a, b = self._two_account_agent('sync2')
        self._save(agent, primary=b)
        record_primary_period(agent, a, None, None, 'always', None)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertTrue(a.is_primary)
        self.assertFalse(b.is_primary)

    def test_a_past_period_never_changes_who_is_primary_today(self):
        from scheduling.five9_primary import record_primary_period
        agent, a, b = self._two_account_agent('sync3')
        self._save(agent, primary=b)
        record_primary_period(agent, a, self.today - timedelta(days=10),
                              self.today - timedelta(days=5), 'past_period', None)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_primary)
        self.assertTrue(b.is_primary, "today's primary is untouched by a past period")

    def test_removing_todays_primary_without_a_replacement_is_refused(self):
        from scheduling.views import _save_five9_profiles, Five9SaveError
        agent, a, b = self._two_account_agent('sync4')
        self._save(agent)   # seed the history
        with self.assertRaises(Five9SaveError):
            _save_five9_profiles(
                self._fake_request(self._post_all(agent, primary=None, delete={a.pk})), agent)

    def test_refused_removal_through_the_real_view_saves_nothing(self):
        """The refusal is raised inside agent_edit's transaction.atomic() and
        caught outside it, so the rollback is what guarantees nothing was saved --
        not just the account, but every other edit on that form too."""
        staff = User.objects.create_user('sync7staff', password='x')
        Agent.objects.create(user=staff, role='admin', role_type='supervisor',
                             agent_name='Sync7 Staff', status='active', is_super_admin=True)
        self.client.login(username='sync7staff', password='x')

        agent, a, b = self._two_account_agent('sync7')
        self._save(agent)
        agent.user.email = 'original@example.com'
        agent.user.save()

        payload = {
            'username': agent.user.username, 'email': 'changed@example.com',
            'legal_name': agent.agent_name, 'password': '', 'agent_name': agent.agent_name,
            'employee_id': '', 'role': 'agent', 'role_type': 'regular_agent',
            'status': 'active', 'employer': 'Infinity', 'billing_status': 'Not Billed',
            'phone_country_code': '+1', 'phone_number': '', 'teams_password': '',
            'hourly_rate': '62.50', 'billing_rate_usd': '', 'admin_bonus_mxn': '', 'notes': '',
            'five9_primary': '',
            f'five9_{a.pk}_delete': 'on',
            f'five9_{b.pk}_username': b.five9_username,
            f'five9_{b.pk}_label': '', f'five9_{b.pk}_billable': 'on',
        }
        resp = self.client.post(reverse('agent_edit', args=[agent.pk]), payload)

        self.assertEqual(resp.status_code, 200, 'refused, not redirected')
        self.assertTrue(Five9Profile.objects.filter(pk=a.pk).exists(),
                        'the primary account must not have been removed')
        agent.user.refresh_from_db()
        self.assertEqual(agent.user.email, 'original@example.com',
                         'the whole save rolled back, not only the Five9 part')

    def test_removing_todays_primary_with_a_replacement_is_allowed(self):
        agent, a, b = self._two_account_agent('sync5')
        self._save(agent)
        self._save(agent, primary=b, delete={a.pk})
        b.refresh_from_db()
        self.assertTrue(b.is_primary)
        self.assertFalse(Five9Profile.objects.filter(pk=a.pk).exists())

    def test_removing_a_non_primary_account_is_untouched(self):
        agent, a, b = self._two_account_agent('sync6')
        self._save(agent)
        self._save(agent, primary=a, delete={b.pk})
        a.refresh_from_db()
        self.assertTrue(a.is_primary)

    def _day(self, agent, d, a_secs, b_secs, name):
        upload, _ = DailyUpload.objects.get_or_create(
            date=d, defaults={'filename': 'x.csv', 'row_count': 2})
        DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username=f'{name}_a',
                                       login_seconds=a_secs, not_ready_seconds=0)
        DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username=f'{name}_b',
                                       login_seconds=b_secs, not_ready_seconds=0)
        return AdherenceRecord.objects.create(agent=agent, date=d, status='P',
                                              actual_hours=Decimal('8'))

    def test_recalculation_creates_no_rows(self):
        agent, a, b = self._two_account_agent('recalc1')
        d = self.today - timedelta(days=2)
        upload, _ = DailyUpload.objects.get_or_create(
            date=d, defaults={'filename': 'x.csv', 'row_count': 1})
        DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username='recalc1_b',
                                       login_seconds=5 * 3600, not_ready_seconds=0)
        before = AdherenceRecord.objects.count()
        self._save(agent, primary=b)
        self.assertEqual(AdherenceRecord.objects.count(), before,
                         'the recalculation is update-only and must never create a row')

    def test_recalculation_never_touches_an_official_admin(self):
        from scheduling.five9_primary import record_primary_period
        user = User.objects.create_user('recalc_admin', password='x')
        admin = Agent.objects.create(user=user, role='admin', role_type='qa',
                                     agent_name='recalc_admin', status='active',
                                     is_official_admin=True)
        a = Five9Profile.objects.create(agent=admin, five9_username='radm_a',
                                        is_primary=True, billable=True)
        b = Five9Profile.objects.create(agent=admin, five9_username='radm_b',
                                        is_primary=False, billable=True)
        d = self.today - timedelta(days=2)
        rec = self._day(admin, d, 8 * 3600, 5 * 3600, 'radm')
        record_primary_period(admin, b, d, None, 'switch', None)
        rec.refresh_from_db()
        self.assertEqual(rec.actual_hours, Decimal('8'),
                         'an Official Admin\'s stored hours are never recalculated')

    def test_recalculation_covers_only_the_days_the_entry_covers(self):
        from scheduling.five9_primary import record_primary_period
        agent, a, b = self._two_account_agent('recalc3')
        before_range = self.today - timedelta(days=12)
        inside_one = self.today - timedelta(days=9)
        inside_two = self.today - timedelta(days=7)
        after_range = self.today - timedelta(days=3)
        recs = {d: self._day(agent, d, 8 * 3600, 5 * 3600, 'recalc3')
                for d in (before_range, inside_one, inside_two, after_range)}

        record_primary_period(agent, b, inside_one, inside_two, 'past_period', None)

        for d, rec in recs.items():
            rec.refresh_from_db()
        self.assertEqual(recs[before_range].actual_hours, Decimal('8'))
        self.assertEqual(recs[inside_one].actual_hours, Decimal('5'))
        self.assertEqual(recs[inside_two].actual_hours, Decimal('5'))
        self.assertEqual(recs[after_range].actual_hours, Decimal('8'))

    def test_every_entry_writes_exactly_one_activity_log_entry(self):
        agent, a, b = self._two_account_agent('log1')
        self._save(agent)                       # backfill only — silent by design
        before = AuditLog.objects.count()
        self._save(agent, primary=b)
        self.assertEqual(AuditLog.objects.count(), before + 1)
        entry = AuditLog.objects.latest('id')
        self.assertEqual(entry.action, 'Changed Five9 primary account')
        self.assertIn('log1_b', entry.detail)
        self.assertIn('Switched', entry.detail)
        self.assertEqual(entry.agent_id, agent.pk)

    def test_backfilling_an_existing_primary_logs_nothing(self):
        agent, a, b = self._two_account_agent('log2')
        before = AuditLog.objects.count()
        self._save(agent, primary=a)
        self.assertEqual(AuditLog.objects.count(), before,
                         'recording what is_primary already said is not a change')


class Five9PrimaryTimelineTests(TestCase):
    """The user detail page shows one muted line, and only when more than one
    account has actually been primary for that agent."""

    def setUp(self):
        self.agent = _make_agent('tlagent')
        self.a = Five9Profile.objects.create(agent=self.agent, five9_username='tl_a',
                                             is_primary=True, billable=True)
        self.b = Five9Profile.objects.create(agent=self.agent, five9_username='tl_b',
                                             is_primary=False, billable=True)

    def _period(self, profile, start, end, kind):
        Five9PrimaryPeriod.objects.create(
            agent=self.agent, profile=profile, five9_username=profile.five9_username,
            start_date=start, end_date=end, kind=kind)

    def test_no_line_when_one_account_has_always_been_primary(self):
        from scheduling.five9_primary import format_primary_timeline
        self._period(self.a, None, None, 'initial')
        self.assertEqual(format_primary_timeline(self.agent), '')

    def test_no_line_when_there_is_no_history_at_all(self):
        from scheduling.five9_primary import format_primary_timeline
        self.assertEqual(format_primary_timeline(self.agent), '')

    def test_line_appears_once_a_second_account_has_been_primary(self):
        from scheduling.five9_primary import format_primary_timeline
        self._period(self.a, None, None, 'initial')
        self._period(self.b, date(2026, 10, 5), None, 'switch')
        line = format_primary_timeline(self.agent)
        self.assertEqual(line, 'Primary: tl_a until Oct 4, 2026 · tl_b from Oct 5, 2026 (current)')

    def test_line_reads_a_past_period_as_a_closed_range(self):
        from scheduling.five9_primary import format_primary_timeline
        self._period(self.a, None, None, 'initial')
        self._period(self.b, date(2026, 10, 5), date(2026, 10, 7), 'past_period')
        line = format_primary_timeline(self.agent)
        self.assertEqual(
            line,
            'Primary: tl_a until Oct 4, 2026 · tl_b Oct 5, 2026 – Oct 7, 2026 '
            '· tl_a from Oct 8, 2026 (current)')

    def test_an_always_entry_collapses_the_line_back_to_one_account(self):
        from scheduling.five9_primary import format_primary_timeline
        self._period(self.a, None, None, 'initial')
        self._period(self.b, date(2026, 10, 5), None, 'switch')
        self._period(self.a, None, None, 'always')
        self.assertEqual(format_primary_timeline(self.agent), '',
                         'once "always" supersedes everything, only one account was ever primary')

    def test_detail_page_renders_the_line_only_when_there_is_one(self):
        staff = User.objects.create_user('tlstaff', password='x')
        Agent.objects.create(user=staff, role='admin', role_type='supervisor',
                             agent_name='TL Staff', status='active')
        self.client.login(username='tlstaff', password='x')

        self._period(self.a, None, None, 'initial')
        html = self.client.get(reverse('agent_detail', args=[self.agent.pk])).content.decode()
        self.assertNotIn('Primary: ', html)

        self._period(self.b, date(2026, 10, 5), None, 'switch')
        html = self.client.get(reverse('agent_detail', args=[self.agent.pk])).content.decode()
        self.assertIn('Primary: tl_a until Oct 4, 2026', html)


class Five9PrimaryMoneyIsUntouchedTests(TestCase):
    """Pinned: no kind of primary-account history entry can move a money figure.
    Billing and payroll select usernames on `billable`, never on the history."""

    def setUp(self):
        self.settings = _settings()
        self.agent = _make_agent('moneyagent')
        self.a = Five9Profile.objects.create(agent=self.agent, five9_username='money_a',
                                             is_primary=True, billable=True)
        self.b = Five9Profile.objects.create(agent=self.agent, five9_username='money_b',
                                             is_primary=False, billable=True)
        upload, _ = DailyUpload.objects.get_or_create(
            date=_WEEK[0], defaults={'filename': 'd.csv', 'row_count': 2})
        DailyAgentHours.objects.create(upload=upload, agent=self.agent, five9_username='money_a',
                                       login_seconds=8 * 3600, not_ready_seconds=0)
        DailyAgentHours.objects.create(upload=upload, agent=self.agent, five9_username='money_b',
                                       login_seconds=3 * 3600, not_ready_seconds=0)

    def _money(self):
        from finance.views import _get_billable_weekly_data
        data = _get_billable_weekly_data([self.agent], _WEEK, self.settings)
        return {k: v for k, v in data[self.agent.pk].items() if k != 'agent'}

    def test_output_identical_after_every_kind_of_history_entry(self):
        baseline = self._money()
        entries = [
            (self.a, None, None, 'initial'),
            (self.b, _WEEK[0], None, 'switch'),
            (self.a, _WEEK[1], _WEEK[3], 'past_period'),
            (self.b, None, None, 'always'),
        ]
        for profile, start, end, kind in entries:
            Five9PrimaryPeriod.objects.create(
                agent=self.agent, profile=profile, five9_username=profile.five9_username,
                start_date=start, end_date=end, kind=kind)
            self.assertEqual(self._money(), baseline,
                             f'a "{kind}" history entry moved a money figure')


class Five9PrimaryQueryCountWithHistoryTests(TestCase):
    """Query counts stay flat with history present — the resolver's cost does not
    grow with the roster, the number of days, or the number of entries."""

    def setUp(self):
        _settings()
        staff = User.objects.create_user('qhstaff', password='x')
        Agent.objects.create(user=staff, role='admin', role_type='supervisor',
                             agent_name='QH Staff', status='active')
        self.client.login(username='qhstaff', password='x')

    def _agent_with_history(self, n):
        agent = _make_agent(f'qh_{n}')
        a = Five9Profile.objects.create(agent=agent, five9_username=f'qh_{n}_a',
                                        is_primary=True, billable=True)
        b = Five9Profile.objects.create(agent=agent, five9_username=f'qh_{n}_b',
                                        is_primary=False, billable=False)
        for profile, start, kind in ((a, None, 'initial'), (b, _WEEK[2], 'switch'),
                                     (a, _WEEK[4], 'switch')):
            Five9PrimaryPeriod.objects.create(
                agent=agent, profile=profile, five9_username=profile.five9_username,
                start_date=start, end_date=None, kind=kind)
        upload, _ = DailyUpload.objects.get_or_create(
            date=_WEEK[0], defaults={'filename': 'd.csv', 'row_count': 1})
        DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username=f'qh_{n}_a',
                                       login_seconds=8 * 3600, not_ready_seconds=0)
        DailyAgentHours.objects.create(upload=upload, agent=agent, five9_username=f'qh_{n}_b',
                                       login_seconds=3 * 3600, not_ready_seconds=0)
        ShiftTemplate.objects.create(agent=agent, day_of_week=_WEEK[0].weekday(),
                                     start_time=time(9, 0), end_time=time(17, 0), is_off=False)
        return agent

    def _count(self, url):
        self.client.get(url)  # warm up
        ctx = CaptureQueriesContext(connection)
        with ctx:
            self.client.get(url)
        return len(ctx)

    def _assert_flat(self, url):
        for i in range(2):
            self._agent_with_history(i)
        small = self._count(url)
        for i in range(2, 12):
            self._agent_with_history(i)
        self.assertEqual(self._count(url), small,
                         'the primary-period resolver must stay bulk with history present')

    def test_daily_hours_stays_flat(self):
        self._assert_flat(reverse('daily_hours') + f'?week_start={_WEEK[0].isoformat()}')

    def test_adherence_rows_stays_flat(self):
        self._assert_flat(reverse('adherence_rows_fragment') + f'?week_start={_WEEK[0].isoformat()}')


class Five9AccountPermissionTests(TestCase):
    """On a SAVED Five9 account, deleting it, changing its username and changing
    its Billable box are super-admin-only. Everything else a user-editor could do
    before, they can still do: add a new account (username and Billable included),
    change label, password and role type, and switch the primary starting today.

    Enforced on the server on every save, so hiding the controls is not what stops
    a crafted request — a refusal rolls the whole save back with nothing written."""

    def setUp(self):
        _settings()
        self.agent = _make_agent('permtarget')
        self.acct = Five9Profile.objects.create(agent=self.agent, five9_username='perm_main',
                                                label='Main', five9_password='pw',
                                                is_primary=True, billable=True)
        self.other = Five9Profile.objects.create(agent=self.agent, five9_username='perm_other',
                                                 label='Other', is_primary=False, billable=False)

    def _request(self, post, super_admin):
        user = User.objects.create_user(f'permuser_{super_admin}_{len(post)}', password='x')
        return SimpleNamespace(POST=post, user=user, has_finance_access=super_admin)

    def _base_post(self, **overrides):
        post = {
            'five9_primary': str(self.acct.pk),
            f'five9_{self.acct.pk}_username': self.acct.five9_username,
            f'five9_{self.acct.pk}_label': self.acct.label,
            f'five9_{self.acct.pk}_password': self.acct.five9_password,
            f'five9_{self.acct.pk}_billable': 'on',
            f'five9_{self.other.pk}_username': self.other.five9_username,
            f'five9_{self.other.pk}_label': self.other.label,
        }
        post.update(overrides)
        return post

    def _save(self, post, super_admin):
        from scheduling.views import _save_five9_profiles
        _save_five9_profiles(self._request(post, super_admin), self.agent)

    # ── refused for a supervisor ──────────────────────────────────────────────

    def test_supervisor_cannot_delete_a_saved_account(self):
        from scheduling.views import Five9SaveError
        post = self._base_post(**{f'five9_{self.other.pk}_delete': 'on'})
        with self.assertRaises(Five9SaveError):
            self._save(post, super_admin=False)
        self.assertTrue(Five9Profile.objects.filter(pk=self.other.pk).exists())

    def test_supervisor_cannot_rename_a_saved_account(self):
        from scheduling.views import Five9SaveError
        post = self._base_post(**{f'five9_{self.acct.pk}_username': 'perm_hijacked'})
        with self.assertRaises(Five9SaveError):
            self._save(post, super_admin=False)
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.five9_username, 'perm_main')

    def test_supervisor_cannot_turn_billable_on(self):
        from scheduling.views import Five9SaveError
        post = self._base_post(**{f'five9_{self.other.pk}_billable': 'on'})
        with self.assertRaises(Five9SaveError):
            self._save(post, super_admin=False)
        self.other.refresh_from_db()
        self.assertFalse(self.other.billable)

    def test_supervisor_cannot_turn_billable_off_by_omitting_the_checkbox(self):
        """An omitted checkbox is indistinguishable from a field that was never
        rendered, so it is treated as unchanged rather than refused. The account
        stays billable either way — the hole closes without a spurious error."""
        post = self._base_post()
        post.pop(f'five9_{self.acct.pk}_billable')
        self._save(post, super_admin=False)
        self.acct.refresh_from_db()
        self.assertTrue(self.acct.billable)

    # ── still allowed for a supervisor ────────────────────────────────────────

    def test_supervisor_can_still_edit_label_password_and_role_type(self):
        post = self._base_post(**{
            f'five9_{self.acct.pk}_label': 'Renamed label',
            f'five9_{self.acct.pk}_password': 'newpw',
            f'five9_{self.acct.pk}_role_type': 'regular_agent',
        })
        self._save(post, super_admin=False)
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.label, 'Renamed label')
        self.assertEqual(self.acct.five9_password, 'newpw')
        self.assertEqual(self.acct.role_type, 'regular_agent')
        self.assertEqual(self.acct.five9_username, 'perm_main')
        self.assertTrue(self.acct.billable)

    def test_supervisor_can_still_add_a_new_account_with_username_and_billable(self):
        post = self._base_post(**{
            'new_five9_0_username': 'perm_added',
            'new_five9_0_label': 'Added',
            'new_five9_0_billable': 'on',
        })
        self._save(post, super_admin=False)
        added = Five9Profile.objects.get(agent=self.agent, five9_username='perm_added')
        self.assertTrue(added.billable)

    def test_supervisor_can_still_add_a_non_billable_new_account(self):
        post = self._base_post(**{
            'new_five9_0_username': 'perm_added_nb',
            'new_five9_0_label': 'Added',
        })
        self._save(post, super_admin=False)
        added = Five9Profile.objects.get(agent=self.agent, five9_username='perm_added_nb')
        self.assertFalse(added.billable)

    def test_supervisor_can_still_switch_the_primary_starting_today(self):
        post = self._base_post(**{'five9_primary': str(self.other.pk)})
        self._save(post, super_admin=False)
        self.acct.refresh_from_db()
        self.other.refresh_from_db()
        self.assertFalse(self.acct.is_primary)
        self.assertTrue(self.other.is_primary)
        entry = Five9PrimaryPeriod.objects.filter(agent=self.agent).latest('id')
        self.assertEqual(entry.kind, 'switch')
        self.assertEqual(entry.start_date, timezone.localdate())

    # ── allowed for a super admin ─────────────────────────────────────────────

    def test_super_admin_can_delete_a_saved_account(self):
        post = self._base_post(**{f'five9_{self.other.pk}_delete': 'on'})
        self._save(post, super_admin=True)
        self.assertFalse(Five9Profile.objects.filter(pk=self.other.pk).exists())

    def test_super_admin_can_rename_a_saved_account(self):
        post = self._base_post(**{f'five9_{self.acct.pk}_username': 'perm_renamed'})
        self._save(post, super_admin=True)
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.five9_username, 'perm_renamed')

    def test_super_admin_can_change_billable(self):
        post = self._base_post(**{f'five9_{self.other.pk}_billable': 'on'})
        self._save(post, super_admin=True)
        self.other.refresh_from_db()
        self.assertTrue(self.other.billable)

    def test_a_django_superuser_counts_as_a_super_admin(self):
        from scheduling.views import _save_five9_profiles
        user = User.objects.create_superuser('permroot', 'r@example.com', 'x')
        post = self._base_post(**{f'five9_{self.acct.pk}_username': 'perm_root_renamed'})
        _save_five9_profiles(SimpleNamespace(POST=post, user=user), self.agent)
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.five9_username, 'perm_root_renamed')

    def test_a_missing_finance_attribute_denies_rather_than_raises(self):
        """AgentAccessMiddleware swallows its own exceptions, so the attribute can
        be missing entirely. Fail closed, the same way skill_list does."""
        from scheduling.views import _save_five9_profiles, Five9SaveError
        user = User.objects.create_user('permbare', password='x')
        post = self._base_post(**{f'five9_{self.acct.pk}_username': 'perm_bare_renamed'})
        with self.assertRaises(Five9SaveError):
            _save_five9_profiles(SimpleNamespace(POST=post, user=user), self.agent)


class Five9AccountPermissionViewTests(TestCase):
    """The same three locks through the real Edit User view: a crafted POST is
    refused with nothing saved, and the screen itself offers a supervisor no
    control it is not allowed to use."""

    def setUp(self):
        _settings()
        self.target = _make_agent('vperm_target')
        self.target.role_type = 'regular_agent'
        self.target.save()
        self.acct = Five9Profile.objects.create(agent=self.target, five9_username='vperm_main',
                                                label='Main', is_primary=True, billable=True)
        self.extra = Five9Profile.objects.create(agent=self.target, five9_username='vperm_extra',
                                                 label='Extra', is_primary=False, billable=False)

    def _login(self, name, super_admin):
        user = User.objects.create_user(name, password='x')
        Agent.objects.create(user=user, role='admin', role_type='supervisor',
                             agent_name=name, status='active', is_super_admin=super_admin)
        self.client.login(username=name, password='x')

    def _payload(self, **overrides):
        post = {
            'username': self.target.user.username, 'email': 'vperm@example.com',
            'legal_name': self.target.agent_name, 'password': '',
            'agent_name': self.target.agent_name, 'employee_id': '',
            'role': 'agent', 'role_type': 'regular_agent', 'status': 'active',
            'employer': 'Infinity', 'billing_status': 'Not Billed',
            'phone_country_code': '+1', 'phone_number': '', 'teams_password': '',
            'hourly_rate': '62.50', 'billing_rate_usd': '', 'admin_bonus_mxn': '', 'notes': '',
            'five9_primary': str(self.acct.pk),
            f'five9_{self.acct.pk}_username': self.acct.five9_username,
            f'five9_{self.acct.pk}_label': self.acct.label,
            f'five9_{self.acct.pk}_billable': 'on',
            f'five9_{self.extra.pk}_username': self.extra.five9_username,
            f'five9_{self.extra.pk}_label': self.extra.label,
        }
        post.update(overrides)
        return post

    def test_crafted_delete_is_refused_and_saves_nothing(self):
        self._login('vperm_sup1', super_admin=False)
        self.target.user.email = 'untouched@example.com'
        self.target.user.save()
        payload = self._payload(email='changed@example.com',
                                **{f'five9_{self.extra.pk}_delete': 'on'})
        resp = self.client.post(reverse('agent_edit', args=[self.target.pk]), payload)

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Five9Profile.objects.filter(pk=self.extra.pk).exists())
        self.target.user.refresh_from_db()
        self.assertEqual(self.target.user.email, 'untouched@example.com',
                         'the whole save rolled back, not only the Five9 part')

    def test_crafted_rename_is_refused_and_saves_nothing(self):
        self._login('vperm_sup2', super_admin=False)
        payload = self._payload(**{f'five9_{self.acct.pk}_username': 'vperm_hijacked'})
        resp = self.client.post(reverse('agent_edit', args=[self.target.pk]), payload)

        self.assertEqual(resp.status_code, 200)
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.five9_username, 'vperm_main')

    def test_crafted_billable_change_is_refused_and_saves_nothing(self):
        self._login('vperm_sup3', super_admin=False)
        payload = self._payload(**{f'five9_{self.extra.pk}_billable': 'on'})
        resp = self.client.post(reverse('agent_edit', args=[self.target.pk]), payload)

        self.assertEqual(resp.status_code, 200)
        self.extra.refresh_from_db()
        self.assertFalse(self.extra.billable)

    def test_a_super_admin_posting_the_same_change_succeeds(self):
        self._login('vperm_root', super_admin=True)
        payload = self._payload(**{f'five9_{self.acct.pk}_username': 'vperm_renamed'})
        resp = self.client.post(reverse('agent_edit', args=[self.target.pk]), payload)

        self.assertRedirects(resp, reverse('agent_detail', args=[self.target.pk]))
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.five9_username, 'vperm_renamed')

    # ── what each role is offered on screen ───────────────────────────────────

    def _form_html(self):
        return self.client.get(reverse('agent_edit', args=[self.target.pk])).content.decode()

    def test_supervisor_sees_no_editable_username_no_billable_box_and_no_remove(self):
        self._login('vperm_sup4', super_admin=False)
        html = self._form_html()
        self.assertNotIn(f'type="text" name="five9_{self.acct.pk}_username"', html)
        self.assertNotIn(f'name="five9_{self.acct.pk}_billable"', html)
        self.assertNotIn(f"removeFive9('{self.acct.pk}')", html)
        self.assertIn('vperm_main', html, 'the username is still shown, as plain text')

    def test_supervisor_still_gets_the_add_account_button_and_the_primary_radio(self):
        self._login('vperm_sup5', super_admin=False)
        html = self._form_html()
        self.assertIn('addFive9Row()', html)
        self.assertIn(f'name="five9_primary" value="{self.acct.pk}"', html)
        self.assertIn(f'name="five9_{self.acct.pk}_label"', html)

    def test_super_admin_sees_every_control(self):
        self._login('vperm_root2', super_admin=True)
        html = self._form_html()
        self.assertIn(f'type="text" name="five9_{self.acct.pk}_username"', html)
        self.assertIn(f'name="five9_{self.acct.pk}_billable"', html)
        self.assertIn(f"removeFive9('{self.acct.pk}')", html)


class Five9BackDatedSwitchTests(TestCase):
    """Super admins can date a primary switch in the past, or declare that an
    account was always the primary to fix a setup mistake. Both are refused for
    anyone else, and no date may be in the future."""

    def setUp(self):
        _settings()
        self.agent = _make_agent('backdate')
        self.a = Five9Profile.objects.create(agent=self.agent, five9_username='bd_a',
                                             is_primary=True, billable=True)
        self.b = Five9Profile.objects.create(agent=self.agent, five9_username='bd_b',
                                             is_primary=False, billable=True)
        self.today = timezone.localdate()

    def _post(self, **overrides):
        post = {
            'five9_primary': str(self.b.pk),
            f'five9_{self.a.pk}_username': self.a.five9_username,
            f'five9_{self.a.pk}_label': '', f'five9_{self.a.pk}_billable': 'on',
            f'five9_{self.b.pk}_username': self.b.five9_username,
            f'five9_{self.b.pk}_label': '', f'five9_{self.b.pk}_billable': 'on',
        }
        post.update(overrides)
        return post

    def _save(self, post, super_admin):
        from scheduling.views import _save_five9_profiles
        user = User.objects.create_user(f'bd_{super_admin}_{len(post)}', password='x')
        _save_five9_profiles(
            SimpleNamespace(POST=post, user=user, has_finance_access=super_admin), self.agent)

    def _latest(self):
        return Five9PrimaryPeriod.objects.filter(agent=self.agent).latest('id')

    def test_super_admin_can_back_date_the_switch(self):
        past = self.today - timedelta(days=6)
        self._save(self._post(five9_primary_start=past.isoformat()), super_admin=True)
        entry = self._latest()
        self.assertEqual(entry.kind, 'switch')
        self.assertEqual(entry.start_date, past)
        self.assertIsNone(entry.end_date)

    def test_super_admin_always_records_a_from_the_beginning_entry(self):
        self._save(self._post(five9_primary_always='on'), super_admin=True)
        entry = self._latest()
        self.assertEqual(entry.kind, 'always')
        self.assertIsNone(entry.start_date)
        self.assertIsNone(entry.end_date)

    def test_always_wins_over_a_start_date_posted_alongside_it(self):
        past = self.today - timedelta(days=6)
        self._save(self._post(five9_primary_always='on', five9_primary_start=past.isoformat()),
                   super_admin=True)
        entry = self._latest()
        self.assertEqual(entry.kind, 'always')
        self.assertIsNone(entry.start_date)

    def test_a_future_start_date_is_refused(self):
        from scheduling.views import Five9SaveError
        future = (self.today + timedelta(days=1)).isoformat()
        with self.assertRaises(Five9SaveError) as ctx:
            self._save(self._post(five9_primary_start=future), super_admin=True)
        self.assertIn("future", str(ctx.exception).lower())
        self.assertFalse(Five9PrimaryPeriod.objects.filter(agent=self.agent, kind='switch').exists())

    def test_an_unparseable_start_date_is_refused(self):
        from scheduling.views import Five9SaveError
        with self.assertRaises(Five9SaveError):
            self._save(self._post(five9_primary_start='not-a-date'), super_admin=True)

    def test_a_start_date_of_today_is_the_ordinary_switch(self):
        self._save(self._post(five9_primary_start=self.today.isoformat()), super_admin=True)
        entry = self._latest()
        self.assertEqual(entry.kind, 'switch')
        self.assertEqual(entry.start_date, self.today)

    def test_supervisor_cannot_back_date(self):
        from scheduling.views import Five9SaveError
        past = (self.today - timedelta(days=6)).isoformat()
        with self.assertRaises(Five9SaveError):
            self._save(self._post(five9_primary_start=past), super_admin=False)
        self.b.refresh_from_db()
        self.assertFalse(self.b.is_primary, 'nothing was saved')

    def test_supervisor_cannot_use_always(self):
        from scheduling.views import Five9SaveError
        with self.assertRaises(Five9SaveError):
            self._save(self._post(five9_primary_always='on'), super_admin=False)

    def test_supervisor_posting_todays_date_is_allowed(self):
        """The reveal line is only informational for a supervisor, but a form that
        did post today's date must not be refused for it."""
        self._save(self._post(five9_primary_start=self.today.isoformat()), super_admin=False)
        entry = self._latest()
        self.assertEqual(entry.start_date, self.today)


class Five9PastPeriodEndpointTests(TestCase):
    """The 'Add a past primary period' endpoint: four server-side gates before
    anything is written, and plain validation messages."""

    def setUp(self):
        _settings()
        self.agent = _make_agent('pp_target')
        self.a = Five9Profile.objects.create(agent=self.agent, five9_username='pp_a',
                                             is_primary=True, billable=True)
        self.b = Five9Profile.objects.create(agent=self.agent, five9_username='pp_b',
                                             is_primary=False, billable=True)
        self.stranger = _make_agent('pp_stranger')
        self.stranger_acct = Five9Profile.objects.create(
            agent=self.stranger, five9_username='pp_stranger_acct', is_primary=True, billable=True)
        self.today = timezone.localdate()

    def _login(self, name, super_admin=True, portal=False):
        user = User.objects.create_user(name, password='x')
        Agent.objects.create(
            user=user, role='agent' if portal else 'admin',
            role_type='regular_agent' if portal else 'supervisor',
            agent_name=name, status='active', is_super_admin=super_admin)
        self.client.login(username=name, password='x')

    def _url(self):
        return reverse('five9_primary_period_add', args=[self.agent.pk])

    def _post(self, profile=None, frm=None, to=None):
        return self.client.post(self._url(), {
            'profile': str((profile or self.b).pk),
            'from': (frm or (self.today - timedelta(days=10))).isoformat(),
            'to': (to or (self.today - timedelta(days=8))).isoformat(),
        })

    def test_super_admin_can_add_a_past_period(self):
        self._login('pp_root')
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])
        entry = Five9PrimaryPeriod.objects.filter(agent=self.agent).latest('id')
        self.assertEqual(entry.kind, 'past_period')
        self.assertEqual(entry.start_date, self.today - timedelta(days=10))
        self.assertEqual(entry.end_date, self.today - timedelta(days=8))

    def test_supervisor_is_refused(self):
        self._login('pp_sup', super_admin=False)
        before = Five9PrimaryPeriod.objects.count()
        resp = self._post()
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.json()['ok'])
        self.assertEqual(Five9PrimaryPeriod.objects.count(), before)

    def test_a_portal_user_never_reaches_the_endpoint(self):
        self._login('pp_portal', super_admin=False, portal=True)
        before = Five9PrimaryPeriod.objects.count()
        resp = self._post()
        self.assertEqual(resp.status_code, 302, 'portal users are redirected away')
        self.assertEqual(Five9PrimaryPeriod.objects.count(), before)

    def test_an_anonymous_request_is_refused(self):
        before = Five9PrimaryPeriod.objects.count()
        resp = self._post()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Five9PrimaryPeriod.objects.count(), before)

    def test_another_agents_account_is_refused(self):
        """A crafted request naming an account that belongs to someone else."""
        self._login('pp_root2')
        before = Five9PrimaryPeriod.objects.count()
        resp = self._post(profile=self.stranger_acct)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()['ok'])
        self.assertEqual(Five9PrimaryPeriod.objects.count(), before)
        self.assertEqual(
            Five9PrimaryPeriod.objects.filter(profile=self.stranger_acct).count(), 0)

    def test_future_dates_are_refused(self):
        self._login('pp_root3')
        resp = self._post(frm=self.today + timedelta(days=1), to=self.today + timedelta(days=2))
        self.assertEqual(resp.json()['error'], "Dates can't be in the future.")

    def test_a_to_date_of_today_is_refused(self):
        self._login('pp_root4')
        resp = self._post(frm=self.today - timedelta(days=2), to=self.today)
        self.assertEqual(
            resp.json()['error'],
            'To must be before today. To change today’s primary, use the Primary button.')

    def test_from_after_to_is_refused(self):
        self._login('pp_root5')
        resp = self._post(frm=self.today - timedelta(days=2), to=self.today - timedelta(days=5))
        self.assertEqual(resp.json()['error'], 'From must be on or before To.')

    def test_missing_input_is_refused(self):
        self._login('pp_root6')
        resp = self.client.post(self._url(), {'profile': '', 'from': '', 'to': ''})
        self.assertFalse(resp.json()['ok'])
        self.assertEqual(Five9PrimaryPeriod.objects.filter(kind='past_period').count(), 0)

    def test_get_is_not_allowed(self):
        self._login('pp_root7')
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 405)

    def test_a_past_period_writes_one_activity_log_entry(self):
        self._login('pp_root8')
        before = AuditLog.objects.count()
        self._post()
        self.assertEqual(AuditLog.objects.count(), before + 1)
        entry = AuditLog.objects.latest('id')
        self.assertEqual(entry.action, 'Changed Five9 primary account')
        self.assertIn('Past period', entry.detail)

    def test_a_past_period_does_not_change_todays_primary(self):
        self._login('pp_root9')
        self._post()
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertTrue(self.a.is_primary)
        self.assertFalse(self.b.is_primary)


class Five9EditUserRevealTests(TestCase):
    """The reveal line and the past-period form appear only where they should."""

    def setUp(self):
        _settings()
        self.target = _make_agent('reveal_target')
        self.target.role_type = 'regular_agent'
        self.target.save()
        self.acct = Five9Profile.objects.create(agent=self.target, five9_username='rv_a',
                                                is_primary=True, billable=True)

    def _login(self, name, super_admin):
        user = User.objects.create_user(name, password='x')
        Agent.objects.create(user=user, role='admin', role_type='supervisor',
                             agent_name=name, status='active', is_super_admin=super_admin)
        self.client.login(username=name, password='x')

    def _html(self):
        return self.client.get(reverse('agent_edit', args=[self.target.pk])).content.decode()

    def test_supervisor_gets_the_plain_starting_today_line_and_no_date_controls(self):
        self._login('rv_sup', super_admin=False)
        html = self._html()
        self.assertIn('New primary counts starting today', html)
        self.assertNotIn('name="five9_primary_start"', html)
        self.assertNotIn('name="five9_primary_always"', html)
        self.assertNotIn('Add a past primary period', html)

    def test_super_admin_gets_the_date_the_always_box_and_the_past_period_form(self):
        self._login('rv_root', super_admin=True)
        html = self._html()
        self.assertIn('name="five9_primary_start"', html)
        self.assertIn('name="five9_primary_always"', html)
        self.assertIn('This account was always the primary', html)
        self.assertIn('Add a past primary period', html)
        self.assertNotIn('New primary counts starting today', html)

    def test_the_date_input_cannot_offer_a_future_day(self):
        self._login('rv_root2', super_admin=True)
        html = self._html()
        today = timezone.localdate().isoformat()
        self.assertIn(f'max="{today}"', html)
        self.assertIn(f'value="{today}"', html)

    def test_the_reveal_starts_hidden(self):
        """It only appears once the selected primary differs from the saved one."""
        self._login('rv_root3', super_admin=True)
        html = self._html()
        self.assertIn('id="five9-primary-reveal"', html)
        self.assertIn('data-saved-primary="', html)


class Five9PastPeriodEndToEndTests(TestCase):
    """The scenario this whole feature exists for: an agent worked on their second
    account for three days in the past and back on the first one afterwards. A
    super admin records that as ONE past primary period. The timeline reads
    correctly and only those three days' stored hours change."""

    def setUp(self):
        _settings(nr_ratio=Decimal('0.125'))
        self.agent = _make_agent('e2e_agent')
        self.a = Five9Profile.objects.create(agent=self.agent, five9_username='e2e_a',
                                             is_primary=True, billable=True)
        self.b = Five9Profile.objects.create(agent=self.agent, five9_username='e2e_b',
                                             is_primary=False, billable=True)
        self.today = timezone.localdate()

        # Ten consecutive days ending yesterday. Account A logs 8h, account B 5h,
        # every day — so a day counted on B is worth 5h and on A 8h.
        self.days = [self.today - timedelta(days=n) for n in range(10, 0, -1)]
        self.records = {}
        for d in self.days:
            upload, _ = DailyUpload.objects.get_or_create(
                date=d, defaults={'filename': 'e2e.csv', 'row_count': 2})
            DailyAgentHours.objects.create(upload=upload, agent=self.agent,
                                           five9_username='e2e_a',
                                           login_seconds=8 * 3600, not_ready_seconds=0)
            DailyAgentHours.objects.create(upload=upload, agent=self.agent,
                                           five9_username='e2e_b',
                                           login_seconds=5 * 3600, not_ready_seconds=0)
            self.records[d] = AdherenceRecord.objects.create(
                agent=self.agent, date=d, status='P', actual_hours=Decimal('8'))

        user = User.objects.create_user('e2e_root', password='x')
        Agent.objects.create(user=user, role='admin', role_type='supervisor',
                             agent_name='E2E Root', status='active', is_super_admin=True)
        self.client.login(username='e2e_root', password='x')

    def test_three_past_days_on_the_second_account_and_back_again(self):
        from scheduling.five9_primary import format_primary_timeline

        # The agent has always been on A, so there is nothing to show yet.
        self.client.post(reverse('agent_edit', args=[self.agent.pk]), {
            'username': self.agent.user.username, 'email': 'e2e@example.com',
            'legal_name': self.agent.agent_name, 'password': '',
            'agent_name': self.agent.agent_name, 'employee_id': '',
            'role': 'agent', 'role_type': 'regular_agent', 'status': 'active',
            'employer': 'Infinity', 'billing_status': 'Not Billed',
            'phone_country_code': '+1', 'phone_number': '', 'teams_password': '',
            'hourly_rate': '62.50', 'billing_rate_usd': '', 'admin_bonus_mxn': '', 'notes': '',
            'five9_primary': str(self.a.pk),
            f'five9_{self.a.pk}_username': 'e2e_a', f'five9_{self.a.pk}_label': '',
            f'five9_{self.a.pk}_billable': 'on',
            f'five9_{self.b.pk}_username': 'e2e_b', f'five9_{self.b.pk}_label': '',
            f'five9_{self.b.pk}_billable': 'on',
        })
        self.assertEqual(format_primary_timeline(self.agent), '')

        frm, to = self.days[3], self.days[5]          # three days, mid-range
        resp = self.client.post(
            reverse('five9_primary_period_add', args=[self.agent.pk]),
            {'profile': str(self.b.pk), 'from': frm.isoformat(), 'to': to.isoformat()})
        self.assertTrue(resp.json()['ok'])

        # Exactly one entry was written, and today's primary is untouched.
        self.assertEqual(
            Five9PrimaryPeriod.objects.filter(agent=self.agent, kind='past_period').count(), 1)
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertTrue(self.a.is_primary)
        self.assertFalse(self.b.is_primary)

        # Only those three days moved to the second account's hours.
        for d, rec in self.records.items():
            rec.refresh_from_db()
            expected = Decimal('5') if frm <= d <= to else Decimal('8')
            self.assertEqual(rec.actual_hours, expected,
                             f'{d.isoformat()} should be {expected}h')

        # And the timeline says so, in plain words.
        line = format_primary_timeline(self.agent)
        self.assertEqual(line, resp.json()['timeline'])
        self.assertIn('e2e_a until ', line)
        self.assertIn('e2e_b ', line)
        self.assertIn('(current)', line)
        self.assertTrue(line.rstrip().endswith('(current)'))
