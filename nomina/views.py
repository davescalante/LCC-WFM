from datetime import date as date_cls, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from wfm.utils import get_week_start, parse_week_param
from .access import loan_access_required, nomina_access_required
from .models import (
    BreakAbuseIncident, Holiday, Loan, NominaOverride, NominaWeek,
    WeeklyPayInput, WelcomeBonusEnrollment,
)

# The manual paste-in fields, in display order. (label, field, is_deduction)
# Per-type input modules. Each maps to one WeeklyPayInput field. `aggregate`
# sums all matched rows per person (e.g. a Comedor POS export); otherwise one
# value per person. `unit` is display only (USD spiffs convert at the week rate).
INPUT_TYPES = [
    {'key': 'lpo', 'field': 'lpo', 'label': 'LPO', 'unit': 'MXN', 'deduction': False,
     'aggregate': False, 'match': 'auto',
     'desc': 'Sales commission. Upload with columns Username + Amount.'},
    {'key': 'spiff', 'field': 'spiff_usd', 'label': 'Spiffs', 'unit': 'USD', 'deduction': False,
     'aggregate': False, 'match': 'auto',
     'desc': 'Spiffs in USD (converted at the week rate). Columns: Username + Amount/Dollars.'},
    {'key': 'referral', 'field': 'referral', 'label': 'Referral', 'unit': 'MXN', 'deduction': False,
     'aggregate': False, 'match': 'auto', 'desc': 'Referral bonus. Columns: Username + Amount.'},
    {'key': 'killqa', 'field': 'kill_team_qa', 'label': 'Kill Team QA', 'unit': 'MXN', 'deduction': False,
     'aggregate': False, 'match': 'auto', 'desc': 'Kill Team / QA bonus. Columns: Username + Amount.'},
    {'key': 'comedor', 'field': 'comedor', 'label': 'Comedor', 'unit': 'MXN', 'deduction': True,
     'aggregate': True, 'match': 'auto',
     'desc': 'Cafeteria POS export — sums all charges per person. Columns: Employee ID + Price.'},
    {'key': 'transportation', 'field': 'transportation', 'label': 'Transportation', 'unit': 'MXN', 'deduction': True,
     'aggregate': False, 'match': 'auto', 'desc': 'Transportation deduction. Columns: Username + Amount.'},
]
INPUT_TYPE_BY_KEY = {t['key']: t for t in INPUT_TYPES}


def _week(request):
    raw = request.GET.get('week_start') or request.GET.get('week') or request.POST.get('week_start')
    week_start = parse_week_param(raw) or get_week_start()
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    return week_start, week_dates


def _dec(val):
    try:
        return Decimal(str(val).strip() or '0')
    except (InvalidOperation, ValueError):
        return Decimal('0')


def _pay_window(week_start):
    return Q(status='active') | Q(
        status='inactive', separations__status='finalized',
        separations__remove_from_adherence_date__gt=week_start,
    )


def _infinity_agents(week_start):
    from scheduling.models import Agent
    return list(
        Agent.objects.filter(_pay_window(week_start))
        .filter(Q(track_attendance=True) | Q(five9_profiles__billable=True))
        .filter(employer='Infinity')       # INFINITY only — LCC excluded
        .exclude(is_official_admin=True)   # admins go on the Admin Nómina
        .distinct()
        .select_related('user', 'supervisor__user')
        .prefetch_related('five9_profiles')
        .order_by('user__last_name', 'user__first_name')
    )


def _admin_agents(week_start):
    from scheduling.models import Agent
    return list(
        Agent.objects.filter(_pay_window(week_start))
        .filter(is_official_admin=True)
        .filter(employer='Infinity')
        .distinct()
        .select_related('user', 'supervisor__user')
        .prefetch_related('five9_profiles')
        .order_by('user__last_name', 'user__first_name')
    )


