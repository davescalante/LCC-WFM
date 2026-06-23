from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.db.models import Q, Sum

from scheduling.models import Agent, Five9Profile, OvertimeShift
from adherence.models import AdherenceRecord, DailyAgentHours, DailyUpload, PayrollAdjustment, Coding
from .models import BillingSettings

# ─── Access control ───────────────────────────────────────────────────────────
# Finance is visible to role='admin' users who are NOT supervisors or coordinators,
# plus Django superusers.
_FINANCE_BLOCKED_TYPES = {'supervisor', 'coordinator'}


def _has_finance_access(user):
    if user.is_superuser:
        return True
    try:
        p = user.agent
        return p.role == 'admin' and p.role_type not in _FINANCE_BLOCKED_TYPES
    except Exception:
        return False


def finance_access_required(view_func):
    from functools import wraps
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        if not _has_finance_access(request.user):
            messages.error(request, "Access denied.")
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return _wrapped


# ─── Week helpers ─────────────────────────────────────────────────────────────

def _get_week_start(request):
    """Parse ?week= param (ISO Monday) or default to current Monday."""
    week_str = request.GET.get('week', '')
    try:
        d = date.fromisoformat(week_str)
        return d - timedelta(days=d.weekday())
    except (ValueError, TypeError):
        today = date.today()
        return today - timedelta(days=today.weekday())


def _week_dates(week_start):
    return [week_start + timedelta(days=i) for i in range(7)]


def _fmt_hrs(h):
    """Format Decimal hours as '12.75' (2 decimal places)."""
    if h is None:
        return '—'
    return f"{h:.2f}"


def _fmt_mxn(v):
    if v is None:
        return '—'
    return f"${v:,.2f}"


def _fmt_usd(v):
    if v is None:
        return '—'
    return f"${v:,.2f}"


# ─── Core weekly calculation ───────────────────────────────────────────────────

