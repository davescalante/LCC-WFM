from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User

from scheduling.models import Agent
from wfm.utils import get_week_start


def _make_agent(username, is_super_admin=False, role='admin', role_type='supervisor'):
    user = User.objects.create_user(username, password='x')
    return Agent.objects.create(
        user=user, role=role, role_type=role_type,
        agent_name=username, status='active', is_super_admin=is_super_admin,
    )


def _make_infinity(username, employee_id):
    """An Infinity agent that shows on the Agent Nómina / input grids."""
    user = User.objects.create_user(username, password='x', first_name=username.title())
    return Agent.objects.create(
        user=user, role='agent', role_type='agent', agent_name=username,
        status='active', employer='Infinity', track_attendance=True,
        employee_id=employee_id,
    )


class NominaAccessTests(TestCase):
    """The Nómina landing is super-admin only (mirrors Finance gating)."""

    def test_super_admin_can_open(self):
        _make_agent('nom_super', is_super_admin=True)
        self.client.login(username='nom_super', password='x')
        resp = self.client.get(reverse('nomina:dashboard'))
        self.assertEqual(resp.status_code, 200)

    def test_non_super_admin_denied(self):
        _make_agent('nom_plain', is_super_admin=False)
        self.client.login(username='nom_plain', password='x')
        resp = self.client.get(reverse('nomina:dashboard'))
        self.assertEqual(resp.status_code, 302)  # redirected to dashboard

    def test_anonymous_denied(self):
        resp = self.client.get(reverse('nomina:dashboard'))
        self.assertEqual(resp.status_code, 302)  # redirected to login


# The real Spiffs export: an unlabelled "$ amount" column, a name sitting under a
# date header, people repeating on many rows, plus an unmatched #N/A row.
SPIFF_CSV = (
    "Agent Username,Agent ID,7/20/2026,\n"
    "josaranda,3745,JOSE MARIA ARANDA PARAMO,$15.00 \n"
    "vresalido,3838,VRENALY ESCARLETT SALIDO FAJARDO,$74.67 \n"
    "josaranda,3745,Jose Maria Aranda Paramo,$65.00 \n"
    "#N/A,#N/A,Alma Lorenia Duarte Carranza,$20.00 \n"
    "vresalido,3838,VRENALY ESCARLETT SALIDO FAJARDO,$35.00 \n"
    "josaranda,3745,JOSE MARIA ARANDA PARAMO,$15.00 \n"
)


