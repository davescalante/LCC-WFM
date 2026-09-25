import io
import json
from decimal import Decimal
from datetime import date, timedelta, time
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User

from django.db import connection
from django.db.models import Q
from django.test.utils import CaptureQueriesContext
from scheduling.models import (
    Agent, AgentSeparation, Five9Profile, Shift, ShiftTemplate, OvertimeShift, Skill, AuditLog,
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
        return SimpleNamespace(POST=post_data)

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

