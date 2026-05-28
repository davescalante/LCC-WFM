from datetime import date, timedelta, datetime
from decimal import Decimal, InvalidOperation
import csv
import json

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.http import HttpResponse, JsonResponse

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


def _parse_hours_input(val):
    """Accept HH:MM:SS, HH:MM, or plain decimal hours string."""
    if not val or not val.strip():
        return None
    val = val.strip()
    if ':' in val:
        parts = val.split(':')
        try:
            h = int(parts[0]) if parts[0] else 0
            m = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            s = int(parts[2]) if len(parts) > 2 and parts[2] else 0
            return (Decimal(h) + Decimal(m) / 60 + Decimal(s) / 3600).quantize(Decimal('0.000001'))
        except (ValueError, IndexError, InvalidOperation):
            return None
    try:
        return Decimal(val)
    except InvalidOperation:
        return None


def _decimal_to_hhmmss(value):
    """Decimal hours → HH:MM:SS string for CSV export."""
    if not value:
        return '00:00:00'
    total_seconds = round(float(value) * 3600)
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f'{h:02d}:{m:02d}:{s:02d}'


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


# ── AJAX endpoints ────────────────────────────────────────────────────────────

@login_required
@require_POST
def save_commission(request):
    data = json.loads(request.body)
    agent_id = data.get('agent_id')
    week_start_str = data.get('week_start')
    amount_str = data.get('amount', '').strip()

    try:
        week_start = date.fromisoformat(week_start_str)
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid date'}, status=400)

    try:
        amount = Decimal(amount_str) if amount_str else Decimal('0')
    except InvalidOperation:
        return JsonResponse({'ok': False, 'error': 'invalid amount'}, status=400)

    agent = get_object_or_404(Agent, pk=agent_id)
    PayrollAdjustment.objects.update_or_create(
        agent=agent,
        week_start=week_start,
        defaults={'commission_deduction': amount},
    )
    return JsonResponse({'ok': True})

@login_required
@require_POST
def save_adherence_cell(request):
    data = json.loads(request.body)
    agent_id = data.get('agent_id')
    date_str = data.get('date')
    status = data.get('status', '')
    hours_str = data.get('actual_hours', '')

    try:
        day_date = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid date'}, status=400)

    agent = get_object_or_404(Agent, pk=agent_id)
    actual_hrs = _parse_hours_input(hours_str)

    if status or actual_hrs is not None:
        AdherenceRecord.objects.update_or_create(
            agent=agent,
            date=day_date,
            defaults={'status': status, 'actual_hours': actual_hrs},
        )
    else:
        AdherenceRecord.objects.filter(agent=agent, date=day_date).delete()

    return JsonResponse({'ok': True})


@login_required
@require_POST
def add_coding_ajax(request):
    data = json.loads(request.body)
    agent_id = data.get('agent_id')
    date_str = data.get('date')
    start_time = data.get('start_time')
    end_time = data.get('end_time')
    notes = data.get('notes', '')

    if not all([agent_id, date_str, start_time, end_time]):
        return JsonResponse({'ok': False, 'error': 'missing fields'}, status=400)

    # Validate time format and end > start
    from datetime import time as time_cls
    try:
        start_t = time_cls.fromisoformat(start_time)
        end_t   = time_cls.fromisoformat(end_time)
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid time format. Use HH:MM:SS (e.g. 16:00:00)'}, status=400)

    if end_t <= start_t:
        return JsonResponse({'ok': False, 'error': 'End time must be after start time. If the shift crossed midnight, split it into two entries.'}, status=400)

    try:
        coding = Coding.objects.create(
            agent_id=agent_id,
            date=date_str,
            start_time=start_time,
            end_time=end_time,
            notes=notes,
        )
        coding.refresh_from_db()  # SQLite returns raw strings; refresh to get proper time objects
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)

    return JsonResponse({
        'ok': True,
        'id': coding.pk,
        'hhmmss': coding.total_hhmmss(),
        'start': coding.start_time.strftime('%H:%M'),
        'end': coding.end_time.strftime('%H:%M'),
        'notes': coding.notes,
    })


@login_required
@require_POST
def delete_coding_ajax(request):
    data = json.loads(request.body)
    coding_id = data.get('coding_id')
    Coding.objects.filter(pk=coding_id).delete()
    return JsonResponse({'ok': True})


# ── Page views ────────────────────────────────────────────────────────────────

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
                actual_hrs = _parse_hours_input(hours_val)

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

    adj_map = {
        pa.agent_id: pa.commission_deduction
        for pa in PayrollAdjustment.objects.filter(week_start=week_start, agent__in=agents)
    }
    for row in rows:
        row['commission_deduction'] = adj_map.get(row['agent'].pk, Decimal('0'))

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

    # Build coding map: {(agent_id, date): [coding, ...]}
    codings_qs = (
        Coding.objects
        .filter(date__in=week_dates, agent__in=agents)
        .select_related('agent__user')
        .order_by('start_time')
    )
    coding_map = {}
    for c in codings_qs:
        key = (c.agent_id, c.date)
        coding_map.setdefault(key, []).append(c)

    rows = []
    for agent in agents:
        cells = []
        agent_total_seconds = 0
        for day_date in week_dates:
            entries = coding_map.get((agent.pk, day_date), [])
            day_seconds = sum(e.total_seconds_count() for e in entries)
            agent_total_seconds += day_seconds
            cells.append({
                'date': day_date,
                'entries': entries,
                'total_seconds': day_seconds,
            })
        rows.append({
            'agent': agent,
            'cells': cells,
            'total_seconds': agent_total_seconds,
        })

    # Only include rows that have at least one coding OR always show all — show all for adding
    return render(request, 'adherence/codings.html', {
        'rows': rows,
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
        action = request.POST.get('action', 'export_payroll')

        # Save commission deductions regardless of which export is triggered
        for agent in agents:
            val = request.POST.get(f'deduction_{agent.pk}', '0')
            deduction = _parse_hours_input(val) or Decimal('0')
            PayrollAdjustment.objects.update_or_create(
                agent=agent,
                week_start=week_start,
                defaults={'commission_deduction': deduction},
            )

        if action == 'export_codings':
            codings_qs = (
                Coding.objects
                .filter(date__in=week_dates, agent__in=agents)
                .select_related('agent__user')
                .order_by('date', 'agent__user__last_name', 'agent__user__first_name', 'start_time')
            )

            response = HttpResponse(content_type='text/csv')
            filename = f'codings_{week_start.isoformat()}_to_{week_end.isoformat()}.csv'
            response['Content-Disposition'] = f'attachment; filename="{filename}"'

            writer = csv.writer(response)
            writer.writerow([
                'Legal Name', 'Agent Name', 'Five9 Username',
                'Date', 'Day', 'Start Time', 'End Time',
                'Total Coded Time', 'Notes',
            ])
            for c in codings_qs:
                writer.writerow([
                    c.agent.user.get_full_name(),
                    c.agent.agent_name or '',
                    c.agent.five9_username or '',
                    c.date.strftime('%Y-%m-%d'),
                    c.date.strftime('%A'),
                    c.start_time.strftime('%H:%M'),
                    c.end_time.strftime('%H:%M'),
                    c.total_hhmmss(),
                    c.notes,
                ])
            return response

        # Default: export payroll summary
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
                _decimal_to_hhmmss(sched_total),
                _decimal_to_hhmmss(actual_total),
                _decimal_to_hhmmss(coded),
                _decimal_to_hhmmss(adjusted),
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
