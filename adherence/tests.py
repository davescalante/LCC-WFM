from decimal import Decimal
from datetime import date, timedelta, time
from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User

from scheduling.models import Agent, Shift
from adherence.models import AdherenceRecord, DailyUpload, DailyAgentHours, Coding
from finance.models import BillingSettings


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
