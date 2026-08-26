import json
from decimal import Decimal
from datetime import date, timedelta, time
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User

from scheduling.models import Agent, AgentSeparation, Five9Profile, Shift, ShiftTemplate
from adherence.models import AdherenceRecord, DailyUpload, DailyAgentHours, Coding, AdherenceNote
from adherence.views import _get_adherence_agent_pks, _net_ot_evening_hours
from finance.models import BillingSettings
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