def _holiday_worked_hours(agents, holiday_dates):
    """{agent_id: billable login hours worked on the given holiday dates}."""
    if not agents or not holiday_dates:
        return {}
    from adherence.models import DailyAgentHours
    from wfm.utils import get_billable_username_map
    bmap, _ = get_billable_username_map([a.pk for a in agents])
    out = {}
    for r in DailyAgentHours.objects.filter(
        upload__date__in=holiday_dates, agent__in=agents
    ).values('agent_id', 'five9_username', 'login_seconds'):
        aid = r['agent_id']
        if aid is None:
            continue
        bn = bmap.get(aid)
        if bn is None or r['five9_username'].strip().lower() in bn:
            out[aid] = out.get(aid, Decimal('0')) + Decimal(str(r['login_seconds'])) / Decimal('3600')
    return out


def _nav(week_start, week_dates):
    return {
        'week_start': week_start,
        'week_end': week_dates[-1],
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
    }


@login_required
@nomina_access_required
def dashboard(request):
    """Nómina landing page — super admin only."""
    week_start, week_dates = _week(request)
    ctx = _nav(week_start, week_dates)
    ctx['today'] = timezone.localdate()
    return render(request, 'nomina/dashboard.html', ctx)


@login_required
@nomina_access_required
def inputs(request):
    """Inputs hub — one module per input type + the week's spiff FX rate."""
    week_start, week_dates = _week(request)
    nweek, _ = NominaWeek.objects.get_or_create(week_start=week_start)

    if request.method == 'POST' and 'spiff_fx_rate' in request.POST:
        nweek.spiff_fx_rate = _dec(request.POST.get('spiff_fx_rate') or nweek.spiff_fx_rate)
        nweek.save()
        messages.success(request, "Spiff rate saved.")
        return redirect(f"{request.path}?week_start={week_start.isoformat()}")

    # Per-type filled count (agents with a non-zero value this week)
    agents = _infinity_agents(week_start)
    existing = {wi.agent_id: wi for wi in WeeklyPayInput.objects.filter(
        agent__in=agents, week_start=week_start)}
    cards = []
    for t in INPUT_TYPES:
        filled = sum(1 for a in agents if existing.get(a.pk) and getattr(existing[a.pk], t['field']))
        cards.append({**t, 'filled': filled})

    ctx = _nav(week_start, week_dates)
    ctx.update({'cards': cards, 'spiff_fx_rate': nweek.spiff_fx_rate})
    return render(request, 'nomina/inputs.html', ctx)


def _read_rows(uploaded):
    """Read an uploaded .csv or .xlsx into (headers, list-of-row-lists)."""
    name = (uploaded.name or '').lower()
    if name.endswith('.csv'):
        import csv, io
        text = uploaded.read().decode('utf-8-sig', errors='replace')
        r = list(csv.reader(io.StringIO(text)))
        return (r[0] if r else []), r[1:]
    import openpyxl, io
    wb = openpyxl.load_workbook(io.BytesIO(uploaded.read()), data_only=True, read_only=True)
    ws = wb.active
    rows = [[c for c in row] for row in ws.iter_rows(values_only=True)]
    return (rows[0] if rows else []), rows[1:]


def _find_col(headers, keywords):
    for i, h in enumerate(headers):
        hl = str(h or '').strip().lower()
        if any(k in hl for k in keywords):
            return i
    return None