def _get_billable_weekly_data(agents, week_dates, settings):
    """
    Returns a dict keyed by agent.pk with:
      total_login_hrs, total_coded_hrs, total_nr_hrs,
      nr_cap_hrs, nr_cap_adj_hrs, final_hrs,
      bonus (bool), ot_regular_hrs, ot_1_5_hrs, ot_power_hrs,
      commission_pct, base_pay_mxn, ot_regular_mxn, ot_1_5_mxn,
      power_hour_usd, bonus_mxn,
      total_pay_mxn, total_pay_usd, billing_usd
    """
    agent_ids = [a.pk for a in agents]
    week_start = week_dates[0]

    # ── Billable username lookup ───────────────────────────────────────────
    billable_map = {}   # agent_id -> set of usernames (lowercase)
    for p in Five9Profile.objects.filter(agent__in=agent_ids, billable=True).values('agent_id', 'five9_username'):
        billable_map.setdefault(p['agent_id'], set()).add(p['five9_username'].strip().lower())

    # ── Sum login + NR seconds from billable DailyAgentHours ──────────────
    nr_secs_map = {}    # agent_id -> total NR seconds
    login_secs_map = {} # agent_id -> total login seconds
    for row in DailyAgentHours.objects.filter(
        upload__date__in=week_dates, agent__in=agent_ids
    ).values('agent_id', 'five9_username', 'login_seconds', 'not_ready_seconds'):
        aid = row['agent_id']
        if aid is None:
            continue
        uname = row['five9_username'].strip().lower()
        bnames = billable_map.get(aid)
        if bnames is None or uname in bnames:
            nr_secs_map[aid] = nr_secs_map.get(aid, 0) + row['not_ready_seconds']
            login_secs_map[aid] = login_secs_map.get(aid, 0) + row['login_seconds']

    # ── AdherenceRecord actual_hours (already has daily NR deduction) ──────
    actual_hrs_map = {}
    for rec in AdherenceRecord.objects.filter(agent__in=agent_ids, date__in=week_dates).values('agent_id', 'actual_hours', 'status'):
        aid = rec['agent_id']
        if rec['actual_hours']:
            actual_hrs_map[aid] = actual_hrs_map.get(aid, Decimal('0')) + rec['actual_hours']

    # ── Coding hours ──────────────────────────────────────────────────────
    coded_hrs_map = {}
    for coding in Coding.objects.filter(agent__in=agent_ids, date__in=week_dates):
        coded_hrs_map[coding.agent_id] = coded_hrs_map.get(coding.agent_id, Decimal('0')) + Decimal(str(coding.total_hours()))

    # ── Adherence bonus (already tracked in adherence tab) ────────────────
    BONUS_QUALIFYING = {'P', 'OT', 'MUT', 'VTO', 'P+VTO', 'V'}
    BONUS_DISQUALIFYING = {'Absent', 'NCNS', 'T', 'T+VTO', 'T+I', 'I', 'LOA', 'S'}
    bonus_map = {}    # agent_id -> True/False/None
    has_status = set()
    records_by_agent = {}
    for rec in AdherenceRecord.objects.filter(agent__in=agent_ids, date__in=week_dates).values('agent_id', 'status'):
        aid = rec['agent_id']
        if rec['status']:
            has_status.add(aid)
            if rec['status'] in BONUS_DISQUALIFYING:
                bonus_map[aid] = False
            elif aid not in bonus_map and rec['status'] in BONUS_QUALIFYING:
                bonus_map[aid] = True
            elif aid not in bonus_map:
                bonus_map[aid] = False
    # OT no-show disqualifies
    for ot in OvertimeShift.objects.filter(agent__in=agent_ids, date__in=week_dates, status='no_show'):
        bonus_map[ot.agent_id] = False

    # ── OT hours by type ──────────────────────────────────────────────────
    ot_regular_map = {}
    ot_1_5_map = {}
    ot_power_map = {}
    for ot in OvertimeShift.objects.filter(agent__in=agent_ids, date__in=week_dates, status='completed'):
        hrs = ot.total_shift_hours()
        if ot.incentive_type == 'none':
            ot_regular_map[ot.agent_id] = ot_regular_map.get(ot.agent_id, Decimal('0')) + hrs
        elif ot.incentive_type == 'time_and_a_half':
            ot_1_5_map[ot.agent_id] = ot_1_5_map.get(ot.agent_id, Decimal('0')) + hrs
        elif ot.incentive_type == 'power_hour':
            ot_power_map[ot.agent_id] = ot_power_map.get(ot.agent_id, Decimal('0')) + hrs

    # ── Commission deductions ─────────────────────────────────────────────
    commission_map = {}
    for pa in PayrollAdjustment.objects.filter(agent__in=agent_ids, week_start=week_start):
        commission_map[pa.agent_id] = pa.commission_deduction

    results = {}
    for agent in agents:
        aid = agent.pk
        hourly_mxn = agent.hourly_rate or Decimal('0')
        billing_rate = agent.billing_rate_usd or settings.billing_rate_usd
        usd_to_mxn = settings.usd_to_mxn

        # NR cap
        total_nr_hrs = Decimal(str(nr_secs_map.get(aid, 0))) / Decimal('3600')
        nr_cap = settings.nr_cap_kill_team_hours if agent.role_type == 'kill_team' else settings.nr_cap_regular_hours
        nr_cap_adj = max(Decimal('0'), total_nr_hrs - nr_cap)

        # Final worked hours
        actual_hrs = actual_hrs_map.get(aid, Decimal('0'))
        coded_hrs = coded_hrs_map.get(aid, Decimal('0'))
        pre_cap_total = actual_hrs + coded_hrs
        final_hrs = max(Decimal('0'), pre_cap_total - nr_cap_adj)

        # OT
        ot_reg = ot_regular_map.get(aid, Decimal('0'))
        ot_1_5 = ot_1_5_map.get(aid, Decimal('0'))
        ot_pow = ot_power_map.get(aid, Decimal('0'))

        # Pay calculations
        base_pay = (final_hrs * hourly_mxn).quantize(Decimal('0.01'), ROUND_HALF_UP)
        ot_reg_pay = (ot_reg * hourly_mxn).quantize(Decimal('0.01'), ROUND_HALF_UP)
        ot_1_5_pay = (ot_1_5 * hourly_mxn * Decimal('1.5')).quantize(Decimal('0.01'), ROUND_HALF_UP)
        power_usd = (ot_pow * billing_rate * Decimal('2')).quantize(Decimal('0.01'), ROUND_HALF_UP)

        is_official_admin = getattr(agent, 'is_official_admin', False)
        bonus_qualifies = (not is_official_admin) and bonus_map.get(aid) is True and aid in has_status
        if bonus_qualifies and settings.adherence_bonus_full_hours > 0:
            bonus_mxn = min(
                settings.adherence_bonus_max_mxn,
                (final_hrs / settings.adherence_bonus_full_hours * settings.adherence_bonus_max_mxn)
            ).quantize(Decimal('0.01'), ROUND_HALF_UP)
        else:
            bonus_mxn = Decimal('0')

        if is_official_admin:
            admin_bonus = (agent.admin_bonus_mxn if agent.admin_bonus_mxn is not None
                           else settings.default_admin_bonus_mxn)
        else:
            admin_bonus = Decimal('0')

        comm_pct = commission_map.get(aid, Decimal('0'))

        total_pay_mxn = base_pay + ot_reg_pay + ot_1_5_pay + bonus_mxn + admin_bonus
        total_pay_usd = (total_pay_mxn / usd_to_mxn).quantize(Decimal('0.01'), ROUND_HALF_UP) if usd_to_mxn else Decimal('0')

        # Billing (what Infinity charges LCC)
        billing_usd = (final_hrs * billing_rate).quantize(Decimal('0.01'), ROUND_HALF_UP)

        results[aid] = {
            'agent': agent,
            'total_nr_hrs': total_nr_hrs,
            'nr_cap_hrs': nr_cap,
            'nr_cap_adj_hrs': nr_cap_adj,
            'actual_hrs': actual_hrs,
            'coded_hrs': coded_hrs,
            'pre_cap_total': pre_cap_total,
            'final_hrs': final_hrs,
            'ot_regular_hrs': ot_reg,
            'ot_1_5_hrs': ot_1_5,
            'ot_power_hrs': ot_pow,
            'hourly_mxn': hourly_mxn,
            'billing_rate_usd': billing_rate,
            'base_pay_mxn': base_pay,
            'ot_regular_mxn': ot_reg_pay,
            'ot_1_5_mxn': ot_1_5_pay,
            'power_hour_usd': power_usd,
            'bonus_qualifies': bonus_qualifies,
            'is_official_admin': is_official_admin,
            'bonus_mxn': bonus_mxn,
            'admin_bonus_mxn': admin_bonus,
            'commission_pct': comm_pct,
            'total_pay_mxn': total_pay_mxn,
            'total_pay_usd': total_pay_usd,
            'billing_usd': billing_usd,
        }
    return results


