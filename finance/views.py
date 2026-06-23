from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.db.models import Q, Sum

from scheduling.models import Agent, Five9Profile, OvertimeShift
from adherence.models import AdherenceRecord, DailyAgentHours, DailyUpload, PayrollAdjustment, Coding
from .models import BillingSettings, BillingSettingsHistory

# ─── Access control ───────────────────────────────────────────────────────────
# Finance is visible only to users with is_super_admin=True, plus Django superusers.

def _has_finance_access(user):
    if user.is_superuser:
        return True
    try:
        return user.agent.is_super_admin
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
    billable_map = {}         # agent_id -> set of usernames (lowercase) for hour filtering
    primary_billable_map = {} # agent_id -> display username (primary billable, or first billable)
    for p in Five9Profile.objects.filter(
        agent__in=agent_ids, billable=True
    ).values('agent_id', 'five9_username', 'is_primary').order_by('agent_id', '-is_primary', 'id'):
        aid = p['agent_id']
        billable_map.setdefault(aid, set()).add(p['five9_username'].strip().lower())
        if aid not in primary_billable_map:
            primary_billable_map[aid] = p['five9_username']

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

        # Raw login hours — no daily NR deductions; weekly checks only
        raw_login_hrs = Decimal(str(login_secs_map.get(aid, 0))) / Decimal('3600')
        coded_hrs = coded_hrs_map.get(aid, Decimal('0'))
        pre_total = raw_login_hrs + coded_hrs

        # Weekly NR deduction — apply the larger of two checks, never both
        total_nr_hrs = Decimal(str(nr_secs_map.get(aid, 0))) / Decimal('3600')
        nr_cap = settings.nr_cap_kill_team_hours if agent.role_type == 'kill_team' else settings.nr_cap_regular_hours
        check1_ded = max(Decimal('0'), total_nr_hrs - nr_cap)
        if pre_total <= Decimal('48'):
            check2_ded = max(Decimal('0'), total_nr_hrs - raw_login_hrs * Decimal('0.125'))
        else:
            check2_ded = Decimal('0')
        nr_deduction = max(check1_ded, check2_ded)
        final_hrs = max(Decimal('0'), pre_total - nr_deduction)

        # OT
        ot_reg = ot_regular_map.get(aid, Decimal('0'))
        ot_1_5 = ot_1_5_map.get(aid, Decimal('0'))
        ot_pow = ot_power_map.get(aid, Decimal('0'))

        # Pay calculations
        # OT base pay is already captured in final_hrs (agents log into Five9 during OT)
        # These columns are top-up only: the LCC incentive premium above the regular rate
        base_pay = (final_hrs * hourly_mxn).quantize(Decimal('0.01'), ROUND_HALF_UP)
        ph_topup_mxn = (ot_pow * hourly_mxn).quantize(Decimal('0.01'), ROUND_HALF_UP)
        ot_1_5_topup_mxn = (ot_1_5 * hourly_mxn * Decimal('0.5')).quantize(Decimal('0.01'), ROUND_HALF_UP)

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

        total_pay_mxn = base_pay + ph_topup_mxn + ot_1_5_topup_mxn + bonus_mxn + admin_bonus
        total_pay_usd = (total_pay_mxn / usd_to_mxn).quantize(Decimal('0.01'), ROUND_HALF_UP) if usd_to_mxn else Decimal('0')

        # Billing (what Infinity charges LCC)
        billing_usd = (final_hrs * billing_rate).quantize(Decimal('0.01'), ROUND_HALF_UP)

        results[aid] = {
            'agent': agent,
            'five9_username': primary_billable_map.get(aid, ''),
            'total_nr_hrs': total_nr_hrs,
            'nr_cap_hrs': nr_cap,
            'nr_deduction': nr_deduction,
            'actual_hrs': raw_login_hrs,
            'coded_hrs': coded_hrs,
            'pre_cap_total': pre_total,
            'final_hrs': final_hrs,
            'ot_regular_hrs': ot_reg,
            'ot_1_5_hrs': ot_1_5,
            'ot_power_hrs': ot_pow,
            'hourly_mxn': hourly_mxn,
            'billing_rate_usd': billing_rate,
            'base_pay_mxn': base_pay,
            'ph_topup_mxn': ph_topup_mxn,
            'ot_1_5_topup_mxn': ot_1_5_topup_mxn,
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
    today = date.today()
    week_start = _get_week_start(request)
    week_dates = _week_dates(week_start)
    week_end = week_dates[-1]
    settings = BillingSettings.get_for_week(week_start)

    agents = Agent.objects.filter(
        status='active',
        five9_profiles__billable=True,
    ).select_related('user', 'supervisor__user').prefetch_related('five9_profiles', 'separations').distinct()

    data = _get_billable_weekly_data(list(agents), week_dates, settings)

    total_hrs = sum(d['final_hrs'] for d in data.values())
    total_billing_usd = sum(d['billing_usd'] for d in data.values())
    total_payroll_mxn = sum(d['total_pay_mxn'] for d in data.values())
    total_payroll_usd = sum(d['total_pay_usd'] for d in data.values())
    bonus_count = sum(1 for d in data.values() if d['bonus_qualifies'])
    bonus_total_mxn = sum(d['bonus_mxn'] for d in data.values())
    admin_bonus_total_mxn = sum(d['admin_bonus_mxn'] for d in data.values())
    ph_topup_total_mxn = sum(d['ph_topup_mxn'] for d in data.values())
    ot_1_5_topup_total_mxn = sum(d['ot_1_5_topup_mxn'] for d in data.values())

    prev_week = (week_start - timedelta(days=7)).isoformat()
    next_week = (week_start + timedelta(days=7)).isoformat()
    current_week = (today - timedelta(days=today.weekday())).isoformat()

    return render(request, 'finance/dashboard.html', {
        'settings': settings,
        'week_start': week_start,
        'week_end': week_end,
        'today': today,
        'prev_week': prev_week,
        'next_week': next_week,
        'current_week': current_week,
        'total_hrs': total_hrs,
        'total_billing_usd': total_billing_usd,
        'total_payroll_mxn': total_payroll_mxn,
        'total_payroll_usd': total_payroll_usd,
        'bonus_count': bonus_count,
        'bonus_total_mxn': bonus_total_mxn,
        'admin_bonus_total_mxn': admin_bonus_total_mxn,
        'ph_topup_total_mxn': ph_topup_total_mxn,
        'ot_1_5_topup_total_mxn': ot_1_5_topup_total_mxn,
        'agent_count': len(data),
    })


@login_required
@finance_access_required
def billing_report(request):
    week_start = _get_week_start(request)
    settings = BillingSettings.get_for_week(week_start)
    week_dates = _week_dates(week_start)
    week_end = week_dates[-1]

    # All billed agents (Infinity employer or LCC) with separation filter
    agents = Agent.objects.filter(
        five9_profiles__billable=True,
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
    ph_topup_total_mxn = sum(d.get('ph_topup_mxn', Decimal('0')) for d in data.values())
    ot_1_5_topup_total_mxn = sum(d.get('ot_1_5_topup_mxn', Decimal('0')) for d in data.values())
    ph_topup_total_usd = (ph_topup_total_mxn / settings.usd_to_mxn).quantize(Decimal('0.01'), ROUND_HALF_UP) if settings.usd_to_mxn else Decimal('0')
    ot_1_5_topup_total_usd = (ot_1_5_topup_total_mxn / settings.usd_to_mxn).quantize(Decimal('0.01'), ROUND_HALF_UP) if settings.usd_to_mxn else Decimal('0')
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
        'ph_topup_total_usd': ph_topup_total_usd,
        'ot_1_5_topup_total_usd': ot_1_5_topup_total_usd,
        'bonus_total_usd': bonus_total_usd,
    })


