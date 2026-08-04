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

    def test_official_admin_matches_by_username_not_unmatched(self):
        """One file covers the whole roster: an Official Admin in an uploaded input
        file matches by username (not flagged unmatched) and the value lands on their
        WeeklyPayInput, so it flows to the Admin Nómina."""
        from nomina.models import UnmatchedInputRow
        admin = Agent.objects.create(
            user=User.objects.create_user('petdamian', password='x', first_name='Pet'),
            role='admin', role_type='supervisor', agent_name='petdamian',
            status='active', employer='Infinity', is_official_admin=True, employee_id='5000')
        csv = ("Agent Username,Agent ID,7/20/2026,Amount\n"
               "petdamian,5000,PET DAMIAN,$300.00 \n")
        f = SimpleUploadedFile('lpo.csv', csv.encode('utf-8'), content_type='text/csv')
        lpo_url = reverse('nomina:input_type', args=['lpo']) + f'?week_start={self.ws.isoformat()}'
        self.client.post(lpo_url, {'file': f}, follow=True)
        wi = self.WeeklyPayInput.objects.get(agent=admin, week_start=self.ws)
        self.assertEqual(wi.lpo, Decimal('300.00'))
        self.assertFalse(UnmatchedInputRow.objects.filter(
            week_start=self.ws, input_key='lpo').exists())


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


class NominaVacationBalanceTests(TestCase):
    """Balance is per WORK ANNIVERSARY: everyone resets to their full LFT days on
    their hire anniversary; only 'V' days since that anniversary count as used."""

    def test_used_counts_only_since_anniversary(self):
        import datetime
        from scheduling.models import EmploymentPeriod
        from adherence.models import AdherenceRecord
        from nomina.views import vacation_balance
        today = datetime.date.today()
        a = _make_agent('annguy', role='agent', role_type='regular_agent')
        # Hire anniversary ~30 days ago, 2 completed years → 14 accrued days.
        anniv = today - datetime.timedelta(days=30)
        EmploymentPeriod.objects.create(agent=a, start_date=anniv.replace(year=anniv.year - 2))
        AdherenceRecord.objects.create(agent=a, date=today - datetime.timedelta(days=60), status='V')  # before anniv
        AdherenceRecord.objects.create(agent=a, date=today - datetime.timedelta(days=10), status='V')  # after anniv
        acc, used, rem = vacation_balance(a)
        self.assertEqual(acc, 14)
        self.assertEqual(used, 1)          # only the post-anniversary V counts
        self.assertEqual(rem, Decimal('13'))


class NominaVacationPayTests(TestCase):
    """Vacation piece A: a 'V' day is paid — min(scheduled, 8) hours, or 8 if a day
    off — folded into Pay (48) and Total Hours; plus the 'on vacation' indicator."""

    def test_scheduled_vacation_day_pays_min_sched_8(self):
        import datetime
        from nomina.views import _agent_nomina_data
        from adherence.models import AdherenceRecord
        from scheduling.models import Shift
        a = _make_infinity('vacpay', '9200')     # hourly_rate defaults to 62.50
        ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        day = week[1]
        Shift.objects.create(agent=a, date=day, is_off=False,
                             start_time=datetime.time(9, 0), end_time=datetime.time(17, 0))  # 8h
        AdherenceRecord.objects.update_or_create(agent=a, date=day, defaults={'status': 'V'})
        rows, totals = _agent_nomina_data(ws, week)
        r = next(x for x in rows if x['agent'].pk == a.pk)
        self.assertEqual(r['vac_hrs'], Decimal('8'))
        self.assertEqual(r['vac_pay'], Decimal('500.00'))    # 8 × 62.50
        self.assertEqual(r['base_pay'], Decimal('500.00'))   # Pay(48) = base 0 + vacation 500
        self.assertEqual(r['total_hrs'], Decimal('8'))       # vacation folded into hours
        self.assertEqual(totals['on_vacation'], 1)

    def test_vacation_on_day_off_pays_flat_8(self):
        import datetime
        from nomina.views import _agent_nomina_data
        from adherence.models import AdherenceRecord
        a = _make_infinity('vacpay2', '9201')
        ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        AdherenceRecord.objects.update_or_create(agent=a, date=week[3], defaults={'status': 'V'})  # no shift
        rows, _ = _agent_nomina_data(ws, week)
        r = next(x for x in rows if x['agent'].pk == a.pk)
        self.assertEqual(r['vac_hrs'], Decimal('8'))         # flat 8 when unscheduled
        self.assertEqual(r['vac_pay'], Decimal('500.00'))

    def test_scheduled_over_8_capped(self):
        import datetime
        from nomina.views import _agent_nomina_data
        from adherence.models import AdherenceRecord
        from scheduling.models import Shift
        a = _make_infinity('vacpay3', '9202')
        ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        Shift.objects.create(agent=a, date=week[1], is_off=False,
                             start_time=datetime.time(8, 0), end_time=datetime.time(18, 0))  # 10h
        AdherenceRecord.objects.update_or_create(agent=a, date=week[1], defaults={'status': 'V'})
        rows, _ = _agent_nomina_data(ws, week)
        r = next(x for x in rows if x['agent'].pk == a.pk)
        self.assertEqual(r['vac_hrs'], Decimal('8'))         # capped at 8


