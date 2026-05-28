from datetime import date, timedelta, datetime
from decimal import Decimal, InvalidOperation
import csv

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.utils import timezone
from django.http import HttpResponse

from scheduling.models import Shift, Agent
from .models import AdherenceRecord, Coding, PayrollAdjustment


BONUS_QUALIFYING = {'P', 'OT', 'MUT', 'VTO', 'P+VTO'}
BONUS_DISQUALIFYING = {'Absent', 'NCNS', 'T', 'T+VTO', 'I'}

STATUS_COLORS = {
    'P':     '#e8f5e9',
    'OT':    '#c8e6c9',
    'MUT':   '#dcedc8',
    'VTO':   '#f1f8e9',
    'P+VTO': '#e8f5e9',
    'T':     '#fff3e0',
    'T+VTO': '#fff8e1',
    'Absent':'#ffebee',
    'NCNS':  '#ffcdd2',
    'I':     '#fce4ec',
    'Quit':  '#eeeeee',
    'Baja':  '#eeeeee',
    'V':     '#e3f2fd',
    'IMSS':  '#e0f2f1',
}


def _parse_decimal(val):
    try:
        return Decimal(val.strip()) if val and val.strip() else None
    except (InvalidOperation, AttributeError):
        return None


def _scheduled_hours(shift):
    if not shift or shift.is_off:
        return Decimal('0')
    start = datetime.combine(date.today(), shift.start_time)
    end = datetime.combine(date.today(), shift.end_time)
    delta = end - start
    if delta.total_seconds() < 0:
        delta += timedelta(days=1)
    return Decimal(str(round(delta.total_seconds() / 3600, 2)))


def _get_week_start(request):
    today = timezone.localdate()
    default = today - timedelta(days=today.weekday())
    raw = request.GET.get('week_start') or request.POST.get('week_start')
    try:
        ws = date.fromisoformat(raw) if raw else default
        return ws - timedelta(days=ws.weekday())
    except ValueError:
        return default


def _build_maps(agents, week_dates):
    shifts_qs = Shift.objects.filter(date__in=week_dates, agent__in=agents)
    shift_map = {(s.agent_id, s.date): s for s in shifts_qs}

    records_qs = AdherenceRecord.objects.filter(date__in=week_dates, agent__in=agents)
    record_map = {(r.agent_id, r.date): r for r in records_qs}

    codings_qs = Coding.objects.filter(date__in=week_dates, agent__in=agents)
    coded_map = {}
    for c in codings_qs:
        h = Decimal(str(c.total_hours()))
        coded_map[c.agent_id] = coded_map.get(c.agent_id, Decimal('0')) + h

    return shift_map, record_map, coded_map


