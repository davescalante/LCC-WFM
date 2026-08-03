from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone

from wfm.utils import get_week_start, parse_week_param
from .access import nomina_access_required


def _week(request):
    raw = request.GET.get('week_start') or request.GET.get('week')
    week_start = parse_week_param(raw) or get_week_start()
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    return week_start, week_dates


@login_required
@nomina_access_required
def dashboard(request):
    """Nómina landing page — super admin only."""
    week_start, week_dates = _week(request)
    return render(request, 'nomina/dashboard.html', {
        'week_start': week_start,
        'week_end': week_dates[-1],
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
        'today': timezone.localdate(),
    })


@login_required
@nomina_access_required
def agent_nomina(request):
    """Agent Nómina — the auto-computed columns, live from the existing engine.

    Reuses finance's `_get_billable_weekly_data` (the same source as Billing v2)
    for hours, base pay, adherence bonus and commission. Infinity employees only
    (LCC excluded); Official Admins are handled on the Admin Nómina. The manual
    paste columns (LPO, spiffs, comedor, …) and overrides are added in later
    phases — shown here as placeholders so the full shape is visible.
    """
    from finance.views import _get_billable_weekly_data
    from finance.models import BillingSettings
    from scheduling.models import Agent

    week_start, week_dates = _week(request)
    settings = BillingSettings.get_for_week(week_start)

    pay_window = Q(status='active') | Q(
        status='inactive', separations__status='finalized',
        separations__remove_from_adherence_date__gt=week_start,
    )
    agents = list(
        Agent.objects.filter(pay_window)
        .filter(Q(track_attendance=True) | Q(five9_profiles__billable=True))
        .filter(employer='Infinity')       # INFINITY only — LCC excluded
        .exclude(is_official_admin=True)   # admins go on the Admin Nómina
        .distinct()
        .select_related('user', 'supervisor__user')
        .prefetch_related('five9_profiles')
        .order_by('user__last_name', 'user__first_name')
    )

    data = _get_billable_weekly_data(agents, week_dates, settings)

    rows = []
    for a in agents:
        d = data.get(a.pk, {})
        rows.append({
            'agent': a,
            'emp': a.employee_id or '',
            'legal_name': a.user.get_full_name() or a.user.username,
            'username': a.user.username,
            'hours': d.get('final_hrs', Decimal('0')),
            'rate': d.get('hourly_mxn', Decimal('0')),
            'base_pay': d.get('base_pay_mxn', Decimal('0')),
            'adherence_bonus': d.get('bonus_mxn', Decimal('0')),
            'commission_pct': d.get('commission_pct', Decimal('0')),
            'auto_total': d.get('total_pay_mxn', Decimal('0')),
        })

    totals = {
        'hours': sum((r['hours'] for r in rows), Decimal('0')),
        'base_pay': sum((r['base_pay'] for r in rows), Decimal('0')),
        'adherence_bonus': sum((r['adherence_bonus'] for r in rows), Decimal('0')),
        'auto_total': sum((r['auto_total'] for r in rows), Decimal('0')),
    }

    return render(request, 'nomina/agent_nomina.html', {
        'rows': rows,
        'totals': totals,
        'week_start': week_start,
        'week_end': week_dates[-1],
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
    })