# ─── Views ────────────────────────────────────────────────────────────────────

@login_required
@finance_access_required
def finance_dashboard(request):
    settings = BillingSettings.get()
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_dates = _week_dates(week_start)
    week_end = week_dates[-1]

    agents = Agent.objects.filter(
        status='active', track_attendance=True,
        billing_status='Billed',
    ).select_related('user', 'supervisor__user').prefetch_related('five9_profiles', 'separations')

    data = _get_billable_weekly_data(list(agents), week_dates, settings)

    total_hrs = sum(d['final_hrs'] for d in data.values())
    total_billing_usd = sum(d['billing_usd'] for d in data.values())
    total_payroll_mxn = sum(d['total_pay_mxn'] for d in data.values())
    total_payroll_usd = sum(d['total_pay_usd'] for d in data.values())
    bonus_count = sum(1 for d in data.values() if d['bonus_qualifies'])
    bonus_total_mxn = sum(d['bonus_mxn'] for d in data.values())
    admin_bonus_total_mxn = sum(d['admin_bonus_mxn'] for d in data.values())
    power_usd = sum(d['power_hour_usd'] for d in data.values())

    return render(request, 'finance/dashboard.html', {
        'settings': settings,
        'week_start': week_start,
        'week_end': week_end,
        'today': today,
        'total_hrs': total_hrs,
        'total_billing_usd': total_billing_usd,
        'total_payroll_mxn': total_payroll_mxn,
        'total_payroll_usd': total_payroll_usd,
        'bonus_count': bonus_count,
        'bonus_total_mxn': bonus_total_mxn,
        'admin_bonus_total_mxn': admin_bonus_total_mxn,
        'power_usd': power_usd,
        'agent_count': len(data),
    })