@login_required
@finance_access_required
def billing_export(request):
    """Export billing report as Excel."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    week_start = _get_week_start(request)
    settings = BillingSettings.get_for_week(week_start)
    week_dates = _week_dates(week_start)
    week_end = week_dates[-1]

    agents = Agent.objects.filter(
        five9_profiles__billable=True,
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
    ph_topup_mxn = sum(d.get('ph_topup_mxn', Decimal('0')) for d in data.values())
    ot_1_5_topup_mxn = sum(d.get('ot_1_5_topup_mxn', Decimal('0')) for d in data.values())
    ph_topup_usd = (ph_topup_mxn / settings.usd_to_mxn).quantize(Decimal('0.01')) if settings.usd_to_mxn else Decimal('0')
    ot_1_5_topup_usd = (ot_1_5_topup_mxn / settings.usd_to_mxn).quantize(Decimal('0.01')) if settings.usd_to_mxn else Decimal('0')
    bonus_mxn = sum(d.get('bonus_mxn', Decimal('0')) for d in data.values())
    bonus_usd = (bonus_mxn / settings.usd_to_mxn).quantize(Decimal('0.01')) if settings.usd_to_mxn else Decimal('0')

    ws.append(['TOTAL', '', '', '', '', '', '', float(total_hrs), '', float(total_usd)])
    ws.append(['Power Hour Top-up (MXN→USD equiv.)', '', '', '', '', '', '', '', '', float(ph_topup_usd)])
    ws.append(['1.5x OT Top-up (MXN→USD equiv.)', '', '', '', '', '', '', '', '', float(ot_1_5_topup_usd)])
    ws.append(['Adherence Bonus Total (USD equiv.)', '', '', '', '', '', '', '', '', float(bonus_usd)])

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
    week_start = _get_week_start(request)
    settings = BillingSettings.get_for_week(week_start)
    week_dates = _week_dates(week_start)
    week_end = week_dates[-1]

    agents = Agent.objects.filter(
        Q(track_attendance=True) | Q(five9_profiles__billable=True),
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
        'ph_topup_mxn': sum(r.get('ph_topup_mxn', Decimal('0')) for r in infinity_rows),
        'ot_1_5_topup_mxn': sum(r.get('ot_1_5_topup_mxn', Decimal('0')) for r in infinity_rows),
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

    week_start = _get_week_start(request)
    settings = BillingSettings.get_for_week(week_start)
    week_dates = _week_dates(week_start)
    week_end = week_dates[-1]

    agents = Agent.objects.filter(
        Q(track_attendance=True) | Q(five9_profiles__billable=True),
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
        '1.5x Top-up (MXN)', 'PH Top-up (MXN)',
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
                float(d.get('ot_1_5_topup_mxn', 0)),
                float(d.get('ph_topup_mxn', 0)),
                float(d.get('commission_pct', 0)),
                '—',
                float(d.get('total_pay_mxn', 0)),
                float(d.get('total_pay_usd', 0)),
            ])
            row_num += 1

    col_widths = [22, 22, 14, 20, 18, 12, 18, 16, 16, 16, 16, 16, 14, 18, 16, 18]
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
    today = date.today()
    current_week = today - timedelta(days=today.weekday())
    singleton = BillingSettings.get()

    if request.method == 'POST':
        try:
            week_str = request.POST.get('effective_week', '').strip()
            try:
                effective_week = date.fromisoformat(week_str)
                effective_week = effective_week - timedelta(days=effective_week.weekday())
            except (ValueError, TypeError):
                effective_week = current_week

            BillingSettingsHistory.objects.create(
                week_start=effective_week,
                changed_by=request.user,
                billing_rate_usd=Decimal(request.POST.get('billing_rate_usd', str(singleton.billing_rate_usd))),
                usd_to_mxn=Decimal(request.POST.get('usd_to_mxn', str(singleton.usd_to_mxn))),
                nr_cap_regular_hours=Decimal(request.POST.get('nr_cap_regular_hours', str(singleton.nr_cap_regular_hours))),
                nr_cap_kill_team_hours=Decimal(request.POST.get('nr_cap_kill_team_hours', str(singleton.nr_cap_kill_team_hours))),
                default_admin_bonus_mxn=Decimal(request.POST.get('default_admin_bonus_mxn', str(singleton.default_admin_bonus_mxn))),
                adherence_bonus_max_mxn=Decimal(request.POST.get('adherence_bonus_max_mxn', str(singleton.adherence_bonus_max_mxn))),
                adherence_bonus_full_hours=Decimal(request.POST.get('adherence_bonus_full_hours', str(singleton.adherence_bonus_full_hours))),
            )
            # Also update the singleton so it always reflects the latest values
            singleton.billing_rate_usd = Decimal(request.POST.get('billing_rate_usd', str(singleton.billing_rate_usd)))
            singleton.usd_to_mxn = Decimal(request.POST.get('usd_to_mxn', str(singleton.usd_to_mxn)))
            singleton.nr_cap_regular_hours = Decimal(request.POST.get('nr_cap_regular_hours', str(singleton.nr_cap_regular_hours)))
            singleton.nr_cap_kill_team_hours = Decimal(request.POST.get('nr_cap_kill_team_hours', str(singleton.nr_cap_kill_team_hours)))
            singleton.default_admin_bonus_mxn = Decimal(request.POST.get('default_admin_bonus_mxn', str(singleton.default_admin_bonus_mxn)))
            singleton.adherence_bonus_max_mxn = Decimal(request.POST.get('adherence_bonus_max_mxn', str(singleton.adherence_bonus_max_mxn)))
            singleton.adherence_bonus_full_hours = Decimal(request.POST.get('adherence_bonus_full_hours', str(singleton.adherence_bonus_full_hours)))
            singleton.save()
            messages.success(request, f"Settings saved — effective from week of {effective_week.strftime('%b %d, %Y')}.")
        except Exception as e:
            messages.error(request, f"Error saving settings: {e}")
        return redirect('finance_settings')

    # Current effective settings (latest history record)
    current = BillingSettings.get_for_week(current_week)
    history = BillingSettingsHistory.objects.order_by('-week_start', '-changed_at')[:50]

    return render(request, 'finance/settings.html', {
        'settings': current,
        'singleton': singleton,
        'history': history,
        'current_week': current_week,
    })


# ─── Admin Codings ────────────────────────────────────────────────────────────

@login_required
@finance_access_required
def admin_codings(request):
    """Codings for Official Admins and coordinators against their billable Five9 user."""
    from django.utils import timezone
    from adherence.models import Coding

    week_start = _get_week_start(request)
    week_dates = _week_dates(week_start)
    week_end = week_dates[-1]

    # Official Admins who have a billable Five9 profile
    agents = Agent.objects.filter(
        status='active',
        five9_profiles__billable=True,
        is_official_admin=True,
    ).distinct().select_related('user', 'supervisor__user').order_by(
        'user__last_name', 'user__first_name'
    )

    # Billable username display map
    agent_ids = [a.pk for a in agents]
    billable_display_map = {}
    for p in Five9Profile.objects.filter(
        agent__in=agent_ids, billable=True
    ).values('agent_id', 'five9_username', 'is_primary').order_by('agent_id', '-is_primary', 'id'):
        if p['agent_id'] not in billable_display_map:
            billable_display_map[p['agent_id']] = p['five9_username']

    # Build coding map (admin codings only)
    codings_qs = Coding.objects.filter(
        date__in=week_dates, agent__in=agents, is_admin_coding=True
    ).select_related('agent__user').order_by('start_time')
    coding_map = {}
    for c in codings_qs:
        coding_map.setdefault((c.agent_id, c.date), []).append(c)

    rows = []
    for agent in agents:
        cells = []
        agent_total_seconds = 0
        for day_date in week_dates:
            entries = coding_map.get((agent.pk, day_date), [])
            day_seconds = sum(e.total_seconds_count() for e in entries)
            agent_total_seconds += day_seconds
            cells.append({'date': day_date, 'entries': entries, 'total_seconds': day_seconds})
        rows.append({
            'agent': agent,
            'billable_five9_username': billable_display_map.get(agent.pk, ''),
            'cells': cells,
            'total_seconds': agent_total_seconds,
        })

    return render(request, 'finance/admin_codings.html', {
        'rows': rows,
        'week_dates': week_dates,
        'week_start': week_start,
        'week_end': week_end,
        'today': timezone.localdate(),
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
    })


@login_required
@finance_access_required
def add_admin_coding_ajax(request):
    import json as _json
    from django.views.decorators.http import require_POST as _rp
    from adherence.models import Coding
    from datetime import time as time_cls

    data = _json.loads(request.body)
    agent_id = data.get('agent_id')
    date_str = data.get('date')
    start_time = data.get('start_time', '').strip()
    end_time = data.get('end_time', '').strip()
    notes = data.get('notes', '')

    if not all([agent_id, date_str, start_time, end_time]):
        return JsonResponse({'ok': False, 'error': 'missing fields'}, status=400)

    def _pad(s):
        parts = s.split(':')
        if parts:
            parts[0] = parts[0].zfill(2)
        return ':'.join(parts)

    start_time = _pad(start_time)
    end_time = _pad(end_time)

    try:
        start_t = time_cls.fromisoformat(start_time)
        end_t = time_cls.fromisoformat(end_time)
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid time format. Use H:MM:SS'}, status=400)

    if end_t <= start_t:
        return JsonResponse({'ok': False, 'error': 'End time must be after start time.'}, status=400)

    try:
        coding = Coding.objects.create(
            agent_id=agent_id, date=date_str,
            start_time=start_time, end_time=end_time,
            notes=notes, is_admin_coding=True,
        )
        coding.refresh_from_db()
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)

    return JsonResponse({
        'ok': True, 'id': coding.pk,
        'hhmmss': coding.total_hhmmss(),
        'start': coding.start_time.strftime('%H:%M'),
        'end': coding.end_time.strftime('%H:%M'),
        'start_full': coding.start_time.strftime('%H:%M:%S'),
        'end_full': coding.end_time.strftime('%H:%M:%S'),
        'notes': coding.notes,
    })


@login_required
@finance_access_required
def edit_admin_coding_ajax(request):
    import json as _json
    from adherence.models import Coding
    from datetime import time as time_cls

    data = _json.loads(request.body)
    coding_id = data.get('coding_id')
    start_time = data.get('start_time', '').strip()
    end_time = data.get('end_time', '').strip()
    notes = data.get('notes', '')

    coding = Coding.objects.filter(pk=coding_id, is_admin_coding=True).first()
    if not coding:
        return JsonResponse({'ok': False, 'error': 'Not found'}, status=404)

    def _pad(s):
        parts = s.split(':')
        if parts:
            parts[0] = parts[0].zfill(2)
        return ':'.join(parts)

    try:
        start_t = time_cls.fromisoformat(_pad(start_time))
        end_t = time_cls.fromisoformat(_pad(end_time))
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid time format.'}, status=400)

    if end_t <= start_t:
        return JsonResponse({'ok': False, 'error': 'End time must be after start time.'}, status=400)

    coding.start_time = start_t
    coding.end_time = end_t
    coding.notes = notes
    coding.save()

    return JsonResponse({
        'ok': True, 'id': coding.pk,
        'hhmmss': coding.total_hhmmss(),
        'start': coding.start_time.strftime('%H:%M'),
        'end': coding.end_time.strftime('%H:%M'),
        'start_full': coding.start_time.strftime('%H:%M:%S'),
        'end_full': coding.end_time.strftime('%H:%M:%S'),
        'notes': coding.notes,
    })


@login_required
@finance_access_required
def delete_admin_coding_ajax(request):
    import json as _json
    from adherence.models import Coding

    data = _json.loads(request.body)
    coding_id = data.get('coding_id')
    Coding.objects.filter(pk=coding_id, is_admin_coding=True).delete()
    return JsonResponse({'ok': True})


# ─── Admin Adherence ──────────────────────────────────────────────────────────

@login_required
@finance_access_required
def admin_adherence(request):
    """Adherence tab for Official Admins only — super admin access."""
    from adherence.views import _build_maps, _build_rows
    from adherence.models import AdherenceRecord, AdherenceNote, Coding as _Coding
    from django.utils import timezone as _tz
    from django.db.models import Count as _Count

    week_start = _get_week_start(request)
    week_dates = _week_dates(week_start)
    week_end = week_dates[-1]

    agents = Agent.objects.filter(
        status='active',
        is_official_admin=True,
    ).select_related('user', 'supervisor__user').prefetch_related('five9_profiles').order_by(
        'user__last_name', 'user__first_name'
    )
    agents = list(agents)

    # Shift/record/OT maps from adherence logic
    shift_map, record_map, _, ot_map, extra_hrs_map, split_labels_map, tmpl_by_agent_dow = _build_maps(agents, week_dates)

    # Coded map uses admin codings, not regular codings
    coded_map = {}
    for c in _Coding.objects.filter(date__in=week_dates, agent__in=agents, is_admin_coding=True):
        coded_map[(c.agent_id, c.date)] = coded_map.get((c.agent_id, c.date), Decimal('0')) + Decimal(str(c.total_hours()))

    rows = _build_rows(agents, week_dates, shift_map, record_map, coded_map, ot_map=ot_map,
                       extra_hrs_map=extra_hrs_map, split_labels_map=split_labels_map,
                       tmpl_by_agent_dow=tmpl_by_agent_dow)

    # Replace adherence bonus with fixed admin bonus for each row
    billing_settings = BillingSettings.get_for_week(week_start)
    billable_five9_map = {}
    for p in Five9Profile.objects.filter(
        agent__in=[a.pk for a in agents], billable=True
    ).values('agent_id', 'five9_username', 'is_primary').order_by('agent_id', '-is_primary', 'id'):
        if p['agent_id'] not in billable_five9_map:
            billable_five9_map[p['agent_id']] = p['five9_username']

    for row in rows:
        agent = row['agent']
        admin_bonus = (
            agent.admin_bonus_mxn if agent.admin_bonus_mxn is not None
            else billing_settings.default_admin_bonus_mxn
        )
        row['bonus'] = 'Admin'
        row['bonus_mxn'] = admin_bonus
        row['admin_bonus_mxn'] = admin_bonus
        row['billable_five9_username'] = billable_five9_map.get(agent.pk, '')

    # Note counts
    note_count_map = {
        (n['agent_id'], n['date']): n['count']
        for n in AdherenceNote.objects.filter(
            agent__in=agents, date__in=week_dates
        ).values('agent_id', 'date').annotate(count=_Count('pk'))
    }
    for row in rows:
        for cell in row['cells']:
            cell['note_count'] = note_count_map.get((row['agent'].pk, cell['date']), 0)

    return render(request, 'finance/admin_adherence.html', {
        'rows': rows,
        'week_dates': week_dates,
        'week_start': week_start,
        'week_end': week_end,
        'today': _tz.localdate(),
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
        'status_choices': AdherenceRecord.STATUS_CHOICES,
    })


@login_required
@finance_access_required
def admin_adherence_export(request):
    """Export admin adherence payroll as Excel."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from adherence.views import _build_maps, _build_rows
    from adherence.models import Coding as _Coding

    week_start = _get_week_start(request)
    billing_settings = BillingSettings.get_for_week(week_start)
    week_dates = _week_dates(week_start)
    week_end = week_dates[-1]

    agents = Agent.objects.filter(
        status='active',
        is_official_admin=True,
    ).select_related('user', 'supervisor__user').prefetch_related('five9_profiles').order_by(
        'user__last_name', 'user__first_name'
    )
    agents = list(agents)

    shift_map, record_map, _, ot_map, extra_hrs_map, split_labels_map, tmpl_by_agent_dow = _build_maps(agents, week_dates)

    coded_map = {}
    for c in _Coding.objects.filter(date__in=week_dates, agent__in=agents, is_admin_coding=True):
        coded_map[(c.agent_id, c.date)] = coded_map.get((c.agent_id, c.date), Decimal('0')) + Decimal(str(c.total_hours()))

    rows = _build_rows(agents, week_dates, shift_map, record_map, coded_map, ot_map=ot_map,
                       extra_hrs_map=extra_hrs_map, split_labels_map=split_labels_map,
                       tmpl_by_agent_dow=tmpl_by_agent_dow)

    billable_five9_map = {}
    for p in Five9Profile.objects.filter(
        agent__in=[a.pk for a in agents], billable=True
    ).values('agent_id', 'five9_username', 'is_primary').order_by('agent_id', '-is_primary', 'id'):
        if p['agent_id'] not in billable_five9_map:
            billable_five9_map[p['agent_id']] = p['five9_username']

    usd_to_mxn = billing_settings.usd_to_mxn

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Admin Payroll"

    headers = [
        'Agent Name', 'Legal Name', 'Employee ID', 'Five9 User',
        'Sch Hrs', 'Login Hrs', 'Coded Hrs', 'NR Cap Adj', 'Total Hrs',
        'Hourly Rate (MXN)', 'Base Pay (MXN)', 'Admin Bonus (MXN)',
        'Total Pay (MXN)', 'Total Pay (USD)',
    ]
    header_font = Font(bold=True, color='FFFFFF')
    header_fill = PatternFill('solid', fgColor='1A3A5C')
    center = Alignment(horizontal='center')

    ws.append([f"Admin Payroll — Week of {week_start.strftime('%B %d, %Y')} to {week_end.strftime('%B %d, %Y')}"])
    ws.merge_cells(f'A1:{get_column_letter(len(headers))}1')
    ws['A1'].font = Font(bold=True, size=13)
    ws.append([])
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        c = ws.cell(row=3, column=col)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center

    for row in rows:
        agent = row['agent']
        admin_bonus = (
            agent.admin_bonus_mxn if agent.admin_bonus_mxn is not None
            else billing_settings.default_admin_bonus_mxn
        )
        hourly_mxn = agent.hourly_rate or Decimal('0')
        final_hrs = row['final_adjusted']
        base_pay = (final_hrs * hourly_mxn).quantize(Decimal('0.01'), ROUND_HALF_UP)
        total_pay_mxn = base_pay + admin_bonus
        total_pay_usd = (total_pay_mxn / usd_to_mxn).quantize(Decimal('0.01'), ROUND_HALF_UP) if usd_to_mxn else Decimal('0')

        ws.append([
            str(agent),
            agent.user.get_full_name(),
            agent.employee_id or '',
            billable_five9_map.get(agent.pk, ''),
            float(row['sched_hours'] or 0),
            float(row['actual_hours'] or 0),
            float(row['coded_hours'] or 0),
            float(row.get('nr_cap_adj') or 0),
            float(final_hrs or 0),
            float(hourly_mxn),
            float(base_pay),
            float(admin_bonus),
            float(total_pay_mxn),
            float(total_pay_usd),
        ])

    col_widths = [22, 22, 14, 20, 10, 10, 10, 12, 10, 18, 16, 18, 16, 16]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = (
        f'attachment; filename="admin_payroll_{week_start.isoformat()}.xlsx"'
    )
    wb.save(response)
    return response
