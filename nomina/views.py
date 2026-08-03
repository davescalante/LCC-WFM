from datetime import date as date_cls, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils import timezone

from wfm.utils import get_week_start, parse_week_param
from .access import nomina_access_required
from .models import BreakAbuseIncident, Holiday, NominaWeek, WeeklyPayInput

# The manual paste-in fields, in display order. (label, field, is_deduction)
INPUT_FIELDS = [
    ('LPO', 'lpo', False),
    ('Spiff (USD)', 'spiff_usd', False),
    ('Welcome', 'welcome', False),
    ('Referral', 'referral', False),
    ('Kill Team QA', 'kill_team_qa', False),
    ('Comedor', 'comedor', True),
    ('Transportation', 'transportation', True),
]


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
    """The paste/entry grid for the manual columns (LPO, spiffs, comedor, …).

    One row per Infinity agent; type or paste values, hit Save. Persists to
    WeeklyPayInput per agent/week and the week's spiff FX rate to NominaWeek.
    """
    week_start, week_dates = _week(request)
    agents = _infinity_agents(week_start)
    nweek, _ = NominaWeek.objects.get_or_create(week_start=week_start)

    if request.method == 'POST':
        nweek.spiff_fx_rate = _dec(request.POST.get('spiff_fx_rate') or nweek.spiff_fx_rate)
        nweek.save()
        for a in agents:
            vals = {f: _dec(request.POST.get(f'{f}_{a.pk}')) for _, f, _ in INPUT_FIELDS}
            WeeklyPayInput.objects.update_or_create(
                agent=a, week_start=week_start, defaults=vals,
            )
        messages.success(request, f"Nómina inputs saved for the week of {week_start:%b %d}.")
        return redirect(f"{request.path}?week_start={week_start.isoformat()}")

    existing = {wi.agent_id: wi for wi in WeeklyPayInput.objects.filter(
        agent__in=agents, week_start=week_start)}
    rows = []
    for a in agents:
        wi = existing.get(a.pk)
        cells = []
        for label, f, is_ded in INPUT_FIELDS:
            v = getattr(wi, f) if wi else Decimal('0')
            cells.append({'field': f, 'display': '' if v == 0 else f'{v:.2f}'})
        rows.append({
            'agent': a,
            'name': a.agent_name or a.user.get_full_name() or a.user.username,
            'username': a.user.username,
            'cells': cells,
        })

    ctx = _nav(week_start, week_dates)
    ctx.update({'rows': rows, 'fields': INPUT_FIELDS, 'spiff_fx_rate': nweek.spiff_fx_rate})
    return render(request, 'nomina/inputs.html', ctx)


@login_required
@nomina_access_required
def agent_nomina(request):
    """Agent Nómina — auto-computed columns (from the existing engine) combined
    with the manual paste inputs, into a live subtotal/total. Infinity only."""
    from finance.views import _get_billable_weekly_data
    from finance.models import BillingSettings

    week_start, week_dates = _week(request)
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

    rows = []
    tot_base = tot_bonus = tot_lpo = tot_spiff = tot_hol = tot_sub = tot_total = Decimal('0')
    for a in agents:
        d = data.get(a.pk, {})
        wi = inputs_map.get(a.pk)
        base = d.get('base_pay_mxn', Decimal('0'))
        bonus = d.get('bonus_mxn', Decimal('0'))
        broke = a.pk in ba_agents
        if broke:
            bonus = Decimal('0')  # break abuse zeroes the adherence bonus
        comm_pct = d.get('commission_pct', Decimal('0'))
        rate = d.get('hourly_mxn', Decimal('0'))

        lpo = wi.lpo if wi else Decimal('0')
        net_lpo = (lpo * (Decimal('1') - comm_pct / Decimal('100'))).quantize(Decimal('0.01'))
        spiff_mxn = ((wi.spiff_usd if wi else Decimal('0')) * fx).quantize(Decimal('0.01'))
        welcome = wi.welcome if wi else Decimal('0')
        referral = wi.referral if wi else Decimal('0')
        kill_qa = wi.kill_team_qa if wi else Decimal('0')
        comedor = wi.comedor if wi else Decimal('0')
        transport = wi.transportation if wi else Decimal('0')
        holiday_pay = (hol_hours.get(a.pk, Decimal('0')) * rate * 2).quantize(Decimal('0.01'))

        subtotal = base + bonus + net_lpo + spiff_mxn + welcome + referral + kill_qa + holiday_pay
        total = subtotal - comedor - transport  # loans added later; may go negative (G6)

        rows.append({
            'agent': a, 'emp': a.employee_id or '',
            'legal_name': a.user.get_full_name() or a.user.username,
            'username': a.user.username, 'break_abuse': broke,
            'hours': d.get('final_hrs', Decimal('0')), 'rate': rate,
            'base_pay': base, 'adherence_bonus': bonus,
            'lpo': lpo, 'net_lpo': net_lpo, 'comm_pct': comm_pct, 'spiff_mxn': spiff_mxn,
            'welcome': welcome, 'referral': referral, 'kill_qa': kill_qa, 'holiday_pay': holiday_pay,
            'subtotal': subtotal, 'comedor': comedor, 'transport': transport, 'total': total,
        })
        tot_base += base; tot_bonus += bonus; tot_lpo += net_lpo; tot_spiff += spiff_mxn
        tot_hol += holiday_pay; tot_sub += subtotal; tot_total += total

    ctx = _nav(week_start, week_dates)
    ctx.update({
        'rows': rows,
        'totals': {'base': tot_base, 'bonus': tot_bonus, 'net_lpo': tot_lpo, 'spiff': tot_spiff,
                   'holiday': tot_hol, 'subtotal': tot_sub, 'total': tot_total},
    })
    return render(request, 'nomina/agent_nomina.html', ctx)


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
def admin_nomina(request):
    """Admin Nómina — Official Admins (Infinity). Base = hours × admin wage,
    plus the fixed admin bonus, plus admin spiffs/commissions/referral, minus
    comedor/transport. Reuses the same paste inputs + the existing engine."""
    from finance.views import _get_billable_weekly_data
    from finance.models import BillingSettings

    week_start, week_dates = _week(request)
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

    ctx = _nav(week_start, week_dates)
    ctx.update({
        'rows': rows,
        'totals': {'base': tot_base, 'bonus': tot_bonus, 'subtotal': tot_sub, 'total': tot_total},
    })
    return render(request, 'nomina/admin_nomina.html', ctx)