@login_required
@finance_access_required
def billing_report(request):
    settings = BillingSettings.get()
    week_start = _get_week_start(request)
    week_dates = _week_dates(week_start)
    week_end = week_dates[-1]

    # All billed agents (Infinity employer or LCC) with separation filter
    agents = Agent.objects.filter(
        billing_status='Billed',
    ).exclude(
        Q(status='inactive') &
        Q(separations__status='finalized') &
        Q(separations__remove_from_adherence_date__lte=week_start)
    ).select_related('user', 'supervisor__user').prefetch_related('five9_profiles', 'separations').distinct()

    data = _get_billable_weekly_data(list(agents), week_dates, settings)

    # Group by employer, then by role_type
    _ROLE_GROUPS = [
        ('regular_agent', 'Regular Agents'),
        ('kill_team', 'Kill Team'),
        ('incubation', 'Incubation'),
        ('night_shift', 'Night Shift'),
        ('training', 'Training'),
        ('qa', 'QA'),
        ('cs', 'CS'),
        ('testing', 'Testing'),
        ('sms_email', 'SMS / Email'),
        ('supervisor', 'Supervisors'),
        ('coordinator', 'Coordinators'),
    ]

    infinity_rows = []
    lcc_rows = []
    for agent in agents:
        d = data.get(agent.pk, {})
        row = {**d, 'agent': agent}
        if agent.employer == 'LCC':
            lcc_rows.append(row)
        else:
            infinity_rows.append(row)

    def _group_rows(rows):
        groups = []
        for role_key, role_label in _ROLE_GROUPS:
            group_rows = [r for r in rows if r['agent'].role_type == role_key]
            if group_rows:
                subtotal_hrs = sum(r.get('final_hrs', Decimal('0')) for r in group_rows)
                subtotal_usd = sum(r.get('billing_usd', Decimal('0')) for r in group_rows)
                groups.append({
                    'label': role_label,
                    'rows': group_rows,
                    'subtotal_hrs': subtotal_hrs,
                    'subtotal_usd': subtotal_usd,
                })
        # Catch-all for any role_types not in the list
        listed_keys = {k for k, _ in _ROLE_GROUPS}
        other_rows = [r for r in rows if r['agent'].role_type not in listed_keys]
        if other_rows:
            groups.append({
                'label': 'Other',
                'rows': other_rows,
                'subtotal_hrs': sum(r.get('final_hrs', Decimal('0')) for r in other_rows),
                'subtotal_usd': sum(r.get('billing_usd', Decimal('0')) for r in other_rows),
            })
        return groups

    infinity_groups = _group_rows(infinity_rows)
    lcc_groups = _group_rows(lcc_rows)

    infinity_total_hrs = sum(r.get('final_hrs', Decimal('0')) for r in infinity_rows)
    infinity_total_usd = sum(r.get('billing_usd', Decimal('0')) for r in infinity_rows)
    power_total_usd = sum(d.get('power_hour_usd', Decimal('0')) for d in data.values())
    bonus_total_mxn = sum(d.get('bonus_mxn', Decimal('0')) for d in data.values())
    bonus_total_usd = (bonus_total_mxn / settings.usd_to_mxn).quantize(Decimal('0.01'), ROUND_HALF_UP) if settings.usd_to_mxn else Decimal('0')

    # Week navigation
    prev_week = (week_start - timedelta(days=7)).isoformat()
    next_week = (week_start + timedelta(days=7)).isoformat()

    return render(request, 'finance/billing.html', {
        'settings': settings,
        'week_start': week_start,
        'week_end': week_end,
        'prev_week': prev_week,
        'next_week': next_week,
        'infinity_groups': infinity_groups,
        'lcc_groups': lcc_groups,
        'infinity_total_hrs': infinity_total_hrs,
        'infinity_total_usd': infinity_total_usd,
        'power_total_usd': power_total_usd,
        'bonus_total_usd': bonus_total_usd,
    })