class NominaNonBillableNoOverpayTests(TestCase):
    """The Agent Nómina lists everyone Infinity, including untracked sales agents. But a
    person who is untracked AND has no billable Five9 profile must NOT be paid base pay or
    an adherence bonus on any NON-billable Five9 hours — the billing engine's 'no billable
    profile → count everything' fallback would otherwise silently overpay them. Their rate
    is preserved so manual Extra Hours / vacation still compute; tracked or billable agents
    are unaffected."""

    def _week(self):
        import datetime
        ws = get_week_start()
        return ws, [ws + datetime.timedelta(days=i) for i in range(7)]

    def test_untracked_non_billable_hours_are_not_paid(self):
        from scheduling.models import Five9Profile
        from adherence.models import DailyUpload, DailyAgentHours
        from nomina.views import _agent_nomina_data
        ws, week = self._week()
        day = week[1]
        up, _ = DailyUpload.objects.get_or_create(date=day)

        # Sales-type agent: Infinity, NOT tracked, ONE non-billable Five9 profile that
        # logged 40 hours this week (e.g. a non-billable campaign / trainee account).
        sales = _make_infinity('salesrep', '4001')
        sales.track_attendance = False
        sales.hourly_rate = Decimal('62.50')
        sales.save()
        Five9Profile.objects.create(agent=sales, five9_username='salesrep.f9',
                                    billable=False, is_primary=True)
        DailyAgentHours.objects.create(upload=up, agent=sales,
            five9_username='salesrep.f9', login_seconds=40 * 3600, not_ready_seconds=0)

        # Control: a normal tracked agent with a BILLABLE profile and the same hours.
        normal = _make_infinity('callrep', '4002')
        normal.hourly_rate = Decimal('62.50')
        normal.save()
        Five9Profile.objects.create(agent=normal, five9_username='callrep.f9',
                                    billable=True, is_primary=True)
        DailyAgentHours.objects.create(upload=up, agent=normal,
            five9_username='callrep.f9', login_seconds=40 * 3600, not_ready_seconds=0)

        rows, _ = _agent_nomina_data(ws, week)
        s = next(r for r in rows if r['agent'].pk == sales.pk)
        n = next(r for r in rows if r['agent'].pk == normal.pk)

        # Sales rep shows on the roster but earns no call-based pay on non-billable hours.
        self.assertEqual(s['base_pay'], Decimal('0'))
        self.assertEqual(s['hours'], Decimal('0'))
        self.assertEqual(s['adherence_bonus'], Decimal('0'))
        # Rate is preserved, so manual Extra Hours / vacation / holiday still compute.
        self.assertEqual(s['rate'], Decimal('62.50'))
        # Control agent (tracked, billable) is paid normally — no regression.
        self.assertGreater(n['base_pay'], Decimal('0'))
        self.assertGreater(n['hours'], Decimal('0'))


class NominaLoanPickerTests(TestCase):
    """Loans page: the agent picker is A–Z by agent name, and the Active-loans panel
    totals the installments actually deducted THIS week (not every loan's weekly amount)."""

    def setUp(self):
        _make_agent('loan_super', is_super_admin=True)
        self.client.login(username='loan_super', password='x')
        self.ws = get_week_start()

    def test_picker_is_alphabetical_and_week_total_is_this_weeks_installments(self):
        import datetime
        from nomina.models import Loan
        zoe = _make_infinity('zoepicker', '9001'); zoe.agent_name = 'Zoe Zapata'; zoe.save()
        amy = _make_infinity('amypicker', '9002'); amy.agent_name = 'amy alvarez'; amy.save()
        # zoe's loan is active THIS week; amy's 1-week loan started last week → not this week.
        Loan.objects.create(agent=zoe, principal=Decimal('1000'), term_weeks=1,
                            rate=Decimal('1.25'), start_week=self.ws)                      # 1,250 this week
        Loan.objects.create(agent=amy, principal=Decimal('500'), term_weeks=1,
                            rate=Decimal('1.25'), start_week=self.ws - datetime.timedelta(days=7))
        resp = self.client.get(reverse('nomina:loans') + f'?week_start={self.ws.isoformat()}')
        self.assertEqual(resp.status_code, 200)
        names = [a.agent_name for a in resp.context['agents']]
        self.assertEqual(names, sorted(names, key=lambda s: (s or '').lower()))   # A–Z (case-insensitive)
        # Only zoe's loan is deducted this week; amy's contributes 0.
        self.assertEqual(resp.context['week_total'], Decimal('1250.00'))


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

    def test_not_worked_holiday_pays_scheduled_1x(self):
        # Decision 5b: scheduled but NOT worked (status 'Holiday') → 0 worked hours,
        # Holiday Pay = scheduled hours × rate (1×), nothing added to Hours Worked.
        from nomina.models import Holiday
        from nomina.views import _agent_nomina_data
        from adherence.models import AdherenceRecord
        from scheduling.models import Shift
        import datetime
        a = self._agent()
        ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        holiday = week[2]
        Holiday.objects.create(date=holiday, name='Test Holiday')
        Shift.objects.create(agent=a, date=holiday, is_off=False,
                             start_time=datetime.time(9, 0), end_time=datetime.time(17, 0))   # 8h scheduled
        AdherenceRecord.objects.update_or_create(agent=a, date=holiday, defaults={'status': 'Holiday'})
        # No Five9 login logged that day → not worked.
        rows, _ = _agent_nomina_data(ws, week)
        r = next(x for x in rows if x['agent'].pk == a.pk)
        self.assertEqual(r['holiday_pay'], Decimal('500.00'))      # 8 × 62.50 × 1
        self.assertEqual(r['holiday_hrs'], Decimal('0'))           # 0 worked holiday hours
        self.assertEqual(r['worked_hrs'], Decimal('0'))            # nothing added to hours worked

    def test_holiday_status_is_bonus_qualifying(self):
        from wfm.constants import BONUS_QUALIFYING, BONUS_DISQUALIFYING
        self.assertIn('Holiday', BONUS_QUALIFYING)        # a not-worked holiday must NOT kill the bonus
        self.assertNotIn('Holiday', BONUS_DISQUALIFYING)


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

    def test_explicit_zero_sticks_and_is_not_reverted_to_400(self):
        """A Kill Team agent can be zeroed out: an explicit $0 (a blank/0 box, then Save)
        is KEPT — not bounced back to the $400 default — on the module box AND the nómina."""
        import datetime
        week = [self.ws + datetime.timedelta(days=i) for i in range(7)]
        self.client.post(self.url, {f'v_{self.kt.pk}': '0'})            # Save $0 via the module
        wi = self.WeeklyPayInput.objects.get(agent=self.kt, week_start=self.ws)
        self.assertEqual(wi.kill_team_qa, Decimal('0'))                 # stored as an explicit 0
        resp = self.client.get(self.url)
        self.assertNotContains(resp, 'value="400.00"')                 # box no longer prefills 400
        from nomina.views import _agent_nomina_data
        rows, _ = _agent_nomina_data(self.ws, week)
        kt_row = next(r for r in rows if r['agent'].pk == self.kt.pk)
        self.assertEqual(kt_row['kill_qa'], Decimal('0'))              # nómina pays 0, not 400

    def test_default_applies_when_only_another_module_created_the_row(self):
        """A Kill Team agent with a WeeklyPayInput row from ANOTHER module (e.g. LPO) but no
        Kill Team QA entry still gets the $400 default — it's the never-entered (NULL) state
        that triggers the default, not merely the absence of a row."""
        import datetime
        week = [self.ws + datetime.timedelta(days=i) for i in range(7)]
        self.WeeklyPayInput.objects.create(agent=self.kt, week_start=self.ws, lpo=Decimal('500'))
        wi = self.WeeklyPayInput.objects.get(agent=self.kt, week_start=self.ws)
        self.assertIsNone(wi.kill_team_qa)                              # QA never entered → NULL
        from nomina.views import _agent_nomina_data
        rows, _ = _agent_nomina_data(self.ws, week)
        kt_row = next(r for r in rows if r['agent'].pk == self.kt.pk)
        self.assertEqual(kt_row['kill_qa'], Decimal('400'))


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