def _build_rows(agents, week_dates, shift_map, record_map, coded_map):
    rows = []
    for agent in agents:
        cells = []
        total_present = 0
        total_absent = 0
        sched_total = Decimal('0')
        actual_total = Decimal('0')
        bonus = True          # True = eligible, False = disqualified, None = incomplete
        bonus_determined = False

        for day_date in week_dates:
            shift = shift_map.get((agent.pk, day_date))
            record = record_map.get((agent.pk, day_date))

            sched_hrs = _scheduled_hours(shift)
            is_off = shift.is_off if shift else False
            has_shift = shift is not None

            if has_shift and not is_off:
                sched_total += sched_hrs
                status = record.status if record else ''
                actual_hrs = record.actual_hours if record else None

                # Totals
                if status in ('P', 'OT', 'MUT', 'VTO', 'P+VTO', 'T', 'T+VTO', 'I'):
                    total_present += 1
                elif status in ('Absent', 'NCNS'):
                    total_absent += 1

                if actual_hrs:
                    actual_total += actual_hrs

                # Bonus logic
                if status in BONUS_DISQUALIFYING:
                    bonus = False
                    bonus_determined = True
                elif status in BONUS_QUALIFYING:
                    bonus_determined = True
                elif status == '':
                    pass  # not yet filled
                else:
                    # V, IMSS, Quit, Baja, etc — not qualifying
                    bonus = False
                    bonus_determined = True
            else:
                status = ''
                actual_hrs = record.actual_hours if record else None

            # Cell color
            if not has_shift:
                cell_color = '#fafafa'
            elif is_off:
                cell_color = '#f0f0f0'
            elif status:
                base = STATUS_COLORS.get(status, '#fff')
                # Override with hours-based color if actual hours are entered
                if actual_hrs is not None and sched_hrs > 0:
                    if actual_hrs >= sched_hrs:
                        cell_color = '#e8f5e9'   # green — met hours
                    else:
                        cell_color = '#fff3e0' if status not in ('Absent', 'NCNS') else '#ffcdd2'
                else:
                    cell_color = base
            else:
                cell_color = '#fff'

            cells.append({
                'date': day_date,
                'scheduled': (
                    'Off' if is_off
                    else f"{shift.start_time.strftime('%H:%M')}–{shift.end_time.strftime('%H:%M')}" if shift
                    else ''
                ),
                'sched_hrs': sched_hrs if has_shift and not is_off else None,
                'is_off': is_off,
                'has_shift': has_shift,
                'status': status,
                'actual_hrs': actual_hrs if actual_hrs is not None else '',
                'color': cell_color,
                'key': f'status_{agent.pk}_{day_date.isoformat()}',
                'hours_key': f'hours_{agent.pk}_{day_date.isoformat()}',
            })

        coded = coded_map.get(agent.pk, Decimal('0'))
        adjusted = actual_total + coded

        if bonus is False:
            bonus_display = 'No'
        elif bonus_determined:
            bonus_display = 'Yes'
        else:
            bonus_display = '—'

        rows.append({
            'agent': agent,
            'cells': cells,
            'total_present': total_present,
            'total_absent': total_absent,
            'sched_hours': sched_total,
            'actual_hours': actual_total,
            'coded_hours': coded,
            'adjusted_total': adjusted,
            'bonus': bonus_display,
        })

    return rows


@login_required
def adherence_week(request):
    week_start = _get_week_start(request)
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    week_end = week_dates[-1]

    agents = Agent.objects.filter(status='active').select_related('user').order_by(
        'user__last_name', 'user__first_name'
    )

    if request.method == 'POST':
        shift_map, record_map, _ = _build_maps(agents, week_dates)
        for agent in agents:
            for day_date in week_dates:
                status_val = request.POST.get(f'status_{agent.pk}_{day_date.isoformat()}', '')
                hours_val = request.POST.get(f'hours_{agent.pk}_{day_date.isoformat()}', '')
                actual_hrs = _parse_decimal(hours_val)

                if status_val or actual_hrs is not None:
                    AdherenceRecord.objects.update_or_create(
                        agent=agent,
                        date=day_date,
                        defaults={
                            'status': status_val,
                            'actual_hours': actual_hrs,
                        },
                    )
                else:
                    AdherenceRecord.objects.filter(agent=agent, date=day_date).delete()

        return redirect(reverse('adherence_dashboard') + f'?week_start={week_start.isoformat()}')

    shift_map, record_map, coded_map = _build_maps(agents, week_dates)
    rows = _build_rows(agents, week_dates, shift_map, record_map, coded_map)

    return render(request, 'adherence/dashboard.html', {
        'rows': rows,
        'week_dates': week_dates,
        'week_start': week_start,
        'week_end': week_end,
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
        'status_choices': AdherenceRecord.STATUS_CHOICES,
    })


