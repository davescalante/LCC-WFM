from decimal import Decimal
from datetime import date, timedelta
from django.test import TestCase
from django.contrib.auth.models import User

from scheduling.models import Agent
from adherence.models import DailyUpload, DailyAgentHours
from finance.models import BillingSettings


_WEEK_START = date(2025, 1, 6)
_WEEK = [_WEEK_START + timedelta(days=i) for i in range(7)]


def _make_agent(username, role_type='agent'):
    user = User.objects.create_user(username, password='x')
    return Agent.objects.create(
        user=user, role='agent', role_type=role_type,
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
