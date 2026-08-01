import io
from decimal import Decimal
from datetime import date, timedelta, time
from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User

import openpyxl

from scheduling.models import Agent, Five9Profile, AgentSeparation
from adherence.models import DailyUpload, DailyAgentHours, Coding
from finance.models import BillingSettings


_WEEK_START = date(2025, 1, 6)
_WEEK = [_WEEK_START + timedelta(days=i) for i in range(7)]


def _make_agent(username, role='agent', role_type='agent'):
    user = User.objects.create_user(username, password='x')
    return Agent.objects.create(
        user=user, role=role, role_type=role_type,
        agent_name=username, status='active', track_attendance=True,
    )


def _settings(**overrides):
    obj, _ = BillingSettings.objects.get_or_create(pk=1)
    for k, v in overrides.items():
        setattr(obj, k, v)
    obj.save()
    return obj


def _add_hours(agent, login_seconds, nr_seconds, week_day=0):
    upload, _ = DailyUpload.objects.get_or_create(date=_WEEK[week_day])
    DailyAgentHours.objects.create(
        upload=upload, agent=agent,
        five9_username=agent.agent_name,
        login_seconds=login_seconds,
        not_ready_seconds=nr_seconds,
    )


class NRCapCheck1Tests(TestCase):
    """check1: deduct NR hours above the absolute weekly cap."""

    def setUp(self):
        self.agent = _make_agent('check1_agent')
        # nr_ratio_max_hours=200 ensures check2 is disabled (pre_total always < 200)
        # Actually set it low so check2 doesn't trigger when login >> 48
        self.s = _settings(
            nr_cap_regular_hours=Decimal('6.00'),
            nr_ratio=Decimal('0.1250'),
            nr_ratio_max_hours=Decimal('200.00'),
        )

    def _call(self):
        from finance.views import _get_billable_weekly_data
        return _get_billable_weekly_data([self.agent], _WEEK, self.s)

    def test_nr_above_cap_triggers_check1(self):
        # 100 h login, 8 h NR → check1=2, check2=8-12.5=0 → deduction=2
        _add_hours(self.agent, 100 * 3600, 8 * 3600)
        result = self._call()[self.agent.pk]
        self.assertAlmostEqual(float(result['final_hrs']), 100.0 - 2.0, places=2)

    def test_nr_at_cap_no_check1(self):
        # 100 h login, 6 h NR → check1=0, check2=6-12.5=0 → no deduction
        _add_hours(self.agent, 100 * 3600, 6 * 3600)
        result = self._call()[self.agent.pk]
        self.assertAlmostEqual(float(result['final_hrs']), 100.0, places=2)


class NRCapCheck2Tests(TestCase):
    """check2: deduct NR hours above 12.5% of login time."""

    def setUp(self):
        self.agent = _make_agent('check2_agent')
        # cap=99 so check1 never triggers; ratio=0.125; max_hours=48
        self.s = _settings(
            nr_cap_regular_hours=Decimal('99.00'),
            nr_ratio=Decimal('0.1250'),
            nr_ratio_max_hours=Decimal('48.00'),
        )

    def _call(self):
        from finance.views import _get_billable_weekly_data
        return _get_billable_weekly_data([self.agent], _WEEK, self.s)

    def test_nr_above_ratio_triggers_check2(self):
        # 20 h login, 6 h NR → allowance=20*0.125=2.5 → excess=3.5
        _add_hours(self.agent, 20 * 3600, 6 * 3600)
        result = self._call()[self.agent.pk]
        # final = 20 - 3.5 = 16.5
        self.assertAlmostEqual(float(result['final_hrs']), 16.5, places=2)

    def test_nr_within_ratio_no_deduction(self):
        # 40 h login, 4 h NR → allowance=40*0.125=5 → no excess
        _add_hours(self.agent, 40 * 3600, 4 * 3600)
        result = self._call()[self.agent.pk]
        self.assertAlmostEqual(float(result['final_hrs']), 40.0, places=2)

    def test_check2_disabled_above_max_hours(self):
        # login=50 h (above nr_ratio_max_hours=48) → check2 disabled
        # cap=99 so check1 also won't fire → no deduction at all
        _add_hours(self.agent, 50 * 3600, 10 * 3600)
        result = self._call()[self.agent.pk]
        self.assertAlmostEqual(float(result['final_hrs']), 50.0, places=2)