class NominaCorrectnessFixTests(TestCase):
    """Batch 1 correctness fixes: spiff FX guard, anniversary-year adjustment keying,
    and split-shift vacation hours."""

    def test_spiff_needs_rate_flag_and_zero_conversion(self):
        import datetime
        from nomina.views import _agent_nomina_data
        from nomina.models import WeeklyPayInput, NominaWeek
        a = _make_infinity('spfguard', '9204')
        ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        WeeklyPayInput.objects.create(agent=a, week_start=ws, spiff_usd=Decimal('20'))
        # No rate set → flagged (not silently paid $0 with no warning).
        rows, totals = _agent_nomina_data(ws, week)
        self.assertTrue(totals['spiff_needs_rate'])
        self.assertEqual(totals['spiff_unpaid_count'], 1)
        self.assertEqual(next(r for r in rows if r['agent'].pk == a.pk)['spiff_mxn'], Decimal('0'))
        # Rate set → flag clears and the spiff converts.
        nw, _ = NominaWeek.objects.get_or_create(week_start=ws)
        nw.spiff_fx_rate = Decimal('18'); nw.save()
        rows, totals = _agent_nomina_data(ws, week)
        self.assertFalse(totals['spiff_needs_rate'])
        self.assertEqual(next(r for r in rows if r['agent'].pk == a.pk)['spiff_mxn'], Decimal('360.00'))

    def test_vacation_year_uses_anniversary_not_calendar(self):
        import datetime
        from scheduling.models import EmploymentPeriod
        from nomina.views import _vacation_year
        a = _make_agent('annyr', role='agent', role_type='regular_agent')
        EmploymentPeriod.objects.create(agent=a, start_date=datetime.date(2020, 6, 15))
        # Jan 2027 is BEFORE the June anniversary → still the vac-year that began 2026.
        self.assertEqual(_vacation_year(a, datetime.date(2027, 1, 10)), 2026)
        # Aug 2027 is after → 2027.
        self.assertEqual(_vacation_year(a, datetime.date(2027, 8, 10)), 2027)

    def test_vacation_adjustment_survives_calendar_boundary(self):
        import datetime
        from scheduling.models import EmploymentPeriod
        from nomina.models import VacationAdjustment
        from nomina.views import vacation_balance
        a = _make_agent('annbnd', role='agent', role_type='regular_agent')
        EmploymentPeriod.objects.create(agent=a, start_date=datetime.date(2020, 6, 15))
        VacationAdjustment.objects.create(agent=a, year=2026, days=Decimal('3'))
        as_of = datetime.date(2027, 1, 10)   # new calendar year, SAME vac-year (June anniversary)
        acc, used, rem = vacation_balance(a, as_of)
        self.assertEqual(rem, Decimal(acc) - used + Decimal('3'))   # adjustment not lost across Jan 1

    def test_split_shift_vacation_counts_extra_block(self):
        import datetime
        from nomina.views import _agent_nomina_data
        from adherence.models import AdherenceRecord
        from scheduling.models import Shift, ShiftBlock
        a = _make_infinity('vacsplit', '9203')
        ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        day = week[2]
        sh = Shift.objects.create(agent=a, date=day, is_off=False,
                                  start_time=datetime.time(9, 0), end_time=datetime.time(13, 0))   # 4h main
        ShiftBlock.objects.create(shift=sh, block_number=2,
                                  start_time=datetime.time(14, 0), end_time=datetime.time(17, 0))  # +3h
        AdherenceRecord.objects.update_or_create(agent=a, date=day, defaults={'status': 'V'})
        rows, _ = _agent_nomina_data(ws, week)
        r = next(x for x in rows if x['agent'].pk == a.pk)
        self.assertEqual(r['vac_hrs'], Decimal('7'))   # 4h main + 3h split block (was 4 before the fix)

    def test_no_vacation_carryover_across_anniversary(self):
        # On the work anniversary everyone resets to full LFT days; leftover unused
        # days are LOST (no carryover) — the anniversary-based balance already does this.
        import datetime
        from scheduling.models import EmploymentPeriod
        from adherence.models import AdherenceRecord
        from nomina.views import vacation_balance
        a = _make_agent('carry', role='agent', role_type='regular_agent')
        EmploymentPeriod.objects.create(agent=a, start_date=datetime.date(2022, 6, 15))
        AdherenceRecord.objects.create(agent=a, date=datetime.date(2025, 3, 1), status='V')
        AdherenceRecord.objects.create(agent=a, date=datetime.date(2025, 3, 2), status='V')
        _acc, used_before, _rem = vacation_balance(a, datetime.date(2025, 6, 10))
        self.assertEqual(used_before, 2)                       # counted pre-anniversary
        acc_after, used_after, rem_after = vacation_balance(a, datetime.date(2025, 6, 20))
        self.assertEqual(used_after, 0)                        # reset on the new anniversary
        self.assertEqual(rem_after, Decimal(acc_after))        # full days — leftover NOT carried