@login_required
@finance_access_required
def billing_export(request):
    """Export billing report as Excel."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    settings = BillingSettings.get()
    week_start = _get_week_start(request)
    week_dates = _week_dates(week_start)
    week_end = week_dates[-1]

    agents = Agent.objects.filter(
        billing_status='Billed',
    ).exclude(
        Q(status='inactive') &
        Q(separations__status='finalized') &
        Q(separations__remove_from_adherence_date__lte=week_start)
    ).select_related('user', 'supervisor__user').prefetch_related('five9_profiles', 'separations').distinct()

    data = _get_billable_weekly_data(list(agents), week_dates, settings)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Billing"

    # Styles
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1A3A5C")
    subheader_fill = PatternFill("solid", fgColor="E5E7EB")
    subheader_font = Font(bold=True, color="374151")
    total_fill = PatternFill("solid", fgColor="DBEAFE")
    center = Alignment(horizontal='center')
    right = Alignment(horizontal='right')

    headers = [
        'Agent Name', 'Legal Name', 'Employee ID', 'Five9 Username (Billable)',
        'Supervisor', 'Agent Type', 'Employer',
        'Worked Hrs (Final)', 'Billing Rate (USD)', 'Total Billing (USD)',
    ]

    # Title
    ws.append([f"Billing Report — Week of {week_start.strftime('%B %d, %Y')} to {week_end.strftime('%B %d, %Y')}"])
    ws.merge_cells(f'A1:{get_column_letter(len(headers))}1')
    ws['A1'].font = Font(bold=True, size=13)
    ws.append([])

    # Headers
    ws.append(headers)
    for col, _ in enumerate(headers, 1):
        cell = ws.cell(row=3, column=col)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center

    def _primary_billable_username(agent):
        # Primary first, then any billable
        profiles = list(agent.five9_profiles.all())
        primary = next((p for p in profiles if p.is_primary and p.billable), None)
        if primary:
            return primary.five9_username
        billable = next((p for p in profiles if p.billable), None)
        return billable.five9_username if billable else ''

    row_num = 4
    for employer_label, employer_agents in [('Infinity', [a for a in agents if a.employer == 'Infinity']),
                                             ('LCC Direct', [a for a in agents if a.employer == 'LCC'])]:
        if not employer_agents:
            continue
        ws.append([employer_label])
        ws.cell(row=row_num, column=1).font = subheader_font
        ws.cell(row=row_num, column=1).fill = subheader_fill
        ws.merge_cells(f'A{row_num}:{get_column_letter(len(headers))}{row_num}')
        row_num += 1

        for agent in sorted(employer_agents, key=lambda a: (a.role_type or '', str(a))):
            d = data.get(agent.pk, {})
            ws.append([
                str(agent),
                agent.user.get_full_name(),
                agent.employee_id or '',
                _primary_billable_username(agent),
                str(agent.supervisor) if agent.supervisor else '',
                agent.get_role_type_display() or '',
                agent.employer,
                float(d.get('final_hrs', 0)),
                float(d.get('billing_rate_usd', settings.billing_rate_usd)),
                float(d.get('billing_usd', 0)),
            ])
            row_num += 1

    # Totals
    ws.append([])
    row_num += 1
    total_hrs = sum(d.get('final_hrs', Decimal('0')) for d in data.values())
    total_usd = sum(d.get('billing_usd', Decimal('0')) for d in data.values())
    power_usd = sum(d.get('power_hour_usd', Decimal('0')) for d in data.values())
    bonus_mxn = sum(d.get('bonus_mxn', Decimal('0')) for d in data.values())
    bonus_usd = (bonus_mxn / settings.usd_to_mxn).quantize(Decimal('0.01')) if settings.usd_to_mxn else Decimal('0')

    ws.append(['TOTAL', '', '', '', '', '', '', float(total_hrs), '', float(total_usd)])
    ws.append(['Power Hour Spiff (LCC pays direct)', '', '', '', '', '', '', '', '', float(power_usd)])
    ws.append([f'Adherence Bonus Total (USD equiv.)', '', '', '', '', '', '', '', '', float(bonus_usd)])

    # Column widths
    col_widths = [22, 22, 14, 24, 20, 18, 12, 16, 16, 18]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = (
        f'attachment; filename="billing_{week_start.isoformat()}.xlsx"'
    )
    wb.save(response)
    return response


@login_required
@finance_access_required
def payroll_report(request):
    settings = BillingSettings.get()
    week_start = _get_week_start(request)
    week_dates = _week_dates(week_start)
    week_end = week_dates[-1]

    agents = Agent.objects.filter(
        track_attendance=True,
    ).exclude(
        Q(status='inactive') &
        Q(separations__status='finalized') &
        Q(separations__remove_from_adherence_date__lte=week_start)
    ).select_related('user', 'supervisor__user').prefetch_related('five9_profiles', 'separations').distinct()

    data = _get_billable_weekly_data(list(agents), week_dates, settings)

    infinity_rows = []
    lcc_rows = []
    for agent in agents:
        d = data.get(agent.pk, {})
        row = {**d, 'agent': agent}
        if agent.employer == 'LCC':
            lcc_rows.append(row)
        else:
            infinity_rows.append(row)

    infinity_rows.sort(key=lambda r: str(r['agent']))
    lcc_rows.sort(key=lambda r: str(r['agent']))

    infinity_totals = {
        'base_pay_mxn': sum(r.get('base_pay_mxn', Decimal('0')) for r in infinity_rows),
        'ot_regular_mxn': sum(r.get('ot_regular_mxn', Decimal('0')) for r in infinity_rows),
        'ot_1_5_mxn': sum(r.get('ot_1_5_mxn', Decimal('0')) for r in infinity_rows),
        'power_hour_usd': sum(r.get('power_hour_usd', Decimal('0')) for r in infinity_rows),
        'bonus_mxn': sum(r.get('bonus_mxn', Decimal('0')) for r in infinity_rows),
        'admin_bonus_mxn': sum(r.get('admin_bonus_mxn', Decimal('0')) for r in infinity_rows),
        'total_pay_mxn': sum(r.get('total_pay_mxn', Decimal('0')) for r in infinity_rows),
        'total_pay_usd': sum(r.get('total_pay_usd', Decimal('0')) for r in infinity_rows),
    }

    prev_week = (week_start - timedelta(days=7)).isoformat()
    next_week = (week_start + timedelta(days=7)).isoformat()

    return render(request, 'finance/payroll.html', {
        'settings': settings,
        'week_start': week_start,
        'week_end': week_end,
        'prev_week': prev_week,
        'next_week': next_week,
        'infinity_rows': infinity_rows,
        'lcc_rows': lcc_rows,
        'infinity_totals': infinity_totals,
    })


@login_required
@finance_access_required
def payroll_export(request):
    """Export payroll report as Excel."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    settings = BillingSettings.get()
    week_start = _get_week_start(request)
    week_dates = _week_dates(week_start)
    week_end = week_dates[-1]

    agents = Agent.objects.filter(
        track_attendance=True,
    ).exclude(
        Q(status='inactive') &
        Q(separations__status='finalized') &
        Q(separations__remove_from_adherence_date__lte=week_start)
    ).select_related('user', 'supervisor__user').prefetch_related('five9_profiles', 'separations').distinct()

    data = _get_billable_weekly_data(list(agents), week_dates, settings)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Payroll"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1A3A5C")
    section_fill = PatternFill("solid", fgColor="E5E7EB")
    section_font = Font(bold=True, color="374151")
    center = Alignment(horizontal='center')

    headers = [
        'Agent Name', 'Legal Name', 'Employee ID', 'Supervisor', 'Agent Type',
        'Worked Hrs', 'Hourly Rate (MXN)',
        'Base Pay (MXN)', 'Adh. Bonus (MXN)', 'Admin Bonus (MXN)',
        'OT Regular (MXN)', 'OT 1.5x (MXN)', 'Power Hour (USD)',
        'Comm. Ded. %', 'Comm. Earned (MXN)',
        'Total Pay (MXN)', 'Total Pay (USD equiv.)',
    ]

    ws.append([f"Payroll Report — Week of {week_start.strftime('%B %d, %Y')} to {week_end.strftime('%B %d, %Y')}"])
    ws.merge_cells(f'A1:{get_column_letter(len(headers))}1')
    ws['A1'].font = Font(bold=True, size=13)
    ws.append([])
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        c = ws.cell(row=3, column=col)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center

    row_num = 4
    for section_label, section_agents in [
        ('Infinity Employees', [a for a in agents if a.employer == 'Infinity']),
        ('LCC Direct Employees', [a for a in agents if a.employer == 'LCC']),
    ]:
        if not section_agents:
            continue
        ws.append([section_label])
        ws.cell(row=row_num, column=1).font = section_font
        ws.cell(row=row_num, column=1).fill = section_fill
        ws.merge_cells(f'A{row_num}:{get_column_letter(len(headers))}{row_num}')
        row_num += 1

        for agent in sorted(section_agents, key=lambda a: str(a)):
            d = data.get(agent.pk, {})
            ws.append([
                str(agent),
                agent.user.get_full_name(),
                agent.employee_id or '',
                str(agent.supervisor) if agent.supervisor else '',
                agent.get_role_type_display() or '',
                float(d.get('final_hrs', 0)),
                float(agent.hourly_rate or 0),
                float(d.get('base_pay_mxn', 0)),
                float(d.get('bonus_mxn', 0)),
                float(d.get('admin_bonus_mxn', 0)),
                float(d.get('ot_regular_mxn', 0)),
                float(d.get('ot_1_5_mxn', 0)),
                float(d.get('power_hour_usd', 0)),
                float(d.get('commission_pct', 0)),
                '—',
                float(d.get('total_pay_mxn', 0)),
                float(d.get('total_pay_usd', 0)),
            ])
            row_num += 1

    col_widths = [22, 22, 14, 20, 18, 12, 18, 16, 16, 16, 16, 14, 16, 14, 18, 16, 18]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = (
        f'attachment; filename="payroll_{week_start.isoformat()}.xlsx"'
    )
    wb.save(response)
    return response