class NRCapMaxOfTwoTests(TestCase):
    """The larger of check1 and check2 is applied, never both."""

    def setUp(self):
        self.agent = _make_agent('max_test')
        self.s = _settings(
            nr_cap_regular_hours=Decimal('6.00'),
            nr_ratio=Decimal('0.1250'),
            nr_ratio_max_hours=Decimal('48.00'),
        )

    def _call(self):
        from finance.views import _get_billable_weekly_data
        return _get_billable_weekly_data([self.agent], _WEEK, self.s)

    def test_check2_wins_when_login_low(self):
        # 20 h login, 8 h NR
        # check1 = max(0, 8-6) = 2
        # check2 = max(0, 8 - 20*0.125) = max(0, 8-2.5) = 5.5
        # max = 5.5 → final = 20 - 5.5 = 14.5
        _add_hours(self.agent, 20 * 3600, 8 * 3600)
        result = self._call()[self.agent.pk]
        self.assertAlmostEqual(float(result['final_hrs']), 14.5, places=2)

    def test_check1_wins_when_login_high(self):
        # 100 h login (above max_hours=48 → check2 disabled), 8 h NR
        # check1 = 2, check2 = 0 (disabled) → final = 100 - 2 = 98
        _add_hours(self.agent, 100 * 3600, 8 * 3600)
        result = self._call()[self.agent.pk]
        self.assertAlmostEqual(float(result['final_hrs']), 98.0, places=2)

    def test_final_hours_never_negative(self):
        # Pathological: 5 h login, 100 h NR
        _add_hours(self.agent, 5 * 3600, 100 * 3600)
        result = self._call()[self.agent.pk]
        self.assertGreaterEqual(float(result['final_hrs']), 0.0)


XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

HEADERS = [
    'Agent Name', 'Agent Username', 'Date', 'Day', 'Type',
    'Start', 'End', 'Duration (Decimal)', 'Duration (HH:MM:SS)',
]


