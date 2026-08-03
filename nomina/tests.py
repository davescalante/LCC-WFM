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