@login_required
@nomina_access_required
def input_type(request, key):
    """One input module: upload a file (mapped by headers) or edit manually."""
    t = INPUT_TYPE_BY_KEY.get(key)
    if not t:
        messages.error(request, "Unknown input type.")
        return redirect('nomina:inputs')
    field = t['field']
    week_start, week_dates = _week(request)
    agents = _infinity_agents(week_start)
    by_username = {a.user.username.strip().lower(): a for a in agents}
    by_empid = {(a.employee_id or '').strip(): a for a in agents if a.employee_id}
    unmatched = []

    if request.method == 'POST' and request.FILES.get('file'):
        headers, data = _read_rows(request.FILES['file'])
        user_col = _find_col(headers, ['username', 'user', 'agent', 'login'])
        id_col = _find_col(headers, ['employee id', 'emp id', 'empid', 'employee', 'id'])
        amt_col = _find_col(headers, ['amount', 'total', 'dollar', 'pesos', 'price', 'value', 'monto'])
        if amt_col is None or (user_col is None and id_col is None):
            messages.error(request, "Couldn't find the columns. Need a Username or Employee ID column and an Amount column.")
            return redirect(f"{request.path}?week_start={week_start.isoformat()}")
        totals = {}
        for row in data:
            if amt_col >= len(row):
                continue
            amt = _dec(row[amt_col])
            agent = None
            if user_col is not None and user_col < len(row) and row[user_col]:
                agent = by_username.get(str(row[user_col]).strip().lower())
            if agent is None and id_col is not None and id_col < len(row) and row[id_col] not in (None, ''):
                agent = by_empid.get(str(row[id_col]).strip())
            if agent is None:
                key_shown = row[user_col] if user_col is not None and user_col < len(row) else (row[id_col] if id_col is not None and id_col < len(row) else '?')
                if amt:
                    unmatched.append({'who': key_shown, 'amount': amt})
                continue
            totals[agent.pk] = (totals.get(agent.pk, Decimal('0')) + amt) if t['aggregate'] else amt
        for aid, val in totals.items():
            WeeklyPayInput.objects.update_or_create(
                agent_id=aid, week_start=week_start, defaults={field: val})
        request.session[f'nomina_unmatched_{key}'] = [
            {'who': str(u['who']), 'amount': str(u['amount'])} for u in unmatched]
        messages.success(request, f"Imported {len(totals)} {t['label']} value(s); {len(unmatched)} row(s) unmatched.")
        return redirect(f"{request.path}?week_start={week_start.isoformat()}")

    if request.method == 'POST':  # manual save
        for a in agents:
            WeeklyPayInput.objects.update_or_create(
                agent=a, week_start=week_start,
                defaults={field: _dec(request.POST.get(f'v_{a.pk}'))})
        messages.success(request, f"{t['label']} saved.")
        return redirect(f"{request.path}?week_start={week_start.isoformat()}")

    existing = {wi.agent_id: wi for wi in WeeklyPayInput.objects.filter(
        agent__in=agents, week_start=week_start)}
    rows = []
    for a in agents:
        wi = existing.get(a.pk)
        v = getattr(wi, field) if wi else Decimal('0')
        rows.append({'agent': a, 'name': a.agent_name or a.user.get_full_name() or a.user.username,
                     'username': a.user.username, 'emp': a.employee_id or '',
                     'display': '' if v == 0 else f'{v:.2f}'})
    unmatched = request.session.pop(f'nomina_unmatched_{key}', [])
    ctx = _nav(week_start, week_dates)
    ctx.update({'t': t, 'rows': rows, 'unmatched': unmatched})
    return render(request, 'nomina/input_type.html', ctx)