class NominaTuesdayEditTests(TestCase):
    """Batch 2 — mid-week touch-up tools: additive spiff add, manual extra hours,
    and Hours Worked now INCLUDING holiday hours."""

    def setUp(self):
        from nomina.models import NominaWeek
        _make_agent('tue_super', is_super_admin=True)
        self.client.login(username='tue_super', password='x')
        self.a = _make_infinity('tueagent', '4100')          # hourly_rate defaults to 62.50
        self.ws = get_week_start()
        NominaWeek.objects.create(week_start=self.ws, spiff_fx_rate=Decimal('18'))

    def _add_more(self, key, agent, amount):
        url = reverse('nomina:input_type', args=[key]) + f'?week_start={self.ws.isoformat()}'
        return self.client.post(url, {'add_more_agent': agent.pk, 'add_more_amount': amount}, follow=True)

    def test_spiff_add_more_accumulates(self):
        from nomina.models import WeeklyPayInput
        self._add_more('spiff', self.a, '5')
        self._add_more('spiff', self.a, '20')
        wi = WeeklyPayInput.objects.get(agent=self.a, week_start=self.ws)
        self.assertEqual(wi.spiff_usd, Decimal('25.00'))     # 5 + 20, accumulated not replaced

    def test_extra_hours_add_more_accumulates_and_pays(self):
        import datetime
        from nomina.models import WeeklyPayInput
        from nomina.views import _agent_nomina_data
        self._add_more('hours', self.a, '2')
        self._add_more('hours', self.a, '3')
        wi = WeeklyPayInput.objects.get(agent=self.a, week_start=self.ws)
        self.assertEqual(wi.extra_hours, Decimal('5.00'))
        week = [self.ws + datetime.timedelta(days=i) for i in range(7)]
        rows, _ = _agent_nomina_data(self.ws, week)
        r = next(x for x in rows if x['agent'].pk == self.a.pk)
        self.assertEqual(r['worked_hrs'], Decimal('5'))      # 0 login + 5 manual
        self.assertEqual(r['total_hrs'], Decimal('5'))
        self.assertEqual(r['base_pay'], Decimal('312.50'))   # 5 × 62.50 folded into Pay (48)

    def test_add_more_rejects_out_of_scope_agent(self):
        from nomina.models import WeeklyPayInput
        outsider = _make_agent('outsider', role='agent', role_type='regular_agent')
        outsider.employer = 'LCC'; outsider.save()        # non-Infinity → not on the Agent Nómina
        self._add_more('spiff', outsider, '50')
        self.assertFalse(WeeklyPayInput.objects.filter(agent=outsider).exists())

    def test_hours_worked_includes_holiday(self):
        import datetime
        from nomina.models import Holiday
        from nomina.views import _agent_nomina_data
        from adherence.models import DailyUpload, DailyAgentHours
        from scheduling.models import Five9Profile
        Five9Profile.objects.create(agent=self.a, five9_username='tuef9', billable=True, is_primary=True)
        week = [self.ws + datetime.timedelta(days=i) for i in range(7)]
        holiday = week[2]
        Holiday.objects.create(date=holiday, name='H')
        up, _ = DailyUpload.objects.get_or_create(date=holiday)
        DailyAgentHours.objects.create(upload=up, agent=self.a, five9_username='tuef9',
                                       login_seconds=8 * 3600, not_ready_seconds=0)
        rows, _ = _agent_nomina_data(self.ws, week)
        r = next(x for x in rows if x['agent'].pk == self.a.pk)
        self.assertEqual(r['worked_hrs'], Decimal('8'))      # holiday hours stay INSIDE hours worked
        self.assertEqual(r['holiday_hrs'], Decimal('8'))     # and shown in the Holiday column