@login_required
def codings_week(request):
    week_start = _get_week_start(request)
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    week_end = week_dates[-1]

    agents = Agent.objects.filter(status='active').select_related('user').order_by(
        'user__last_name', 'user__first_name'
    )

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add':
            agent_id = request.POST.get('agent_id')
            coding_date = request.POST.get('coding_date')
            start_time = request.POST.get('start_time')
            end_time = request.POST.get('end_time')
            notes = request.POST.get('notes', '')
            if agent_id and coding_date and start_time and end_time:
                Coding.objects.create(
                    agent_id=agent_id,
                    date=coding_date,
                    start_time=start_time,
                    end_time=end_time,
                    notes=notes,
                )

        elif action == 'delete':
            coding_id = request.POST.get('coding_id')
            Coding.objects.filter(pk=coding_id).delete()

        return redirect(reverse('codings_week') + f'?week_start={week_start.isoformat()}')

    codings = (
        Coding.objects
        .filter(date__in=week_dates)
        .select_related('agent__user')
        .order_by('date', 'agent__user__last_name', 'start_time')
    )

    return render(request, 'adherence/codings.html', {
        'codings': codings,
        'agents': agents,
        'week_dates': week_dates,
        'week_start': week_start,
        'week_end': week_end,
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
    })


@login_required
def payroll_export(request):
    week_start = _get_week_start(request)
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    week_end = week_dates[-1]

    agents = Agent.objects.filter(status='active').select_related('user').order_by(
        'user__last_name', 'user__first_name'
    )

    if request.method == 'POST':
        # Save commission deductions
        for agent in agents:
            val = request.POST.get(f'deduction_{agent.pk}', '0')
            deduction = _parse_decimal(val) or Decimal('0')
            PayrollAdjustment.objects.update_or_create(
                agent=agent,
                week_start=week_start,
                defaults={'commission_deduction': deduction},
            )

        shift_map, record_map, coded_map = _build_maps(agents, week_dates)
        adj_map = {
            pa.agent_id: pa.commission_deduction
            for pa in PayrollAdjustment.objects.filter(week_start=week_start, agent__in=agents)
        }

        response = HttpResponse(content_type='text/csv')
        filename = f'payroll_{week_start.isoformat()}_to_{week_end.isoformat()}.csv'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            'Legal Name', 'Agent Name', 'Five9 Username',
            'Scheduled Hours', 'Actual Login Hours', 'Coded Hours',
            'Adjusted Total Hours', 'Commission Deduction', 'Adherence Bonus',
        ])

        for agent in agents:
            sched_total = Decimal('0')
            actual_total = Decimal('0')
            bonus = True
            bonus_determined = False

            for day_date in week_dates:
                shift = shift_map.get((agent.pk, day_date))
                record = record_map.get((agent.pk, day_date))
                if shift and not shift.is_off:
                    sched_total += _scheduled_hours(shift)
                    status = record.status if record else ''
                    if status in BONUS_DISQUALIFYING:
                        bonus = False
                        bonus_determined = True
                    elif status in BONUS_QUALIFYING:
                        bonus_determined = True
                    elif status:
                        bonus = False
                        bonus_determined = True
                    if record and record.actual_hours:
                        actual_total += record.actual_hours

            coded = coded_map.get(agent.pk, Decimal('0'))
            adjusted = actual_total + coded
            commission = adj_map.get(agent.pk, Decimal('0'))
            bonus_label = 'Yes' if (bonus and bonus_determined) else 'No'

            writer.writerow([
                agent.user.get_full_name(),
                agent.agent_name or '',
                agent.five9_username or '',
                f'{sched_total:.2f}',
                f'{actual_total:.2f}',
                f'{coded:.2f}',
                f'{adjusted:.2f}',
                f'{commission:.2f}',
                bonus_label,
            ])

        return response

    # GET — show preview with editable deductions
    shift_map, record_map, coded_map = _build_maps(agents, week_dates)
    rows = _build_rows(agents, week_dates, shift_map, record_map, coded_map)

    adj_map = {
        pa.agent_id: pa.commission_deduction
        for pa in PayrollAdjustment.objects.filter(week_start=week_start, agent__in=agents)
    }
    for row in rows:
        row['commission_deduction'] = adj_map.get(row['agent'].pk, Decimal('0'))

    return render(request, 'adherence/payroll_export.html', {
        'rows': rows,
        'week_start': week_start,
        'week_end': week_end,
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
    })