def _agent_nomina_data(week_start, week_dates):
    """Compute the Agent Nómina rows + totals (shared by the view and the export)."""
    from finance.views import _get_billable_weekly_data
    from finance.models import BillingSettings

    settings = BillingSettings.get_for_week(week_start)
    nweek, _ = NominaWeek.objects.get_or_create(week_start=week_start)
    fx = nweek.spiff_fx_rate

    agents = _infinity_agents(week_start)
    data = _get_billable_weekly_data(agents, week_dates, settings)
    inputs_map = {wi.agent_id: wi for wi in WeeklyPayInput.objects.filter(
        agent__in=agents, week_start=week_start)}
    ba_agents = set(BreakAbuseIncident.objects.filter(
        agent__in=agents, date__in=week_dates).values_list('agent_id', flat=True))
    holiday_dates = list(Holiday.objects.filter(date__in=week_dates).values_list('date', flat=True))
    hol_hours = _holiday_worked_hours(agents, holiday_dates)
    enrolls = {e.agent_id: e for e in WelcomeBonusEnrollment.objects.filter(agent__in=agents)}
    loan_ded = {}
    for ln in Loan.objects.filter(agent__in=agents):
        inst = ln.installment_for_week(week_start)
        if inst:
            loan_ded[ln.agent_id] = loan_ded.get(ln.agent_id, Decimal('0')) + inst
    overrides = {(o.agent_id, o.field): o.value
                 for o in NominaOverride.objects.filter(agent__in=agents, week_start=week_start)}

    def ov(aid, field, computed):
        return overrides.get((aid, field), computed)

    rows = []
    tot_base = tot_bonus = tot_lpo = tot_spiff = tot_hol = tot_sub = tot_ded = tot_total = Decimal('0')
    for a in agents:
        d = data.get(a.pk, {})
        wi = inputs_map.get(a.pk)
        broke = a.pk in ba_agents
        comm_pct = d.get('commission_pct', Decimal('0'))
        rate = d.get('hourly_mxn', Decimal('0'))

        base = ov(a.pk, 'base_pay', d.get('base_pay_mxn', Decimal('0')))
        bonus = ov(a.pk, 'adherence', Decimal('0') if broke else d.get('bonus_mxn', Decimal('0')))
        net_lpo = ov(a.pk, 'net_lpo', ((wi.lpo if wi else Decimal('0')) * (Decimal('1') - comm_pct / Decimal('100'))).quantize(Decimal('0.01')))
        spiff_mxn = ov(a.pk, 'spiff', ((wi.spiff_usd if wi else Decimal('0')) * fx).quantize(Decimal('0.01')))
        # Welcome: enrollment-driven (paid only if they earned an adherence bonus), else the manual input.
        enroll = enrolls.get(a.pk)
        welcome_default = enroll.amount if (enroll and enroll.covers_week(week_start) and bonus > 0) else (wi.welcome if wi else Decimal('0'))
        welcome = ov(a.pk, 'welcome', welcome_default)
        referral = ov(a.pk, 'referral', wi.referral if wi else Decimal('0'))
        kill_qa = ov(a.pk, 'kill_qa', wi.kill_team_qa if wi else Decimal('0'))
        holiday_pay = ov(a.pk, 'holiday', (hol_hours.get(a.pk, Decimal('0')) * rate * 2).quantize(Decimal('0.01')))
        comedor = ov(a.pk, 'comedor', wi.comedor if wi else Decimal('0'))
        transport = ov(a.pk, 'transport', wi.transportation if wi else Decimal('0'))
        loan = ov(a.pk, 'loan', loan_ded.get(a.pk, Decimal('0')))

        subtotal = base + bonus + net_lpo + spiff_mxn + welcome + referral + kill_qa + holiday_pay
        total = subtotal - comedor - transport - loan  # may go negative (G6)

        rows.append({
            'agent': a, 'emp': a.employee_id or '',
            'legal_name': a.user.get_full_name() or a.user.username,
            'username': a.user.username, 'break_abuse': broke,
            'hours': d.get('final_hrs', Decimal('0')), 'rate': rate,
            'base_pay': base, 'adherence_bonus': bonus,
            'net_lpo': net_lpo, 'comm_pct': comm_pct, 'spiff_mxn': spiff_mxn,
            'welcome': welcome, 'referral': referral, 'kill_qa': kill_qa, 'holiday_pay': holiday_pay,
            'subtotal': subtotal, 'comedor': comedor, 'transport': transport, 'loan': loan, 'total': total,
        })
        tot_base += base; tot_bonus += bonus; tot_lpo += net_lpo; tot_spiff += spiff_mxn
        tot_hol += holiday_pay; tot_sub += subtotal; tot_ded += (comedor + transport + loan); tot_total += total

    totals = {'base': tot_base, 'bonus': tot_bonus, 'net_lpo': tot_lpo, 'spiff': tot_spiff,
              'holiday': tot_hol, 'subtotal': tot_sub, 'total': tot_total}
    return rows, totals


@login_required
@nomina_access_required
def agent_nomina(request):
    """Agent Nómina — auto columns (existing engine) + manual inputs + modules
    (break abuse, holiday, welcome, loans) + overrides → subtotal/total."""
    week_start, week_dates = _week(request)
    rows, totals = _agent_nomina_data(week_start, week_dates)
    ctx = _nav(week_start, week_dates)
    ctx.update({'rows': rows, 'totals': totals})
    return render(request, 'nomina/agent_nomina.html', ctx)