class CodingsExportTests(TestCase):
    """Export Codings — regular + admin codings for a week, per agent, with totals."""

    def setUp(self):
        # role='admin'/role_type='supervisor' keeps AgentAccessMiddleware from
        # treating this login as a portal agent restricted to /agent/ paths.
        self.boss = _make_agent('codeboss', role='admin', role_type='supervisor')
        self.boss.is_super_admin = True
        self.boss.save()
        self.client.login(username='codeboss', password='x')

    def _export_ws(self, week_start=_WEEK_START):
        resp = self.client.get(reverse('codings_export') + f'?week={week_start.isoformat()}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], XLSX_MIME)
        return openpyxl.load_workbook(io.BytesIO(resp.content)).active

    def _rows(self, ws):
        return [list(r) for r in ws.iter_rows(values_only=True)]

    def test_header_row(self):
        ws = self._export_ws()
        rows = self._rows(ws)
        self.assertEqual(rows[2], HEADERS)

    def test_filename_and_content_type(self):
        resp = self.client.get(reverse('codings_export') + f'?week={_WEEK_START.isoformat()}')
        self.assertEqual(resp['Content-Type'], XLSX_MIME)
        self.assertIn(f'codings_{_WEEK_START.isoformat()}.xlsx', resp['Content-Disposition'])

    def test_regular_and_admin_codings_both_appear(self):
        agent = _make_agent('rega')
        agent.is_official_admin = True
        agent.save()
        Five9Profile.objects.create(agent=agent, five9_username='rega.f9', billable=True, is_primary=True)
        Coding.objects.create(agent=agent, date=_WEEK_START, start_time=time(9, 0), end_time=time(10, 0),
                               is_admin_coding=False)
        Coding.objects.create(agent=agent, date=_WEEK_START, start_time=time(11, 0), end_time=time(12, 30),
                               is_admin_coding=True)
        rows = self._rows(self._export_ws())
        full_name = agent.user.get_full_name() or agent.agent_name
        types = [r[4] for r in rows if r[0] == full_name]
        self.assertIn('Regular', types)
        self.assertIn('Admin', types)

    def test_block_duration_conversion(self):
        # 90 minutes -> 1.5 decimal hours and 01:30:00
        agent = _make_agent('conv1')
        Coding.objects.create(agent=agent, date=_WEEK_START, start_time=time(9, 0), end_time=time(10, 30),
                               is_admin_coding=False)
        rows = self._rows(self._export_ws())
        block = next(r for r in rows if r[4] == 'Regular')
        self.assertEqual(block[7], 1.5)
        self.assertEqual(block[8], '01:30:00')

    def test_daily_total(self):
        agent = _make_agent('daily1')
        Coding.objects.create(agent=agent, date=_WEEK_START, start_time=time(9, 0), end_time=time(10, 0),
                               is_admin_coding=False)  # 1h
        Coding.objects.create(agent=agent, date=_WEEK_START, start_time=time(11, 0), end_time=time(11, 30),
                               is_admin_coding=False)  # 0.5h
        rows = self._rows(self._export_ws())
        daily = next(r for r in rows if r[0] == 'Daily total')
        self.assertEqual(daily[7], 1.5)
        self.assertEqual(daily[8], '01:30:00')

    def test_weekly_total(self):
        agent = _make_agent('weekly1')
        Coding.objects.create(agent=agent, date=_WEEK_START, start_time=time(9, 0), end_time=time(10, 0),
                               is_admin_coding=False)  # 1h, day 0
        Coding.objects.create(agent=agent, date=_WEEK_START + timedelta(days=1), start_time=time(9, 0),
                               end_time=time(9, 30), is_admin_coding=False)  # 0.5h, day 1
        rows = self._rows(self._export_ws())
        weekly = next(r for r in rows if r[0] == 'Weekly total')
        self.assertEqual(weekly[7], 1.5)
        self.assertEqual(weekly[8], '01:30:00')

    def test_time_columns_are_text_formatted(self):
        agent = _make_agent('fmt1')
        Coding.objects.create(agent=agent, date=_WEEK_START, start_time=time(9, 0), end_time=time(10, 30),
                               is_admin_coding=False)
        ws = self._export_ws()
        row_idx = next(
            r for r in range(4, ws.max_row + 1) if ws.cell(row=r, column=5).value == 'Regular'
        )
        self.assertEqual(ws.cell(row=row_idx, column=6).number_format, '@')
        self.assertEqual(ws.cell(row=row_idx, column=7).number_format, '@')
        self.assertEqual(ws.cell(row=row_idx, column=9).number_format, '@')

    def test_agent_with_no_codings_absent(self):
        agent = _make_agent('empty1')
        rows = self._rows(self._export_ws())
        names = [r[0] for r in rows]
        self.assertNotIn(agent.user.get_full_name() or agent.agent_name, names)

    def test_coding_outside_week_excluded(self):
        agent = _make_agent('outside1')
        Coding.objects.create(agent=agent, date=_WEEK_START - timedelta(days=1), start_time=time(9, 0),
                               end_time=time(10, 0), is_admin_coding=False)
        Coding.objects.create(agent=agent, date=_WEEK_START + timedelta(days=7), start_time=time(9, 0),
                               end_time=time(10, 0), is_admin_coding=False)
        rows = self._rows(self._export_ws())
        names = [r[0] for r in rows]
        self.assertNotIn('outside1', names)

    def test_non_super_admin_denied(self):
        self.client.logout()
        _make_agent('plainadmin', role='admin', role_type='supervisor')
        self.client.login(username='plainadmin', password='x')
        resp = self.client.get(reverse('codings_export') + f'?week={_WEEK_START.isoformat()}')
        self.assertEqual(resp.status_code, 302)

    def test_unauthenticated_denied(self):
        self.client.logout()
        resp = self.client.get(reverse('codings_export') + f'?week={_WEEK_START.isoformat()}')
        self.assertEqual(resp.status_code, 302)

    def test_pay_window_inclusion_regular(self):
        agent = _make_agent('payin1')
        agent.status = 'inactive'
        agent.save()
        AgentSeparation.objects.create(
            agent=agent, status='finalized', separation_type='quit',
            last_day_worked=_WEEK_START - timedelta(days=3),
            remove_from_adherence_date=_WEEK_START + timedelta(days=1),
        )
        Coding.objects.create(agent=agent, date=_WEEK_START, start_time=time(9, 0), end_time=time(10, 0),
                               is_admin_coding=False)
        rows = self._rows(self._export_ws())
        names = [r[0] for r in rows]
        self.assertIn('payin1', names)

    def test_pay_window_inclusion_admin(self):
        agent = _make_agent('payin2')
        agent.status = 'inactive'
        agent.is_official_admin = True
        agent.save()
        Five9Profile.objects.create(agent=agent, five9_username='payin2.f9', billable=True, is_primary=True)
        AgentSeparation.objects.create(
            agent=agent, status='finalized', separation_type='quit',
            last_day_worked=_WEEK_START - timedelta(days=3),
            remove_from_adherence_date=_WEEK_START + timedelta(days=1),
        )
        Coding.objects.create(agent=agent, date=_WEEK_START, start_time=time(9, 0), end_time=time(10, 0),
                               is_admin_coding=True)
        rows = self._rows(self._export_ws())
        names = [r[0] for r in rows]
        self.assertIn('payin2', names)

    def test_pay_window_exclusion(self):
        agent = _make_agent('payout1')
        agent.status = 'inactive'
        agent.save()
        AgentSeparation.objects.create(
            agent=agent, status='finalized', separation_type='quit',
            last_day_worked=_WEEK_START - timedelta(days=10),
            remove_from_adherence_date=_WEEK_START,  # not > week_start -> already passed
        )
        Coding.objects.create(agent=agent, date=_WEEK_START, start_time=time(9, 0), end_time=time(10, 0),
                               is_admin_coding=False)
        rows = self._rows(self._export_ws())
        names = [r[0] for r in rows]
        self.assertNotIn('payout1', names)