class NominaAdminPenaltyTests(TestCase):
    """Batch 3 — admin bonus penalty engine (design #4) + the deduction applied on
    the Admin Nómina. GUIDE: the engine recommends, the coder enters the final %."""

    def _admin(self, username, bonus='1000'):
        u = User.objects.create_user(username, password='x', first_name=username.title())
        return Agent.objects.create(
            user=u, role='admin', role_type='supervisor', agent_name=username,
            status='active', employer='Infinity', is_official_admin=True,
            admin_bonus_mxn=Decimal(bonus), hourly_rate=Decimal('80'))

    def _rec(self, agent, day, status):
        from adherence.models import AdherenceRecord
        AdherenceRecord.objects.update_or_create(agent=agent, date=day, defaults={'status': status})

    def test_single_tardy_same_week_is_10(self):
        import datetime
        from nomina.views import admin_bonus_penalty
        a = self._admin('pen1'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'T')
        self.assertEqual(admin_bonus_penalty(a, ws)['pct'], Decimal('10'))

    def _pct(self, agent, ws):
        from nomina.views import admin_bonus_penalty
        return admin_bonus_penalty(agent, ws)['pct']

    def test_tardy_and_incomplete_stack_to_20(self):
        # THE reported bug: separate tardy + incomplete tracks each add 10% = 20%.
        import datetime
        a = self._admin('pen2'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'T')
        self._rec(a, ws + datetime.timedelta(days=2), 'I')
        self.assertEqual(self._pct(a, ws), Decimal('20'))

    def test_single_t_plus_i_day_is_20(self):
        # One T+I day counts on BOTH tracks → 10% + 10% = 20%.
        import datetime
        a = self._admin('pen2b'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'T+I')
        self.assertEqual(self._pct(a, ws), Decimal('20'))

    def test_single_incomplete_is_10(self):
        import datetime
        a = self._admin('pen2c'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'I')
        self.assertEqual(self._pct(a, ws), Decimal('10'))

    def test_two_tardies_same_week_is_30(self):
        import datetime
        a = self._admin('pen2d'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'T')
        self._rec(a, ws + datetime.timedelta(days=2), 'T')
        self.assertEqual(self._pct(a, ws), Decimal('30'))

    def test_two_tardies_one_incomplete_is_40(self):
        # Tardy track (2 → 30%) + Incomplete track (1 → 10%) = 40%.
        import datetime
        a = self._admin('pen2e'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'T')
        self._rec(a, ws + datetime.timedelta(days=2), 'T')
        self._rec(a, ws + datetime.timedelta(days=3), 'I')
        self.assertEqual(self._pct(a, ws), Decimal('40'))

    def test_tardy_two_weeks_running_is_30(self):
        import datetime
        a = self._admin('pen2f'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'T')
        self._rec(a, ws - datetime.timedelta(days=6), 'T')
        self.assertEqual(self._pct(a, ws), Decimal('30'))

    def test_tardy_three_weeks_running_is_60(self):
        import datetime
        a = self._admin('pen2g'); ws = get_week_start()
        for wk in (0, 1, 2):
            self._rec(a, ws + datetime.timedelta(days=1) - datetime.timedelta(days=7 * wk), 'T')
        self.assertEqual(self._pct(a, ws), Decimal('60'))

    def test_tardy_four_weeks_running_is_100(self):
        import datetime
        a = self._admin('pen2h'); ws = get_week_start()
        for wk in (0, 1, 2, 3):
            self._rec(a, ws + datetime.timedelta(days=1) - datetime.timedelta(days=7 * wk), 'T')
        self.assertEqual(self._pct(a, ws), Decimal('100'))

    def test_tracks_recur_independently(self):
        # Tardy only this week (10%) + incomplete two weeks running (30%) = 40%.
        import datetime
        a = self._admin('pen2i'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'T')
        self._rec(a, ws + datetime.timedelta(days=2), 'I')
        self._rec(a, ws - datetime.timedelta(days=6), 'I')
        self.assertEqual(self._pct(a, ws), Decimal('40'))

    def test_recurrence_breaks_on_gap(self):
        # Tardy this week, none last week, tardy 2 weeks ago → run resets to 1 → 10%.
        import datetime
        a = self._admin('pen2j'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'T')
        self._rec(a, ws - datetime.timedelta(days=13), 'T')
        self.assertEqual(self._pct(a, ws), Decimal('10'))

    def test_five_tardies_clamps_to_100(self):
        import datetime
        a = self._admin('pen2k'); ws = get_week_start()
        for i in range(1, 6):
            self._rec(a, ws + datetime.timedelta(days=i), 'T')
        self.assertEqual(self._pct(a, ws), Decimal('100'))

    def test_ncns_and_suspension_are_100(self):
        import datetime
        for uname, st in (('pen2l', 'NCNS'), ('pen2m', 'S')):
            a = self._admin(uname); ws = get_week_start()
            self._rec(a, ws + datetime.timedelta(days=1), st)
            self.assertEqual(self._pct(a, ws), Decimal('100'))

    def test_two_issues_same_week_is_50(self):
        import datetime
        from nomina.views import admin_bonus_penalty
        a = self._admin('pen2n'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'Issues')
        self._rec(a, ws + datetime.timedelta(days=2), 'Issues')
        reco = admin_bonus_penalty(a, ws)
        self.assertEqual(reco['pct'], Decimal('50'))
        self.assertIn('4', reco['hours_note'])           # pay 8 first day, 4 second

    def test_issues_two_weeks_running_is_50(self):
        import datetime
        a = self._admin('pen2o'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'Issues')
        self._rec(a, ws - datetime.timedelta(days=6), 'Issues')
        self.assertEqual(self._pct(a, ws), Decimal('50'))

    def test_issues_plus_tardy_stacks_to_35(self):
        import datetime
        a = self._admin('pen2p'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'Issues')   # 25
        self._rec(a, ws + datetime.timedelta(days=2), 'T')        # 10
        self.assertEqual(self._pct(a, ws), Decimal('35'))

    def test_t_plus_vto_counts_as_tardy(self):
        import datetime
        a = self._admin('pen2q'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'T+VTO')
        self.assertEqual(self._pct(a, ws), Decimal('10'))

    def test_everything_stacks_and_caps_at_100(self):
        import datetime
        a = self._admin('pen2r'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'T')        # 10
        self._rec(a, ws + datetime.timedelta(days=2), 'I')        # 10
        self._rec(a, ws + datetime.timedelta(days=3), 'Issues')   # 25
        self._rec(a, ws + datetime.timedelta(days=4), 'Absent')   # 100 → cap
        self.assertEqual(self._pct(a, ws), Decimal('100'))

    def test_clean_week_is_zero(self):
        import datetime
        a = self._admin('pen2s'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'P')
        self.assertEqual(self._pct(a, ws), Decimal('0'))

    # ── Scenarios surfaced by the adversarial audit (all correct; locked in) ──────
    def test_two_t_plus_i_days_is_60(self):
        # Each T+I hits BOTH tracks: 2 tardies (30%) + 2 incompletes (30%) = 60%.
        import datetime
        a = self._admin('pen3a'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'T+I')
        self._rec(a, ws + datetime.timedelta(days=2), 'T+I')
        self.assertEqual(self._pct(a, ws), Decimal('60'))

    def test_two_incompletes_same_week_is_30(self):
        import datetime
        a = self._admin('pen3b'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'I')
        self._rec(a, ws + datetime.timedelta(days=2), 'I')
        self.assertEqual(self._pct(a, ws), Decimal('30'))

    def test_incomplete_two_weeks_running_is_30(self):
        import datetime
        a = self._admin('pen3c'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'I')
        self._rec(a, ws - datetime.timedelta(days=6), 'I')
        self.assertEqual(self._pct(a, ws), Decimal('30'))

    def test_recurring_t_plus_i_drives_both_runs_to_60(self):
        # T+I this week + T+I last week → tardy run2 (30%) + incomplete run2 (30%).
        import datetime
        a = self._admin('pen3d'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'T+I')
        self._rec(a, ws - datetime.timedelta(days=6), 'T+I')
        self.assertEqual(self._pct(a, ws), Decimal('60'))

    def test_per_track_selective_gap(self):
        # wk-2 = I, wk-1 = T, wk-0 = T+I → tardy run2 (30, wk-1 had T) + incomplete
        # run1 (10, wk-1 had NO incomplete so its run reset) = 40%.
        import datetime
        a = self._admin('pen3e'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'T+I')
        self._rec(a, ws - datetime.timedelta(days=6), 'T')
        self._rec(a, ws - datetime.timedelta(days=13), 'I')
        self.assertEqual(self._pct(a, ws), Decimal('40'))

    def test_override_participates_in_run(self):
        # A saved T last week + an unsaved (override) T this week → run2 count1 = 30%.
        import datetime
        from nomina.views import admin_bonus_penalty
        a = self._admin('pen3f'); ws = get_week_start()
        self._rec(a, ws - datetime.timedelta(days=6), 'T')
        reco = admin_bonus_penalty(a, ws, override=(ws + datetime.timedelta(days=1), 'T'))
        self.assertEqual(reco['pct'], Decimal('30'))

    def test_issues_three_same_week_is_100_with_hours(self):
        import datetime
        from nomina.views import admin_bonus_penalty
        a = self._admin('pen3g'); ws = get_week_start()
        for i in (1, 2, 3):
            self._rec(a, ws + datetime.timedelta(days=i), 'Issues')
        reco = admin_bonus_penalty(a, ws)
        self.assertEqual(reco['pct'], Decimal('100'))
        self.assertIn('0 hrs day 3', reco['hours_note'])

    def test_issues_recurrence_breaks_on_gap(self):
        # Issue this week + issue 2 weeks ago, clean last week → run resets to 1 → 25%.
        import datetime
        a = self._admin('pen3h'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'Issues')
        self._rec(a, ws - datetime.timedelta(days=13), 'Issues')
        self.assertEqual(self._pct(a, ws), Decimal('25'))

    def test_pure_track_stacking_caps_at_100(self):
        # 3 tardies (60%) + 3 incompletes (60%) = 120 → capped 100, no full-penalty.
        import datetime
        a = self._admin('pen3i'); ws = get_week_start()
        for i in (1, 2, 3):
            self._rec(a, ws + datetime.timedelta(days=i), 'T')
        for i in (4, 5, 6):
            self._rec(a, ws + datetime.timedelta(days=i), 'I')
        self.assertEqual(self._pct(a, ws), Decimal('100'))

    def test_multiple_full_penalty_statuses_list_both(self):
        import datetime
        from nomina.views import admin_bonus_penalty
        a = self._admin('pen3j'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'Absent')
        self._rec(a, ws + datetime.timedelta(days=2), 'NCNS')
        reco = admin_bonus_penalty(a, ws)
        self.assertEqual(reco['pct'], Decimal('100'))
        joined = ' '.join(reco['reasons'])
        self.assertIn('Absent', joined)
        self.assertIn('NCNS', joined)

    def test_prior_week_penalty_does_not_leak(self):
        # An Absent (and a tardy) only in a PRIOR week must not penalize a clean week.
        import datetime
        a = self._admin('pen3k'); ws = get_week_start()
        self._rec(a, ws - datetime.timedelta(days=6), 'Absent')
        self._rec(a, ws - datetime.timedelta(days=5), 'T')
        self.assertEqual(self._pct(a, ws), Decimal('0'))

    def test_non_penalty_statuses_are_zero(self):
        # VTO (a substring of T+VTO/P+VTO) and other benign statuses contribute 0.
        import datetime
        a = self._admin('pen3l'); ws = get_week_start()
        for i, st in enumerate(['VTO', 'OT', 'MUT', 'P+VTO', 'V', 'Holiday'], start=1):
            self._rec(a, ws + datetime.timedelta(days=i), st)
        self.assertEqual(self._pct(a, ws), Decimal('0'))

    def test_save_deduction_clamps_out_of_range(self):
        import datetime, json
        from nomina.models import AdminBonusDeduction
        _make_agent('penclampsuper', is_super_admin=True)
        self.client.login(username='penclampsuper', password='x')
        a = self._admin('pen3m'); ws = get_week_start()
        for raw, expect in (('150', Decimal('100')), ('-5', Decimal('0'))):
            self.client.post(reverse('save_admin_deduction'),
                             data=json.dumps({'agent_id': a.pk, 'week': ws.isoformat(), 'pct': raw}),
                             content_type='application/json')
            self.assertEqual(AdminBonusDeduction.objects.get(agent=a, week_start=ws).deduction_pct, expect)

    def test_recurrence_escalates(self):
        import datetime
        from nomina.views import admin_bonus_penalty
        a = self._admin('pen3'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'T')     # this week: 1 incident
        self._rec(a, ws - datetime.timedelta(days=6), 'T')     # last week: 1 incident
        self.assertEqual(admin_bonus_penalty(a, ws)['pct'], Decimal('30'))   # run 2, count 1 → 30

    def test_absent_is_100(self):
        import datetime
        from nomina.views import admin_bonus_penalty
        a = self._admin('pen4'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'Absent')
        self.assertEqual(admin_bonus_penalty(a, ws)['pct'], Decimal('100'))

    def test_issues_gives_pct_and_hours_note(self):
        import datetime
        from nomina.views import admin_bonus_penalty
        a = self._admin('pen5'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'Issues')
        reco = admin_bonus_penalty(a, ws)
        self.assertEqual(reco['pct'], Decimal('25'))
        self.assertIn('8', reco['hours_note'])                 # pay 8 hrs for a first issue

    def test_penalty_stacks_and_caps_at_100(self):
        import datetime
        from nomina.views import admin_bonus_penalty
        a = self._admin('pen6'); ws = get_week_start()
        self._rec(a, ws + datetime.timedelta(days=1), 'Absent')  # 100
        self._rec(a, ws + datetime.timedelta(days=2), 'T')       # +10 → capped at 100
        self.assertEqual(admin_bonus_penalty(a, ws)['pct'], Decimal('100'))

    def test_admin_nomina_penalty_is_noted_not_applied(self):
        # New model: the sheet shows the FULL bonus; the penalty (and vacation) land in
        # the corrected value + Note, nothing modified in place.
        import datetime
        from nomina.views import _admin_nomina_data
        from nomina.models import AdminBonusDeduction
        a = self._admin('pen7'); ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        gross = next(r for r in _admin_nomina_data(ws, week)[0] if r['agent'].pk == a.pk)['admin_bonus']
        self.assertGreater(gross, 0)
        AdminBonusDeduction.objects.create(agent=a, week_start=ws, deduction_pct=Decimal('50'))
        r2 = next(r for r in _admin_nomina_data(ws, week)[0] if r['agent'].pk == a.pk)
        self.assertEqual(r2['admin_bonus'], gross)                                     # sheet still full
        self.assertEqual(r2['bonus_ded_pct'], Decimal('50'))
        self.assertEqual(r2['admin_bonus_corrected'], (gross * Decimal('0.5')).quantize(Decimal('0.01')))
        self.assertIn('Bonus should be', r2['note'])

    def test_reco_override_reflects_unsaved_status(self):
        # Race-proof: the recommendation reflects the just-set cell even before its
        # save lands (the bug: reco read stale data → 0% for a real NCNS).
        import datetime
        from nomina.views import admin_bonus_penalty
        a = self._admin('pen8'); ws = get_week_start()
        day = ws + datetime.timedelta(days=1)
        self.assertEqual(admin_bonus_penalty(a, ws)['pct'], Decimal('0'))                     # nothing saved
        self.assertEqual(admin_bonus_penalty(a, ws, override=(day, 'NCNS'))['pct'], Decimal('100'))
        # An override that clears the status is reflected too.
        self._rec(a, day, 'NCNS')
        self.assertEqual(admin_bonus_penalty(a, ws)['pct'], Decimal('100'))
        self.assertEqual(admin_bonus_penalty(a, ws, override=(day, ''))['pct'], Decimal('0'))

    def test_reco_endpoint_and_save_deduction(self):
        import datetime, json
        from nomina.models import AdminBonusDeduction
        _make_agent('pensuper', is_super_admin=True)
        self.client.login(username='pensuper', password='x')
        a = self._admin('pen9'); ws = get_week_start()
        day = (ws + datetime.timedelta(days=1)).isoformat()
        resp = self.client.get(reverse('admin_penalty_reco'),
                               {'agent': a.pk, 'week': ws.isoformat(), 'date': day, 'status': 'NCNS'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['pct'], '100')                    # override reflected over HTTP
        resp = self.client.post(reverse('save_admin_deduction'),
                                data=json.dumps({'agent_id': a.pk, 'week': ws.isoformat(), 'pct': '40'}),
                                content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AdminBonusDeduction.objects.get(agent=a, week_start=ws).deduction_pct, Decimal('40'))


class NominaYoursMineTests(TestCase):
    """Batch 4 — the Agent export splits into a raw 'Yours' sheet (before edits) and a
    corrected 'Mine' sheet (overrides, net LPO, vacation + manual hours folded in)."""

    def test_yours_is_raw_mine_is_corrected(self):
        import datetime
        from nomina.views import _agent_nomina_data
        from nomina.models import WeeklyPayInput, NominaOverride
        from adherence.models import AdherenceRecord
        a = _make_infinity('ymagent', '5000')                 # rate 62.50
        ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        WeeklyPayInput.objects.create(agent=a, week_start=ws, lpo=Decimal('1000'), extra_hours=Decimal('5'))
        AdherenceRecord.objects.update_or_create(agent=a, date=week[1], defaults={'status': 'V'})   # flat 8h
        NominaOverride.objects.create(agent=a, week_start=ws, field='base_pay', value=Decimal('999'))
        NominaOverride.objects.create(agent=a, week_start=ws, field='net_lpo', value=Decimal('800'))

        yours = next(r for r in _agent_nomina_data(ws, week, corrected=False)[0] if r['agent'].pk == a.pk)
        mine = next(r for r in _agent_nomina_data(ws, week, corrected=True)[0] if r['agent'].pk == a.pk)

        self.assertEqual(yours['net_lpo'], Decimal('1000'))   # gross
        self.assertEqual(mine['net_lpo'], Decimal('800'))     # override
        self.assertEqual(yours['total_hrs'], Decimal('0'))    # no vacation / extra folded in
        self.assertEqual(mine['total_hrs'], Decimal('13'))    # 5 extra + 8 vacation
        self.assertEqual(yours['base_pay'], Decimal('0'))     # computed base, no override
        self.assertEqual(mine['base_pay'], Decimal('1811.50'))  # 999 + 5×62.50 + 8×62.50

    def test_note_reports_lpo_and_vacation(self):
        import datetime
        from nomina.views import _agent_nomina_data, _agent_note
        from nomina.models import WeeklyPayInput, NominaOverride
        from adherence.models import AdherenceRecord
        a = _make_infinity('ymnote', '5001')
        ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        WeeklyPayInput.objects.create(agent=a, week_start=ws, lpo=Decimal('1000'))
        NominaOverride.objects.create(agent=a, week_start=ws, field='net_lpo', value=Decimal('800'))
        AdherenceRecord.objects.update_or_create(agent=a, date=week[2], defaults={'status': 'V'})
        yours = next(r for r in _agent_nomina_data(ws, week, corrected=False)[0] if r['agent'].pk == a.pk)
        mine = next(r for r in _agent_nomina_data(ws, week, corrected=True)[0] if r['agent'].pk == a.pk)
        note = _agent_note(yours, mine)
        self.assertIn('LPO should be $800.00', note)
        self.assertIn('1 day of vacation', note)
        self.assertIn('total hours worked should be 8', note)

    def test_no_corrections_no_note(self):
        import datetime
        from nomina.views import _agent_nomina_data, _agent_note
        from nomina.models import WeeklyPayInput
        a = _make_infinity('ymclean', '5002')
        ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        WeeklyPayInput.objects.create(agent=a, week_start=ws, lpo=Decimal('500'))   # no commission, no vac
        yours = next(r for r in _agent_nomina_data(ws, week, corrected=False)[0] if r['agent'].pk == a.pk)
        mine = next(r for r in _agent_nomina_data(ws, week, corrected=True)[0] if r['agent'].pk == a.pk)
        self.assertEqual(_agent_note(yours, mine), '')


class NominaRosterTests(TestCase):
    """Split keys off is_official_admin (non-official → Agent, official → Admin); no
    Infinity person is silently dropped from BOTH sheets."""

    def test_everyone_infinity_shows_no_overlap(self):
        from nomina.views import _unrostered_infinity, _infinity_agents, _admin_agents
        from scheduling.models import Agent
        ws = get_week_start()
        # A sales-type agent: Infinity, NO attendance tracking, NO billable Five9 profile.
        stray = _make_agent('salesagent', role='agent', role_type='regular_agent')
        stray.employer = 'Infinity'; stray.track_attendance = False; stray.save()
        worker = _make_infinity('rworker', '7001')                 # tracked
        offu = User.objects.create_user('roffadmin', password='x', first_name='Off')
        off = Agent.objects.create(user=offu, role='admin', role_type='supervisor', agent_name='roffadmin',
                                   status='active', employer='Infinity', is_official_admin=True)

        agent_ids = {a.pk for a in _infinity_agents(ws)}
        admin_ids = {a.pk for a in _admin_agents(ws)}
        self.assertIn(stray.pk, agent_ids)                 # shows even without tracking/billable
        self.assertIn(worker.pk, agent_ids)
        self.assertIn(off.pk, admin_ids)                   # official admin → Admin nómina
        self.assertFalse(agent_ids & admin_ids)            # no overlap
        # Everyone Infinity is now rostered → nobody flagged as unpaid.
        self.assertNotIn(stray.pk, {a.pk for a in _unrostered_infinity(ws)})


class NominaAdminVacationTests(TestCase):
    """Batch 4b — admin bonus prorated by worked ÷ scheduled days, penalty × proration
    multiply, vacation stated in Notes, and admin overrides applied."""

    def _admin(self, username, bonus='500'):
        u = User.objects.create_user(username, password='x', first_name=username.title())
        return Agent.objects.create(
            user=u, role='admin', role_type='supervisor', agent_name=username,
            status='active', employer='Infinity', is_official_admin=True,
            admin_bonus_mxn=Decimal(bonus), hourly_rate=Decimal('80'))

    def _schedule(self, agent, week, days):
        import datetime
        from scheduling.models import Shift
        for i in days:
            Shift.objects.create(agent=agent, date=week[i], is_off=False,
                                 start_time=datetime.time(9, 0), end_time=datetime.time(17, 0))

    def test_bonus_prorated_by_worked_days(self):
        import datetime
        from nomina.views import _admin_nomina_data
        from adherence.models import AdherenceRecord
        a = self._admin('av1', bonus='500'); ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        self._schedule(a, week, [0, 1, 2, 3, 4])                # scheduled 5 days
        AdherenceRecord.objects.update_or_create(agent=a, date=week[0], defaults={'status': 'V'})  # 1 vacation
        r = next(r for r in _admin_nomina_data(ws, week)[0] if r['agent'].pk == a.pk)
        self.assertEqual(r['admin_bonus'], Decimal('500'))                     # sheet shows FULL
        self.assertEqual(r['admin_bonus_corrected'], Decimal('400.00'))        # 500 × 4/5
        self.assertIn('Bonus should be $400.00', r['note'])
        self.assertIn('1 day of vacation', r['note'])

    def test_proration_and_penalty_multiply(self):
        import datetime
        from nomina.views import _admin_nomina_data
        from nomina.models import AdminBonusDeduction
        from adherence.models import AdherenceRecord
        a = self._admin('av2', bonus='500'); ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        self._schedule(a, week, [0, 1, 2, 3, 4])
        AdherenceRecord.objects.update_or_create(agent=a, date=week[0], defaults={'status': 'V'})
        AdminBonusDeduction.objects.create(agent=a, week_start=ws, deduction_pct=Decimal('50'))
        r = next(r for r in _admin_nomina_data(ws, week)[0] if r['agent'].pk == a.pk)
        self.assertEqual(r['admin_bonus_corrected'], Decimal('200.00'))        # 500 × 4/5 × (1−50%)
        self.assertIn('−50% penalty', r['note'])
        self.assertIn('1 vacation day', r['note'])

    def test_admin_override_applied_in_place(self):
        import datetime
        from nomina.views import _admin_nomina_data
        from nomina.models import NominaOverride
        a = self._admin('av3', bonus='500'); ws = get_week_start()
        week = [ws + datetime.timedelta(days=i) for i in range(7)]
        NominaOverride.objects.create(agent=a, week_start=ws, field='admin_bonus', value=Decimal('777'))
        NominaOverride.objects.create(agent=a, week_start=ws, field='base_pay', value=Decimal('1234'))
        r = next(r for r in _admin_nomina_data(ws, week)[0] if r['agent'].pk == a.pk)
        self.assertEqual(r['admin_bonus'], Decimal('777'))     # override replaces the gross bonus
        self.assertEqual(r['base_pay'], Decimal('1234'))


class NominaFinalizeTests(TestCase):
    """Batch 5 — finalizing a week snapshots what was paid; numbers then never recompute
    and the week's inputs are locked (permanent, no un-lock)."""

    def setUp(self):
        from nomina.models import NominaWeek
        _make_agent('fin_super', is_super_admin=True)
        self.client.login(username='fin_super', password='x')
        self.a = _make_infinity('finagent', '8000')
        self.ws = get_week_start()
        NominaWeek.objects.create(week_start=self.ws, spiff_fx_rate=Decimal('18'))

    def _finalize(self):
        return self.client.post(reverse('nomina:finalize') + f'?week_start={self.ws.isoformat()}',
                                {'week_start': self.ws.isoformat()}, follow=True)

    def test_finalize_freezes_numbers(self):
        from nomina.models import WeeklyPayInput, PayrollRun
        WeeklyPayInput.objects.create(agent=self.a, week_start=self.ws, lpo=Decimal('1000'))
        self.assertEqual(self._finalize().status_code, 200)
        run = PayrollRun.objects.get(week_start=self.ws)
        frozen = next(r for r in run.agent_rows if r['agent_pk'] == self.a.pk)
        self.assertEqual(frozen['net_lpo'], 1000.0)                 # snapshot captured it
        # Change the underlying input — the frozen snapshot must NOT move.
        WeeklyPayInput.objects.filter(agent=self.a, week_start=self.ws).update(lpo=Decimal('5000'))
        run.refresh_from_db()
        frozen2 = next(r for r in run.agent_rows if r['agent_pk'] == self.a.pk)
        self.assertEqual(frozen2['net_lpo'], 1000.0)               # still what was paid
        resp = self.client.get(reverse('nomina:agent_nomina') + f'?week_start={self.ws.isoformat()}')
        self.assertContains(resp, 'Finalized')

    def test_inputs_locked_after_finalize(self):
        from nomina.models import PayrollRun, WeeklyPayInput
        PayrollRun.objects.create(week_start=self.ws)              # mark finalized
        url = reverse('nomina:input_type', args=['spiff']) + f'?week_start={self.ws.isoformat()}'
        resp = self.client.post(url, {'add_more_agent': self.a.pk, 'add_more_amount': '50'}, follow=True)
        self.assertFalse(WeeklyPayInput.objects.filter(
            agent=self.a, week_start=self.ws, spiff_usd__gt=0).exists())   # edit blocked
        self.assertContains(resp, 'finalized (locked)')                    # lock banner shown

    def test_finalize_is_permanent_no_double(self):
        from nomina.models import PayrollRun
        self._finalize()
        self._finalize()                                          # second is a no-op
        self.assertEqual(PayrollRun.objects.filter(week_start=self.ws).count(), 1)


class NominaAuditFixTests(TestCase):
    """Fixes from the pre-go-live money-math audit: loan-repayment reconciliation
    warning, and holiday worked/not-worked mutual exclusion."""

    def test_uncredited_loan_repayment_flagged(self):
        import datetime
        from nomina.models import Loan
        from nomina.views import _admin_nomina_data
        u = User.objects.create_user('uncadmin', password='x', first_name='Unc')
        a = Agent.objects.create(user=u, role='admin', role_type='supervisor', agent_name='uncadmin',
                                 status='active', employer='Infinity', is_official_admin=True,
                                 admin_bonus_mxn=Decimal('0'), hourly_rate=Decimal('80'))
        ws = get_week_start(); week = [ws + datetime.timedelta(days=i) for i in range(7)]
        Loan.objects.create(agent=a, principal=Decimal('1000'), term_weeks=1,
                            rate=Decimal('1.25'), start_week=ws, granted_by=None)   # no manager
        _rows, tot = _admin_nomina_data(ws, week)
        self.assertEqual(tot['uncredited_loans'], 1)               # borrower deducted, credit lands nowhere
        self.assertEqual(tot['uncredited_repay'], Decimal('1250.00'))

    def test_holiday_notworked_excludes_worked_hours(self):
        import datetime
        from nomina.models import Holiday
        from nomina.views import _agent_nomina_data
        from adherence.models import AdherenceRecord, DailyUpload, DailyAgentHours
        from scheduling.models import Five9Profile, Shift
        a = _make_infinity('holx', '9300')
        Five9Profile.objects.create(agent=a, five9_username='holxf9', billable=True, is_primary=True)
        ws = get_week_start(); week = [ws + datetime.timedelta(days=i) for i in range(7)]
        hol = week[2]
        Holiday.objects.create(date=hol, name='H')
        Shift.objects.create(agent=a, date=hol, is_off=False,
                             start_time=datetime.time(9, 0), end_time=datetime.time(17, 0))   # 8h scheduled
        AdherenceRecord.objects.update_or_create(agent=a, date=hol, defaults={'status': 'Holiday'})
        up, _ = DailyUpload.objects.get_or_create(date=hol)
        DailyAgentHours.objects.create(upload=up, agent=a, five9_username='holxf9',
                                       login_seconds=2 * 3600, not_ready_seconds=0)   # stray login
        rows, _ = _agent_nomina_data(ws, week)
        r = next(x for x in rows if x['agent'].pk == a.pk)
        self.assertEqual(r['holiday_hrs'], Decimal('0'))          # not paid as WORKED holiday
        self.assertEqual(r['holiday_pay'], Decimal('500.00'))     # 8 sched × 62.50 × 1 only