# Agent export column order — mirrors the LCC AGENT NOMINA file.
AGENT_EXPORT_COLS = [
    ('EMP', 'emp'), ('Legal name', 'legal_name'), ('User', 'username'),
    ('Hours Worked', 'hours'), ('Holiday Pay', 'holiday_pay'), ('Pay (48)', 'base_pay'),
    ('LPO', 'net_lpo'), ('Referral', 'referral'), ('Welcome Bonus', 'welcome'),
    ('Kill Team QA Bonus', 'kill_qa'), ('Spiff', 'spiff_mxn'), ('Adherence', 'adherence_bonus'),
    ('Sub Total', 'subtotal'), ('Cafeteria', 'comedor'), ('Prestamo', 'loan'),
    (' Transportation', 'transport'), ('Total', 'total'),
]


def _agent_note(r):
    """Auto-note for the 'Yours' sheet (only the two locked triggers)."""
    notes = []
    if r['net_lpo']:
        notes.append(f"LPO should be ${r['net_lpo']:.2f}")
    return " · ".join(notes)


@login_required
@nomina_access_required
def agent_export(request):
    """Export the Agent Nómina: 'Yours' (with a Notes column) + 'Mine' sheets."""
    import openpyxl
    from openpyxl.styles import Font
    week_start, week_dates = _week(request)
    rows, _ = _agent_nomina_data(week_start, week_dates)

    wb = openpyxl.Workbook()
    for i, sheet in enumerate(('Yours', 'Mine')):
        ws = wb.active if i == 0 else wb.create_sheet(sheet)
        ws.title = sheet
        headers = [h for h, _ in AGENT_EXPORT_COLS] + (['Notes'] if sheet == 'Yours' else [])
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            row = [r[f] if not isinstance(r[f], type(None)) else '' for _, f in AGENT_EXPORT_COLS]
            # Decimals → float for Excel
            row = [float(v) if hasattr(v, 'quantize') else v for v in row]
            if sheet == 'Yours':
                row.append(_agent_note(r))
            ws.append(row)
    resp = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = f'attachment; filename="LCC AGENT NOMINA {week_start:%m%d%Y}.xlsx"'
    wb.save(resp)
    return resp


@login_required
@nomina_access_required
def break_abuse(request):
    """Log break-abuse incidents. Any incident in a pay week zeroes that agent's
    adherence bonus for the week (applied in the Agent Nómina)."""
    from scheduling.models import Agent
    week_start, week_dates = _week(request)

    if request.method == 'POST':
        if request.POST.get('delete'):
            BreakAbuseIncident.objects.filter(pk=request.POST['delete']).delete()
            messages.success(request, "Incident removed.")
        else:
            agent_id = request.POST.get('agent')
            try:
                d = date_cls.fromisoformat(request.POST.get('date'))
            except (ValueError, TypeError):
                d = None
            if agent_id and d:
                BreakAbuseIncident.objects.create(
                    agent_id=agent_id, date=d, note=request.POST.get('note', '').strip())
                messages.success(request, "Break-abuse incident logged.")
        return redirect(f"{request.path}?week_start={week_start.isoformat()}")

    incidents = list(BreakAbuseIncident.objects.filter(
        date__in=week_dates).select_related('agent__user').order_by('date'))
    agents = Agent.objects.filter(status='active').select_related('user').order_by(
        'user__last_name', 'user__first_name')

    ctx = _nav(week_start, week_dates)
    ctx.update({'incidents': incidents, 'agents': agents, 'week_dates': week_dates})
    return render(request, 'nomina/break_abuse.html', ctx)


