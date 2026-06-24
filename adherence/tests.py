from decimal import Decimal
from datetime import date, timedelta
from django.test import TestCase
from django.contrib.auth.models import User

from scheduling.models import Agent
from adherence.models import AdherenceRecord, DailyUpload, DailyAgentHours
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