@login_required
@finance_access_required
def finance_settings(request):
    settings = BillingSettings.get()
    if request.method == 'POST':
        try:
            settings.billing_rate_usd = Decimal(request.POST.get('billing_rate_usd', str(settings.billing_rate_usd)))
            settings.usd_to_mxn = Decimal(request.POST.get('usd_to_mxn', str(settings.usd_to_mxn)))
            usd_updated_str = request.POST.get('usd_to_mxn_updated', '').strip()
            if usd_updated_str:
                settings.usd_to_mxn_updated = date.fromisoformat(usd_updated_str)
            settings.nr_cap_regular_hours = Decimal(request.POST.get('nr_cap_regular_hours', str(settings.nr_cap_regular_hours)))
            settings.nr_cap_kill_team_hours = Decimal(request.POST.get('nr_cap_kill_team_hours', str(settings.nr_cap_kill_team_hours)))
            settings.default_admin_bonus_mxn = Decimal(request.POST.get('default_admin_bonus_mxn', str(settings.default_admin_bonus_mxn)))
            settings.adherence_bonus_max_mxn = Decimal(request.POST.get('adherence_bonus_max_mxn', str(settings.adherence_bonus_max_mxn)))
            settings.adherence_bonus_full_hours = Decimal(request.POST.get('adherence_bonus_full_hours', str(settings.adherence_bonus_full_hours)))
            settings.save()
            messages.success(request, "Settings saved.")
        except Exception as e:
            messages.error(request, f"Error saving settings: {e}")
        return redirect('finance_settings')

    return render(request, 'finance/settings.html', {'settings': settings})