@login_required
@nomina_access_required
def holidays(request):
    """Manage the company holiday calendar. A holiday worked earns a 2× premium."""
    if request.method == 'POST':
        if request.POST.get('delete'):
            Holiday.objects.filter(pk=request.POST['delete']).delete()
            messages.success(request, "Holiday removed.")
        else:
            try:
                d = date_cls.fromisoformat(request.POST.get('date'))
                Holiday.objects.update_or_create(date=d, defaults={'name': request.POST.get('name', '').strip()})
                messages.success(request, "Holiday saved.")
            except (ValueError, TypeError):
                messages.error(request, "Enter a valid date.")
        return redirect(request.path)

    week_start, week_dates = _week(request)
    ctx = _nav(week_start, week_dates)
    ctx['holidays'] = Holiday.objects.all()
    return render(request, 'nomina/holidays.html', ctx)


@login_required
@nomina_access_required
def exports(request):
    """Download the Agent (Yours/Mine) and Admin (Yours) nómina Excel files."""
    week_start, week_dates = _week(request)
    ctx = _nav(week_start, week_dates)
    return render(request, 'nomina/exports.html', ctx)


@login_required
@loan_access_required
def loans(request):
    """Add loans and view active ones. 1wk ×1.25 / 2wk ×1.35; weekly drawdown.
    Accessible to super admins and users granted `can_manage_loans`."""
    from scheduling.models import Agent
    week_start, week_dates = _week(request)

    if request.method == 'POST':
        if request.POST.get('delete'):
            Loan.objects.filter(pk=request.POST['delete']).delete()
            messages.success(request, "Loan removed.")
        else:
            agent_id = request.POST.get('agent')
            principal = _dec(request.POST.get('principal'))
            term = 2 if request.POST.get('term') == '2' else 1
            if agent_id and principal > 0:
                Loan.objects.create(
                    agent_id=agent_id, principal=principal, term_weeks=term,
                    rate=Decimal('1.35') if term == 2 else Decimal('1.25'),
                    start_week=week_start,
                )
                messages.success(request, "Loan added.")
        return redirect(f"{request.path}?week_start={week_start.isoformat()}")

    loan_list = list(Loan.objects.select_related('agent__user').all())
    for ln in loan_list:
        ln.bal = ln.balance(week_start)
    agents = Agent.objects.filter(status='active').select_related('user').order_by(
        'user__last_name', 'user__first_name')
    ctx = _nav(week_start, week_dates)
    ctx.update({'loans': loan_list, 'agents': agents})
    return render(request, 'nomina/loans.html', ctx)


@login_required
@nomina_access_required
def welcome(request):
    """The welcome-bonus eligibility table. Paid per covered week the agent earns
    an adherence bonus; every calendar week counts toward the term."""
    from scheduling.models import Agent
    week_start, week_dates = _week(request)

    if request.method == 'POST':
        if request.POST.get('delete'):
            WelcomeBonusEnrollment.objects.filter(pk=request.POST['delete']).delete()
            messages.success(request, "Removed.")
        else:
            agent_id = request.POST.get('agent')
            amount = _dec(request.POST.get('amount'))
            num_weeks = int(request.POST.get('num_weeks') or 4)
            if agent_id:
                WelcomeBonusEnrollment.objects.create(
                    agent_id=agent_id, amount=amount, num_weeks=num_weeks, start_week=week_start)
                messages.success(request, "Enrolled.")
        return redirect(f"{request.path}?week_start={week_start.isoformat()}")

    enrolls = list(WelcomeBonusEnrollment.objects.select_related('agent__user').all())
    for e in enrolls:
        e.weeks_in = e.covers_week(week_start)
    agents = Agent.objects.filter(status='active').select_related('user').order_by(
        'user__last_name', 'user__first_name')
    ctx = _nav(week_start, week_dates)
    ctx.update({'enrolls': enrolls, 'agents': agents})
    return render(request, 'nomina/welcome.html', ctx)