class NominaSpiffUploadTests(TestCase):
    """The Spiffs module must parse the real export (unlabelled $ column, repeats
    summed), convert USD→MXN at the week rate, surface unmatched rows, and let you
    add people by hand afterward without wiping the upload."""

    def setUp(self):
        from nomina.models import NominaWeek, WeeklyPayInput
        self.WeeklyPayInput = WeeklyPayInput
        _make_agent('spf_super', is_super_admin=True)
        self.client.login(username='spf_super', password='x')
        self.josaranda = _make_infinity('josaranda', '3745')
        self.vresalido = _make_infinity('vresalido', '3838')
        self.ws = get_week_start()
        NominaWeek.objects.create(week_start=self.ws, spiff_fx_rate=Decimal('18'))
        self.url = reverse('nomina:input_type', args=['spiff']) + f'?week_start={self.ws.isoformat()}'

    def _upload(self):
        f = SimpleUploadedFile('Spiff example .csv', SPIFF_CSV.encode('utf-8'), content_type='text/csv')
        return self.client.post(self.url, {'file': f}, follow=True)

    def test_upload_sums_repeats_and_converts(self):
        resp = self._upload()
        self.assertEqual(resp.status_code, 200)
        jo = self.WeeklyPayInput.objects.get(agent=self.josaranda, week_start=self.ws)
        vr = self.WeeklyPayInput.objects.get(agent=self.vresalido, week_start=self.ws)
        self.assertEqual(jo.spiff_usd, Decimal('95.00'))    # 15 + 65 + 15
        self.assertEqual(vr.spiff_usd, Decimal('109.67'))   # 74.67 + 35
        # Pesos shown = USD × 18 (95 × 18 = 1,710.00 ; 109.67 × 18 = 1,974.06)
        self.assertContains(resp, 'Pesos (MXN)')
        self.assertContains(resp, '1,710.00')
        self.assertContains(resp, '1,974.06')

    def test_unmatched_rows_are_surfaced(self):
        resp = self._upload()
        self.assertContains(resp, 'unmatched')  # the #N/A row can't be matched

    def test_manual_add_preserves_upload(self):
        self._upload()
        # Re-post the grid: keep josaranda's 95, add vresalido 200 by hand.
        resp = self.client.post(self.url, {
            f'v_{self.josaranda.pk}': '95.00',
            f'v_{self.vresalido.pk}': '200',
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        jo = self.WeeklyPayInput.objects.get(agent=self.josaranda, week_start=self.ws)
        vr = self.WeeklyPayInput.objects.get(agent=self.vresalido, week_start=self.ws)
        self.assertEqual(jo.spiff_usd, Decimal('95.00'))
        self.assertEqual(vr.spiff_usd, Decimal('200.00'))

    def test_reupload_wipes_dropped_people(self):
        self._upload()   # josaranda 95, vresalido 109.67
        # A corrected file that only lists josaranda ($50) — vresalido must go to 0.
        corrected = (
            "Agent Username,Agent ID,7/20/2026,Amount\n"
            "josaranda,3745,JOSE MARIA ARANDA PARAMO,$50.00 \n"
        )
        f = SimpleUploadedFile('corrected.csv', corrected.encode('utf-8'), content_type='text/csv')
        self.client.post(self.url, {'file': f}, follow=True)
        jo = self.WeeklyPayInput.objects.get(agent=self.josaranda, week_start=self.ws)
        vr = self.WeeklyPayInput.objects.get(agent=self.vresalido, week_start=self.ws)
        self.assertEqual(jo.spiff_usd, Decimal('50.00'))   # replaced
        self.assertEqual(vr.spiff_usd, Decimal('0'))       # dropped from new file → wiped

    def test_semicolon_delimited_upload(self):
        csv_semi = (
            "Agent Username;Agent ID;7/20/2026;Amount\n"
            "josaranda;3745;JOSE MARIA ARANDA PARAMO;$15.00 \n"
            "vresalido;3838;VRENALY;$74.67 \n"
        )
        f = SimpleUploadedFile('semi.csv', csv_semi.encode('utf-8'), content_type='text/csv')
        self.client.post(self.url, {'file': f}, follow=True)
        jo = self.WeeklyPayInput.objects.get(agent=self.josaranda, week_start=self.ws)
        self.assertEqual(jo.spiff_usd, Decimal('15.00'))   # not $0 from a one-column mis-parse

    def test_unmatched_rows_persist_until_acknowledged(self):
        from nomina.models import UnmatchedInputRow
        self._upload()
        open_qs = UnmatchedInputRow.objects.filter(
            week_start=self.ws, input_key='spiff', acknowledged=False)
        self.assertEqual(open_qs.count(), 1)   # the #N/A row
        # It still shows on a fresh GET (persistent, not flashed once)
        resp = self.client.get(self.url)
        self.assertContains(resp, 'need review')
        # Acknowledge it → gone
        row = open_qs.first()
        self.client.post(self.url, {'ack': str(row.pk)}, follow=True)
        self.assertEqual(open_qs.count(), 0)
        resp = self.client.get(self.url)
        self.assertNotContains(resp, 'need review')

    def test_acknowledge_all(self):
        from nomina.models import UnmatchedInputRow
        self._upload()
        self.client.post(self.url, {'ack': 'all'}, follow=True)
        self.assertEqual(UnmatchedInputRow.objects.filter(
            week_start=self.ws, input_key='spiff', acknowledged=False).count(), 0)


class NominaVacationsPageTests(TestCase):
    """The top-level Vacations page: admins see everyone, agents see only their own
    row, super admins can adjust available days, others are read-only."""

    def setUp(self):
        self.admin = _make_agent('vac_admin')                                # role='admin'
        self.a1 = _make_agent('vac_a1', role='agent', role_type='regular_agent')
        self.a2 = _make_agent('vac_a2', role='agent', role_type='regular_agent')
        self.superadm = _make_agent('vac_super', is_super_admin=True)
        self.url = reverse('vacations')

    def test_agent_sees_only_their_own_row(self):
        self.client.login(username='vac_a1', password='x')
        resp = self.client.get(self.url)
        self.assertContains(resp, 'vac_a1')          # own agent name
        self.assertNotContains(resp, 'vac_a2')       # not other agents

    def test_admin_sees_everyone(self):
        self.client.login(username='vac_admin', password='x')
        resp = self.client.get(self.url)
        self.assertContains(resp, 'vac_a1')
        self.assertContains(resp, 'vac_a2')

    def test_admin_search_filters(self):
        self.client.login(username='vac_admin', password='x')
        resp = self.client.get(self.url, {'q': 'vac_a2'})
        self.assertContains(resp, 'vac_a2')
        self.assertNotContains(resp, 'vac_a1')

    def test_super_admin_adjusts_available_days(self):
        from nomina.models import VacationAdjustment
        from nomina.views import vacation_balance
        self.client.login(username='vac_super', password='x')
        # a1 has 0 accrued/used → set available to 20 → stores a +20 adjustment.
        self.client.post(self.url, {'agent': self.a1.pk, 'available': '20'}, follow=True)
        adj = VacationAdjustment.objects.get(agent=self.a1)
        self.assertEqual(adj.days, Decimal('20.0'))
        _acc, _used, remaining = vacation_balance(self.a1)
        self.assertEqual(remaining, Decimal('20.0'))

    def test_non_super_admin_cannot_edit(self):
        from nomina.models import VacationAdjustment
        self.client.login(username='vac_admin', password='x')   # admin, not super
        self.client.post(self.url, {'agent': self.a1.pk, 'available': '20'}, follow=True)
        self.assertFalse(VacationAdjustment.objects.filter(agent=self.a1).exists())


class NominaWelcomeBonusTests(TestCase):
    """Welcome Bonus is a POSITIVE for the enrolled agent — paid the enrollment
    amount in a covered week, but only when they earned that week's adherence bonus."""

    def _agent(self):
        from scheduling.models import Five9Profile
        u = User.objects.create_user('welguy', password='x', first_name='Wel', last_name='Guy')
        a = Agent.objects.create(user=u, role='agent', role_type='regular_agent', agent_name='welguy',
            status='active', employer='Infinity', track_attendance=True, hourly_rate=Decimal('62.50'))
        Five9Profile.objects.create(agent=a, five9_username='welagent', billable=True, is_primary=True)
        return a

    def _week(self):
        import datetime
        ws = get_week_start()
        return ws, [ws + datetime.timedelta(days=i) for i in range(7)]

    def test_welcome_paid_positive_when_enrolled_and_bonus_earned(self):
        from nomina.models import WelcomeBonusEnrollment
        from nomina.views import _agent_nomina_data
        from adherence.models import DailyUpload, DailyAgentHours, AdherenceRecord
        a = self._agent()
        ws, week = self._week()
        day = week[1]
        up, _ = DailyUpload.objects.get_or_create(date=day)
        DailyAgentHours.objects.create(upload=up, agent=a, five9_username='welagent',
            login_seconds=40 * 3600, not_ready_seconds=0)               # 40 hrs → full bonus
        AdherenceRecord.objects.update_or_create(agent=a, date=day, defaults={'status': 'P'})
        WelcomeBonusEnrollment.objects.create(agent=a, amount=Decimal('1000'), num_weeks=4, start_week=ws)

        rows, _ = _agent_nomina_data(ws, week)
        r = next(x for x in rows if x['agent'].pk == a.pk)
        self.assertGreater(r['adherence_bonus'], 0)      # earned the adherence bonus
        self.assertEqual(r['welcome'], Decimal('1000'))  # welcome PAID
        self.assertEqual(r['subtotal'], r['base_pay'] + r['adherence_bonus'] + r['welcome'])  # added in

    def test_welcome_zero_without_adherence_bonus(self):
        from nomina.models import WelcomeBonusEnrollment
        from nomina.views import _agent_nomina_data
        a = self._agent()                                # no hours / no adherence → no bonus
        ws, week = self._week()
        WelcomeBonusEnrollment.objects.create(agent=a, amount=Decimal('1000'), num_weeks=4, start_week=ws)
        rows, _ = _agent_nomina_data(ws, week)
        r = next(x for x in rows if x['agent'].pk == a.pk)
        self.assertEqual(r['welcome'], Decimal('0'))     # not paid without the adherence bonus


class NominaAdminLoanTests(TestCase):
    """Admin Nómina loans: the manager who handed out a loan is CREDITED this week's
    repayment (Prestamo given); an admin who took a loan has it DEDUCTED (Prestamo owed)."""

    def _admin(self, username):
        u = User.objects.create_user(username, password='x')
        return Agent.objects.create(
            user=u, role='admin', role_type='supervisor', agent_name=username,
            status='active', employer='Infinity', is_official_admin=True)

    def setUp(self):
        import datetime
        from nomina.models import Loan
        self.Loan = Loan
        self.ws = get_week_start()
        self.week = [self.ws + datetime.timedelta(days=i) for i in range(7)]
        self.mgr = self._admin('mgradmin')
        self.borrower = _make_infinity('loanborrower', '9101')   # regular agent borrower
        self.admin_borrower = self._admin('adminborrower')

    def test_manager_credited_and_admin_repayment_deducted(self):
        from nomina.views import _admin_nomina_data
        # Manager grants a 1-wk $1000 loan to the agent → $1250 back, all this week.
        self.Loan.objects.create(agent=self.borrower, granted_by=self.mgr,
            principal=Decimal('1000'), term_weeks=1, rate=Decimal('1.25'), start_week=self.ws)
        # The admin_borrower takes their OWN 1-wk $500 loan → $625 repaid this week.
        self.Loan.objects.create(agent=self.admin_borrower,
            principal=Decimal('500'), term_weeks=1, rate=Decimal('1.25'), start_week=self.ws)
        rows, _ = _admin_nomina_data(self.ws, self.week)
        mgr = next(r for r in rows if r['agent'].pk == self.mgr.pk)
        ab = next(r for r in rows if r['agent'].pk == self.admin_borrower.pk)
        self.assertEqual(mgr['prestamo_given'], Decimal('1250.00'))   # credited what they handed out
        self.assertEqual(mgr['prestamo_repay'], Decimal('0'))
        self.assertEqual(ab['prestamo_repay'], Decimal('625.00'))     # their own loan deducted
        self.assertEqual(ab['prestamo_given'], Decimal('0'))


class NominaVacationAdherenceGateTests(TestCase):
    """Safety net when a 'V' is placed directly on the adherence grid: a non-super
    can't push an agent past their available vacation days; a super admin can."""

    def setUp(self):
        import datetime
        self.placer = _make_agent('adh_admin')                 # admin/supervisor, non-super
        self.superadm = _make_agent('adh_super', is_super_admin=True)
        self.target = _make_agent('adh_target', role='agent', role_type='regular_agent')  # 0 accrued
        self.date = datetime.date.today()
        self.url = reverse('save_adherence_cell')

    def _post_v(self):
        import json
        return self.client.post(self.url, data=json.dumps({
            'agent_id': self.target.pk, 'date': self.date.isoformat(), 'status': 'V'}),
            content_type='application/json')

    def test_non_super_blocked_when_no_days_left(self):
        from adherence.models import AdherenceRecord
        self.client.login(username='adh_admin', password='x')
        data = self._post_v().json()
        self.assertFalse(data['ok'])
        self.assertTrue(data.get('rejected'))
        self.assertIn('cannot use any more vacation', data['error'])
        self.assertFalse(AdherenceRecord.objects.filter(
            agent=self.target, date=self.date, status='V').exists())

    def test_super_admin_can_place_going_negative(self):
        from adherence.models import AdherenceRecord
        self.client.login(username='adh_super', password='x')
        self.assertTrue(self._post_v().json()['ok'])
        self.assertTrue(AdherenceRecord.objects.filter(
            agent=self.target, date=self.date, status='V').exists())


class NominaVacationApprovalTests(TestCase):
    """Vacation stays a normal request (any supervisor approves), and 'V' days
    draw down the LFT balance. But an OVER-balance approval is blocked for
    supervisors — only a super admin (David/Jhon) can approve the overdraw."""

    def setUp(self):
        import datetime
        from scheduling.models import AgentRequest, EmploymentPeriod
        self.AgentRequest = AgentRequest
        self.today = get_week_start()   # a real date
        import datetime as _dt
        today = _dt.date.today()
        # Requester: ~1 completed year → 12 accrued LFT days.
        self.emp = _make_agent('vacagent', role='agent', role_type='regular_agent')
        EmploymentPeriod.objects.create(agent=self.emp, start_date=_dt.date(today.year - 1, 1, 1))
        # A supervisor (not super admin) and a super admin.
        self.sup = _make_agent('vacsup')                 # role='admin', not super
        self.super = _make_agent('vacsuper', is_super_admin=True)
        self.yr = today.year

    def _req(self, start, end):
        return self.AgentRequest.objects.create(
            agent=self.emp, request_type='vacation', status='pending',
            is_staff_request=False, vacation_start=start, vacation_end=end)

    def _use_days(self, n):
        import datetime
        from adherence.models import AdherenceRecord
        for i in range(n):
            AdherenceRecord.objects.update_or_create(
                agent=self.emp, date=datetime.date(self.yr, 1, 5 + i), defaults={'status': 'V'})

    # ---- balance math ----
    def test_balance_and_overdraw_flags(self):
        from nomina.views import vacation_request_check
        import datetime
        self._use_days(11)   # 11 used of 12 → 1 remaining
        acc, used, rem, new_days, over = vacation_request_check(
            self.emp, datetime.date(self.yr, 3, 2), datetime.date(self.yr, 3, 4))
        self.assertEqual((acc, used, rem), (12, 11, 1))
        self.assertEqual(new_days, 3)
        self.assertTrue(over)                 # 3 requested > 1 remaining

    # ---- endpoint: within balance ----
    def test_supervisor_approves_within_balance(self):
        import datetime
        from adherence.models import AdherenceRecord
        ar = self._req(datetime.date(self.yr, 3, 2), datetime.date(self.yr, 3, 4))
        self.client.login(username='vacsup', password='x')
        self.client.post(reverse('request_approve', args=[ar.pk]), follow=True)
        v = AdherenceRecord.objects.filter(agent=self.emp, status='V',
                                           date__range=(ar.vacation_start, ar.vacation_end)).count()
        self.assertEqual(v, 3)               # approved → 3 V days marked

    # ---- endpoint: overdraw blocked for supervisor, allowed for super ----
    def test_supervisor_blocked_on_overdraw(self):
        import datetime
        from adherence.models import AdherenceRecord
        self._use_days(11)
        ar = self._req(datetime.date(self.yr, 3, 2), datetime.date(self.yr, 3, 4))
        self.client.login(username='vacsup', password='x')
        self.client.post(reverse('request_approve', args=[ar.pk]), follow=True)
        ar.refresh_from_db()
        self.assertEqual(ar.status, 'pending')   # not approved
        self.assertEqual(AdherenceRecord.objects.filter(
            agent=self.emp, status='V', date__range=(ar.vacation_start, ar.vacation_end)).count(), 0)

    def test_super_admin_approves_overdraw(self):
        import datetime
        from adherence.models import AdherenceRecord
        self._use_days(11)
        ar = self._req(datetime.date(self.yr, 3, 2), datetime.date(self.yr, 3, 4))
        self.client.login(username='vacsuper', password='x')
        self.client.post(reverse('request_approve', args=[ar.pk]), follow=True)
        self.assertEqual(AdherenceRecord.objects.filter(
            agent=self.emp, status='V', date__range=(ar.vacation_start, ar.vacation_end)).count(), 3)


class NominaHolidayPayTests(TestCase):
    """End-to-end proof of the holiday-worked premium: an agent who works a
    designated holiday earns a 2× premium on those hours (on top of the 1×
    already in base pay = triple). Uses injected Five9 attendance so the numbers
    are real, and pins down the raw-login vs NR-adjusted-hours behavior."""

    def _agent(self, rate='62.50'):
        from scheduling.models import Five9Profile
        u = User.objects.create_user('holguy', password='x', first_name='Hol', last_name='Guy')
        a = Agent.objects.create(
            user=u, role='agent', role_type='regular_agent', agent_name='holguy',
            status='active', employer='Infinity', track_attendance=True,
            hourly_rate=Decimal(rate),
        )
        Five9Profile.objects.create(agent=a, five9_username='holagent', billable=True, is_primary=True)
        return a

    def _log_day(self, agent, day, login_h, nr_h=0):
        from adherence.models import DailyUpload, DailyAgentHours
        up, _ = DailyUpload.objects.get_or_create(date=day)
        DailyAgentHours.objects.create(
            upload=up, agent=agent, five9_username='holagent',
            login_seconds=int(login_h * 3600), not_ready_seconds=int(nr_h * 3600))

    def test_worked_holiday_pays_triple_with_no_nr(self):
        from nomina.models import Holiday
        from nomina.views import _agent_nomina_data
        import datetime
        a = self._agent()
        ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        holiday = week[2]                       # a mid-week holiday, worked 8h, no NR
        Holiday.objects.create(date=holiday, name='Test Holiday')
        self._log_day(a, holiday, login_h=8, nr_h=0)

        rows, _ = _agent_nomina_data(ws, week)
        r = next(x for x in rows if x['agent'].pk == a.pk)
        # rate 62.50, 8 worked holiday hours, no NR deduction
        self.assertEqual(r['rate'], Decimal('62.50'))
        self.assertEqual(r['hours'], Decimal('8'))                 # final_hrs
        self.assertEqual(r['base_pay'], Decimal('500.00'))         # 8 × 62.50 (the 1×)
        self.assertEqual(r['holiday_pay'], Decimal('1000.00'))     # 8 × 62.50 × 2 (the +2×)
        # Base (1×) + holiday premium (2×) = triple pay on the holiday hours.
        self.assertEqual(r['base_pay'] + r['holiday_pay'], Decimal('8') * Decimal('62.50') * 3)

    def test_holiday_hours_discount_excess_not_ready(self):
        # The rule: 8h logged, 1.5h not-ready → discount the EXCESS over the 12.5%
        # allowance (1.0h) = 0.5h, so the premium is on 7.5 holiday hours, not 8.
        from nomina.models import Holiday
        from nomina.views import _agent_nomina_data
        import datetime
        a = self._agent()
        ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        holiday = week[2]
        Holiday.objects.create(date=holiday, name='Test Holiday')
        self._log_day(a, holiday, login_h=8, nr_h=1.5)

        rows, _ = _agent_nomina_data(ws, week)
        r = next(x for x in rows if x['agent'].pk == a.pk)
        self.assertEqual(r['hours'], Decimal('7.5'))               # final_hrs = 8 − 0.5 NR
        self.assertEqual(r['base_pay'], Decimal('468.75'))         # 7.5 × 62.50
        self.assertEqual(r['holiday_pay'], Decimal('937.50'))      # 7.5 × 62.50 × 2 (NR-adjusted)
        # Exactly triple on the productive (NR-adjusted) holiday hours.
        self.assertEqual(r['base_pay'] + r['holiday_pay'], Decimal('7.5') * Decimal('62.50') * 3)

    def test_holiday_hours_heavier_not_ready(self):
        # 8h logged, 3h not-ready → excess over allowance = 3 − 1.0 = 2.0 → 6 holiday hours.
        from nomina.models import Holiday
        from nomina.views import _agent_nomina_data
        import datetime
        a = self._agent()
        ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        holiday = week[2]
        Holiday.objects.create(date=holiday, name='Test Holiday')
        self._log_day(a, holiday, login_h=8, nr_h=3)

        rows, _ = _agent_nomina_data(ws, week)
        r = next(x for x in rows if x['agent'].pk == a.pk)
        self.assertEqual(r['holiday_pay'], Decimal('750.00'))      # 6 × 62.50 × 2
        self.assertEqual(r['base_pay'], Decimal('375.00'))         # 6 × 62.50


class NominaKillTeamScopeTests(TestCase):
    """Kill Team QA: role-scoped to Kill Team, no EMP column, standard $400 default."""

    def setUp(self):
        from nomina.models import WeeklyPayInput
        self.WeeklyPayInput = WeeklyPayInput
        _make_agent('kt_super', is_super_admin=True)
        self.client.login(username='kt_super', password='x')
        self.kt = _make_infinity('ktone', '9001')
        self.kt.role_type = 'kill_team'; self.kt.agent_name = 'K. Uno'; self.kt.save()
        self.reg = _make_infinity('regone', '9002')   # role_type='agent'
        self.ws = get_week_start()
        self.url = reverse('nomina:input_type', args=['killqa'])

    def test_killqa_shows_only_kill_team(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, 'ktone')
        self.assertNotContains(resp, 'regone')

    def test_other_modules_show_all(self):
        resp = self.client.get(reverse('nomina:input_type', args=['lpo']))
        self.assertContains(resp, 'ktone')
        self.assertContains(resp, 'regone')

    def test_killqa_defaults_to_400_and_hides_emp(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, 'value="400.00"')     # box prefilled with the standard
        self.assertNotContains(resp, '>EMP<')            # EMP column header removed
        self.assertContains(resp, 'K. Uno')             # agent name, not legal name

    def test_nomina_defaults_kill_team_qa_to_400(self):
        from nomina.views import _agent_nomina_data
        week_dates = [self.ws + __import__('datetime').timedelta(days=i) for i in range(7)]
        rows, _ = _agent_nomina_data(self.ws, week_dates)
        kt_row = next(r for r in rows if r['agent'].pk == self.kt.pk)
        self.assertEqual(kt_row['kill_qa'], Decimal('400'))   # auto $400 without any save
        # An entered value wins over the default
        self.WeeklyPayInput.objects.create(agent=self.kt, week_start=self.ws, kill_team_qa=Decimal('250'))
        rows, _ = _agent_nomina_data(self.ws, week_dates)
        kt_row = next(r for r in rows if r['agent'].pk == self.kt.pk)
        self.assertEqual(kt_row['kill_qa'], Decimal('250'))


class NominaComedorUploadTests(TestCase):
    """Comedor is matched by Employee # (EMP), like the real cafeteria export, and
    all of a person's charges are summed. Unmatched emp#s surface for review."""

    def setUp(self):
        from nomina.models import WeeklyPayInput
        self.WeeklyPayInput = WeeklyPayInput
        _make_agent('cf_super', is_super_admin=True)
        self.client.login(username='cf_super', password='x')
        self.a = _make_infinity('comer', '4872')
        self.ws = get_week_start()
        self.url = reverse('nomina:input_type', args=['comedor'])

    def test_matches_by_emp_and_sums(self):
        csv_data = (
            "EMP #, PRECIO ,,,,,,,,\n"
            "4872,$27.00,#REF!, $27.00 ,,,,,,\n"
            "4872,$15.00,#REF!, $15.00 ,,,,,,\n"
            "9999,$50.00,#REF!, $50.00 ,,,,,,\n"
            ",,#REF!, $-   ,,,,,,\n"       # trailing empty row — ignored
        )
        f = SimpleUploadedFile('comedor.csv', csv_data.encode('utf-8'), content_type='text/csv')
        resp = self.client.post(self.url, {'file': f}, follow=True)
        self.assertEqual(resp.status_code, 200)
        wi = self.WeeklyPayInput.objects.get(agent=self.a, week_start=self.ws)
        self.assertEqual(wi.comedor, Decimal('42.00'))     # 27 + 15, matched by emp# 4872
        from nomina.models import UnmatchedInputRow
        unmatched = UnmatchedInputRow.objects.filter(week_start=self.ws, input_key='comedor')
        self.assertEqual(unmatched.count(), 1)             # emp# 9999, not the empty row
        self.assertEqual(unmatched.first().who.strip(), '9999')


class NominaTransportationTests(TestCase):
    """Transportation is manual-only: no upload, an add-an-agent flow, per-week."""

    def setUp(self):
        from nomina.models import WeeklyPayInput
        self.WeeklyPayInput = WeeklyPayInput
        _make_agent('tr_super', is_super_admin=True)
        self.client.login(username='tr_super', password='x')
        self.a = _make_infinity('trone', '8001')
        self.ws = get_week_start()
        self.url = reverse('nomina:input_type', args=['transportation'])

    def test_manual_only_no_upload(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, 'Add an agent')
        self.assertNotContains(resp, 'Upload a file')

    def test_add_shows_then_remove(self):
        self.client.post(self.url, {'add_agent': self.a.pk, 'add_amount': '150'}, follow=True)
        wi = self.WeeklyPayInput.objects.get(agent=self.a, week_start=self.ws)
        self.assertEqual(wi.transportation, Decimal('150'))
        resp = self.client.get(self.url)
        self.assertContains(resp, f'name="v_{self.a.pk}"')   # now an editable list row
        self.client.post(self.url, {'remove': self.a.pk}, follow=True)
        wi.refresh_from_db()
        self.assertEqual(wi.transportation, Decimal('0'))

    def test_starts_empty_each_week(self):
        import datetime
        self.client.post(self.url, {'add_agent': self.a.pk, 'add_amount': '150'}, follow=True)
        nxt = (self.ws + datetime.timedelta(days=7)).isoformat()
        resp = self.client.get(self.url + f'?week_start={nxt}')
        self.assertContains(resp, 'No one added yet this week')


class NominaReferralManualTests(TestCase):
    """Referral uses the same manual add-list as Transportation (no upload)."""

    def setUp(self):
        from nomina.models import WeeklyPayInput
        self.WeeklyPayInput = WeeklyPayInput
        _make_agent('rf_super', is_super_admin=True)
        self.client.login(username='rf_super', password='x')
        self.a = _make_infinity('rfone', '7001')
        self.ws = get_week_start()
        self.url = reverse('nomina:input_type', args=['referral'])

    def test_manual_only_and_add(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, 'Add an agent')
        self.assertNotContains(resp, 'Upload a file')
        self.client.post(self.url, {'add_agent': self.a.pk, 'add_amount': '500'}, follow=True)
        wi = self.WeeklyPayInput.objects.get(agent=self.a, week_start=self.ws)
        self.assertEqual(wi.referral, Decimal('500'))


class NominaFxRateTests(TestCase):
    """The weekly USD→MXN rate is a scalar — a comma is a DECIMAL point (es-MX
    '18,50'), never a thousands separator. A comma must not inflate it 100x, and
    an out-of-band rate must be rejected, not silently stored."""

    def setUp(self):
        from nomina.models import NominaWeek
        self.NominaWeek = NominaWeek
        _make_agent('fx_super', is_super_admin=True)
        self.client.login(username='fx_super', password='x')
        self.ws = get_week_start()
        self.url = reverse('nomina:inputs') + f'?week_start={self.ws.isoformat()}'

    def _post_rate(self, raw):
        return self.client.post(self.url, {'spiff_fx_rate': raw,
                                           'week_start': self.ws.isoformat()}, follow=True)

    def test_decimal_comma_rate_not_inflated(self):
        self._post_rate('18,50')
        nw = self.NominaWeek.objects.get(week_start=self.ws)
        self.assertEqual(nw.spiff_fx_rate, Decimal('18.50'))   # NOT 1850

    def test_plain_decimal_rate(self):
        self._post_rate('18.50')
        self.assertEqual(self.NominaWeek.objects.get(week_start=self.ws).spiff_fx_rate, Decimal('18.50'))

    def test_out_of_range_rate_rejected(self):
        self._post_rate('1850')     # absurd FX rate → rejected, left unset
        self.assertIsNone(self.NominaWeek.objects.get(week_start=self.ws).spiff_fx_rate)

    def test_blank_clears_rate(self):
        self._post_rate('18.50')
        self._post_rate('')
        self.assertIsNone(self.NominaWeek.objects.get(week_start=self.ws).spiff_fx_rate)