# Mexican LFT "Vacaciones Dignas" vacation-day schedule by completed years of service.
def _lft_vacation_days(years):
    if years < 1:
        return 0
    table = {1: 12, 2: 14, 3: 16, 4: 18, 5: 20}
    if years in table:
        return table[years]
    if years <= 10:
        return 22
    return 22 + 2 * ((years - 6) // 5)  # +2 every 5 yrs beyond year 6-10 band


@login_required
@nomina_access_required
def vacation(request):
    """Vacation tracker — tenure-based accrual (LFT), used ('V' days), remaining."""
    from scheduling.models import Agent
    week_start, week_dates = _week(request)
    today = timezone.localdate()
    agents = list(Agent.objects.filter(status='active').select_related('user')
                  .prefetch_related('employment_periods').order_by('user__last_name', 'user__first_name'))

    from adherence.models import AdherenceRecord
    used_map = {}
    for r in AdherenceRecord.objects.filter(
        agent__in=agents, status='V', date__year=today.year).values('agent_id'):
        used_map[r['agent_id']] = used_map.get(r['agent_id'], 0) + 1

    rows = []
    for a in agents:
        starts = [ep.start_date for ep in a.employment_periods.all() if ep.start_date]
        start = min(starts) if starts else a.start_date
        years = 0
        if start:
            years = max(0, today.year - start.year - ((today.month, today.day) < (start.month, start.day)))
        accrued = _lft_vacation_days(years)
        used = used_map.get(a.pk, 0)
        rows.append({'agent': a, 'name': a.agent_name or a.user.get_full_name() or a.user.username,
                     'start': start, 'years': years, 'accrued': accrued, 'used': used,
                     'remaining': accrued - used})
    ctx = _nav(week_start, week_dates)
    ctx['rows'] = rows
    return render(request, 'nomina/vacation.html', ctx)


# Auto columns that can be overridden on the Agent Nómina.
OVERRIDE_FIELDS = [('base_pay', 'Base Pay'), ('adherence', 'Adherence'), ('holiday', 'Holiday')]


@login_required
@nomina_access_required
def overrides(request):
    """Override the auto-computed columns (base pay, adherence, holiday) per agent.
    Leave a cell blank to use the computed value; type a value to override it."""
    from finance.views import _get_billable_weekly_data
    from finance.models import BillingSettings

    week_start, week_dates = _week(request)
    agents = _infinity_agents(week_start)

    if request.method == 'POST':
        for a in agents:
            for field, _label in OVERRIDE_FIELDS:
                raw = (request.POST.get(f'{field}_{a.pk}') or '').strip()
                if raw == '':
                    NominaOverride.objects.filter(agent=a, week_start=week_start, field=field).delete()
                else:
                    NominaOverride.objects.update_or_create(
                        agent=a, week_start=week_start, field=field, defaults={'value': _dec(raw)})
        messages.success(request, "Overrides saved.")
        return redirect(f"{request.path}?week_start={week_start.isoformat()}")

    settings = BillingSettings.get_for_week(week_start)
    data = _get_billable_weekly_data(agents, week_dates, settings)
    ba_agents = set(BreakAbuseIncident.objects.filter(
        agent__in=agents, date__in=week_dates).values_list('agent_id', flat=True))
    holiday_dates = list(Holiday.objects.filter(date__in=week_dates).values_list('date', flat=True))
    hol_hours = _holiday_worked_hours(agents, holiday_dates)
    existing = {(o.agent_id, o.field): o.value
                for o in NominaOverride.objects.filter(agent__in=agents, week_start=week_start)}

    rows = []
    for a in agents:
        d = data.get(a.pk, {})
        rate = d.get('hourly_mxn', Decimal('0'))
        computed = {
            'base_pay': d.get('base_pay_mxn', Decimal('0')),
            'adherence': Decimal('0') if a.pk in ba_agents else d.get('bonus_mxn', Decimal('0')),
            'holiday': (hol_hours.get(a.pk, Decimal('0')) * rate * 2).quantize(Decimal('0.01')),
        }
        cells = []
        for field, label in OVERRIDE_FIELDS:
            ov = existing.get((a.pk, field))
            cells.append({'field': field, 'computed': computed[field],
                          'override': '' if ov is None else f'{ov:.2f}'})
        rows.append({'agent': a, 'name': a.agent_name or a.user.get_full_name() or a.user.username, 'cells': cells})
    ctx = _nav(week_start, week_dates)
    ctx.update({'rows': rows, 'fields': OVERRIDE_FIELDS})
    return render(request, 'nomina/overrides.html', ctx)


def _admin_nomina_data(week_start, week_dates):
    """Compute the Admin Nómina rows + totals (shared by the view and export)."""
    from finance.views import _get_billable_weekly_data
    from finance.models import BillingSettings

    settings = BillingSettings.get_for_week(week_start)
    nweek, _ = NominaWeek.objects.get_or_create(week_start=week_start)
    fx = nweek.spiff_fx_rate

    agents = _admin_agents(week_start)
    data = _get_billable_weekly_data(agents, week_dates, settings)
    inputs_map = {wi.agent_id: wi for wi in WeeklyPayInput.objects.filter(
        agent__in=agents, week_start=week_start)}

    rows = []
    tot_base = tot_bonus = tot_sub = tot_total = Decimal('0')
    for a in agents:
        d = data.get(a.pk, {})
        wi = inputs_map.get(a.pk)
        base = d.get('base_pay_mxn', Decimal('0'))
        admin_bonus = d.get('admin_bonus_mxn', Decimal('0'))

        commissions = wi.lpo if wi else Decimal('0')
        spiffs = ((wi.spiff_usd if wi else Decimal('0')) * fx).quantize(Decimal('0.01'))
        referral = wi.referral if wi else Decimal('0')
        comedor = wi.comedor if wi else Decimal('0')
        transport = wi.transportation if wi else Decimal('0')

        subtotal = base + spiffs + commissions + referral
        total = subtotal + admin_bonus - comedor - transport  # loans/holiday later

        rows.append({
            'agent': a, 'emp': a.employee_id or '',
            'name': a.user.get_full_name() or a.user.username,
            'username': a.user.username,
            'wage': d.get('hourly_mxn', Decimal('0')), 'hours': d.get('final_hrs', Decimal('0')),
            'base_pay': base, 'spiffs': spiffs, 'commissions': commissions,
            'referral': referral, 'subtotal': subtotal, 'admin_bonus': admin_bonus,
            'comedor': comedor, 'transport': transport, 'total': total,
        })
        tot_base += base; tot_bonus += admin_bonus; tot_sub += subtotal; tot_total += total

    totals = {'base': tot_base, 'bonus': tot_bonus, 'subtotal': tot_sub, 'total': tot_total}
    return rows, totals


@login_required
@nomina_access_required
def admin_nomina(request):
    """Admin Nómina — Official Admins (Infinity). Base = hours × admin wage,
    plus the fixed admin bonus, plus admin spiffs/commissions/referral, minus
    comedor/transport."""
    week_start, week_dates = _week(request)
    rows, totals = _admin_nomina_data(week_start, week_dates)
    ctx = _nav(week_start, week_dates)
    ctx.update({'rows': rows, 'totals': totals})
    return render(request, 'nomina/admin_nomina.html', ctx)


ADMIN_EXPORT_COLS = [
    ('ID', 'emp'), ('Nombre', 'name'), ('Admin Wage', 'wage'), ('Hours Worked', 'hours'),
    ('Base Pay', 'base_pay'), ('Spiffs', 'spiffs'), ('Comissions', 'commissions'),
    ('Refferal', 'referral'), ('Subtotal', 'subtotal'), ('Bonus', 'admin_bonus'),
    ('Cafeteria', 'comedor'), (' Transportation', 'transport'), ('Total', 'total'),
]


@login_required
@nomina_access_required
def admin_export(request):
    """Export the Admin Nómina — one 'Yours' sheet (with a Notes column)."""
    import openpyxl
    from openpyxl.styles import Font
    week_start, week_dates = _week(request)
    rows, _ = _admin_nomina_data(week_start, week_dates)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Yours'
    ws.append([h for h, _ in ADMIN_EXPORT_COLS] + ['Notes'])
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        row = [float(r[f]) if hasattr(r[f], 'quantize') else r[f] for _, f in ADMIN_EXPORT_COLS]
        ws.append(row + [''])
    resp = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = f'attachment; filename="LCC ADMIN NOMINA {week_start:%m%d%Y}.xlsx"'
    wb.save(resp)
    return resp
