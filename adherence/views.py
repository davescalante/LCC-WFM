from datetime import date, timedelta, datetime
from decimal import Decimal, InvalidOperation
import csv
import io
import json

from django.db.models import Q, Count as DbCount

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.http import HttpResponse, JsonResponse

from scheduling.models import Shift, ShiftTemplate, Agent, Five9Profile, OvertimeShift, log_action
from .models import AdherenceRecord, AdherenceNote, Coding, PayrollAdjustment, DailyUpload, DailyAgentHours


BONUS_QUALIFYING = {'P', 'OT', 'MUT', 'VTO', 'P+VTO'}
BONUS_DISQUALIFYING = {'Absent', 'NCNS', 'T', 'T+VTO', 'I', 'LOA'}
VTO_STATUSES = {'VTO', 'P+VTO', 'T+VTO', 'LOA'}

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
    'LOA':   '#f3e5f5',
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
    return Decimal(str(round(delta.total_seconds() / 3600, 6)))


def _ot_hours(ot_shift):
    if not ot_shift:
        return Decimal('0')
    start = datetime.combine(date.today(), ot_shift.start_time)
    end = datetime.combine(date.today(), ot_shift.end_time)
    delta = end - start
    if delta.total_seconds() < 0:
        delta += timedelta(days=1)
    return Decimal(str(round(delta.total_seconds() / 3600, 6)))


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

    # Fill gaps with recurring templates where no specific Shift record exists
    template_qs = ShiftTemplate.objects.filter(agent__in=agents)
    template_map = {(t.agent_id, t.day_of_week): t for t in template_qs}
    agent_ids = [a.pk for a in agents]
    for day_date in week_dates:
        for agent_id in agent_ids:
            if (agent_id, day_date) not in shift_map:
                tmpl = template_map.get((agent_id, day_date.weekday()))
                if tmpl:
                    shift_map[(agent_id, day_date)] = tmpl

    records_qs = AdherenceRecord.objects.filter(date__in=week_dates, agent__in=agents)
    record_map = {(r.agent_id, r.date): r for r in records_qs}

    codings_qs = Coding.objects.filter(date__in=week_dates, agent__in=agents)
    coded_map = {}
    for c in codings_qs:
        key = (c.agent_id, c.date)
        coded_map[key] = coded_map.get(key, Decimal('0')) + Decimal(str(c.total_hours()))

    ot_qs = OvertimeShift.objects.filter(date__in=week_dates, agent__in=agents)
    ot_map = {(s.agent_id, s.date): s for s in ot_qs}

    return shift_map, record_map, coded_map, ot_map


def _build_rows(agents, week_dates, shift_map, record_map, coded_map, ot_map=None):
    rows = []
    for agent in agents:
        cells = []
        total_present = 0
        total_absent = 0
        total_tardy = 0
        total_incomplete = 0
        sched_total = Decimal('0')
        actual_total = Decimal('0')
        bonus = True          # True = eligible, False = disqualified, None = incomplete
        bonus_determined = False

        for day_date in week_dates:
            shift = shift_map.get((agent.pk, day_date))
            record = record_map.get((agent.pk, day_date))
            ot_shift = (ot_map or {}).get((agent.pk, day_date))

            sched_hrs = _scheduled_hours(shift)
            ot_hrs = _ot_hours(ot_shift)
            is_off = shift.is_off if shift else False
            has_shift = shift is not None

            # A day is scheduled if there's a non-off regular shift OR an OT shift
            is_scheduled_day = (has_shift and not is_off) or bool(ot_shift)

            effective_sched = Decimal('0')

            # Always read status and actual hours from the record
            status = record.status if record else ''
            actual_hrs = record.actual_hours if record else None

            # Scheduled hours only apply when a shift is set up
            if is_scheduled_day:
                raw_sched = sched_hrs + ot_hrs
                if status in ('VTO', 'LOA'):
                    effective_sched = Decimal('0')
                elif status in ('P+VTO', 'T+VTO') and actual_hrs is not None:
                    effective_sched = min(actual_hrs, raw_sched)
                else:
                    effective_sched = raw_sched
                sched_total += effective_sched

            # Present/A/T/I counts and bonus apply whenever a status is set
            if status:
                if status in ('P', 'OT', 'MUT', 'VTO', 'P+VTO', 'T', 'T+VTO', 'I'):
                    total_present += 1
                elif status in ('Absent', 'NCNS'):
                    total_absent += 1
                if status in ('T', 'T+VTO'):
                    total_tardy += 1
                if status == 'I':
                    total_incomplete += 1

                if status in BONUS_DISQUALIFYING:
                    bonus = False
                    bonus_determined = True
                elif status in BONUS_QUALIFYING:
                    bonus_determined = True
                else:
                    bonus = False
                    bonus_determined = True

            # Login hours accumulate whenever actual hours exist
            if actual_hrs:
                actual_total += actual_hrs

            cell_coded_hrs = coded_map.get((agent.pk, day_date), Decimal('0'))

            # Cell color — use total (login + codings) vs effective scheduled for color logic
            cell_total = (actual_hrs or Decimal('0')) + cell_coded_hrs
            if not has_shift and not ot_shift:
                cell_color = '#fafafa'
            elif is_off and not ot_shift:
                cell_color = STATUS_COLORS.get(status, '#f0f0f0') if status else '#f0f0f0'
            elif status:
                base = STATUS_COLORS.get(status, '#fff')
                if (actual_hrs is not None or cell_coded_hrs) and effective_sched > 0:
                    if cell_total >= effective_sched:
                        cell_color = '#e8f5e9'
                    else:
                        cell_color = '#fff3e0' if status not in ('Absent', 'NCNS') else '#ffcdd2'
                else:
                    cell_color = base
            else:
                cell_color = '#fff'

            cell_sched_hrs = effective_sched if is_scheduled_day else None
            if cell_sched_hrs and (actual_hrs is not None or cell_coded_hrs) and cell_sched_hrs > cell_total:
                missing_hrs = cell_sched_hrs - cell_total
            else:
                missing_hrs = None

            display_hrs = cell_total if (actual_hrs is not None or cell_coded_hrs) else None

            cells.append({
                'date': day_date,
                'scheduled': (
                    'Off' if (is_off and not ot_shift)
                    else f"{shift.start_time.strftime('%H:%M')}–{shift.end_time.strftime('%H:%M')}" if shift and not is_off
                    else ''
                ),
                'ot_time': f"{ot_shift.start_time.strftime('%H:%M')}–{ot_shift.end_time.strftime('%H:%M')}" if ot_shift else None,
                'sched_hrs': cell_sched_hrs,
                'missing_hrs': missing_hrs,
                'is_off': is_off and not ot_shift,
                'has_shift': is_scheduled_day or (has_shift and is_off),
                'status': status,
                'display_hrs': display_hrs,
                'color': cell_color,
                'key': f'status_{agent.pk}_{day_date.isoformat()}',
                'hours_key': f'hours_{agent.pk}_{day_date.isoformat()}',
            })

        coded = sum(coded_map.get((agent.pk, d), Decimal('0')) for d in week_dates)
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
            'total_tardy': total_tardy,
            'total_incomplete': total_incomplete,
            'sched_hours': sched_total,
            'actual_hours': actual_total,
            'coded_hours': coded,
            'adjusted_total': adjusted,
            'bonus': bonus_display,
        })

    return rows


def _hhmmss_to_seconds(s):
    """Convert 'HH:MM:SS' string to integer seconds. Returns 0 on any error."""
    try:
        parts = str(s).strip().split(':')
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, IndexError, AttributeError):
        return 0


def _get_supervisor_filter(request):
    """Returns (supervisor_id_str, supervisors_qs).
    Reads GET param 'supervisor' (saving to session), or falls back to session."""
    supervisors = Agent.objects.filter(
        role_type='supervisor', status='active'
    ).select_related('user').order_by('user__last_name', 'user__first_name')

    if 'supervisor' in request.GET:
        val = request.GET.get('supervisor', '')
        request.session['supervisor_filter'] = val
        return val, supervisors

    return request.session.get('supervisor_filter', ''), supervisors


def _apply_supervisor_filter(agents_qs, supervisor_id):
    if supervisor_id:
        try:
            return agents_qs.filter(supervisor_id=int(supervisor_id))
        except (ValueError, TypeError):
            pass
    return agents_qs


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

    try:
        day_date = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid date'}, status=400)

    agent = get_object_or_404(Agent, pk=agent_id)

    if status:
        AdherenceRecord.objects.update_or_create(
            agent=agent,
            date=day_date,
            defaults={'status': status},
        )
        display = 'A' if status == 'Absent' else status
        log_action(request.user, 'Set adherence status',
                   f'{agent} — {day_date} → {display}', agent=agent)
    else:
        # Only delete if there are no system-written hours to preserve
        AdherenceRecord.objects.filter(
            agent=agent, date=day_date, actual_hours__isnull=True
        ).delete()
        log_action(request.user, 'Cleared adherence status',
                   f'{agent} — {day_date}', agent=agent)

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

    agent = get_object_or_404(Agent, pk=agent_id)
    log_action(request.user, 'Added coding',
               f'{agent} — {date_str}: {coding.start_time.strftime("%H:%M")}–{coding.end_time.strftime("%H:%M")}',
               agent=agent)

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
    coding = Coding.objects.filter(pk=coding_id).select_related('agent').first()
    if coding:
        log_action(request.user, 'Deleted coding',
                   f'{coding.agent} — {coding.date}: {coding.start_time.strftime("%H:%M")}–{coding.end_time.strftime("%H:%M")}',
                   agent=coding.agent)
        coding.delete()
    return JsonResponse({'ok': True})


# ── Page views ────────────────────────────────────────────────────────────────

@login_required
def adherence_week(request):
    week_start = _get_week_start(request)
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    week_end = week_dates[-1]

    supervisor_id, supervisors = _get_supervisor_filter(request)
    agents = Agent.objects.filter(status='active').select_related('user', 'supervisor__user').order_by(
        'user__last_name', 'user__first_name'
    )
    agents = _apply_supervisor_filter(agents, supervisor_id)
    agents = agents.filter(
        Q(shifts__date__in=week_dates) |
        Q(overtime_shifts__date__in=week_dates) |
        Q(shift_templates__isnull=False)
    ).distinct()

    if request.method == 'POST':
        shift_map, record_map, _, _ot = _build_maps(agents, week_dates)
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

    shift_map, record_map, coded_map, ot_map = _build_maps(agents, week_dates)
    rows = _build_rows(agents, week_dates, shift_map, record_map, coded_map, ot_map=ot_map)

    adj_map = {
        pa.agent_id: pa.commission_deduction
        for pa in PayrollAdjustment.objects.filter(week_start=week_start, agent__in=agents)
    }
    note_count_map = {
        (n['agent_id'], n['date']): n['count']
        for n in AdherenceNote.objects.filter(
            agent__in=agents, date__in=week_dates
        ).values('agent_id', 'date').annotate(count=DbCount('pk'))
    }
    for row in rows:
        row['commission_deduction'] = adj_map.get(row['agent'].pk, Decimal('0'))
        for cell in row['cells']:
            cell['note_count'] = note_count_map.get((row['agent'].pk, cell['date']), 0)

    return render(request, 'adherence/dashboard.html', {
        'rows': rows,
        'week_dates': week_dates,
        'week_start': week_start,
        'week_end': week_end,
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
        'status_choices': AdherenceRecord.STATUS_CHOICES,
        'supervisors': supervisors,
        'selected_supervisor': str(supervisor_id) if supervisor_id else '',
    })


@login_required
def codings_week(request):
    week_start = _get_week_start(request)
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    week_end = week_dates[-1]

    supervisor_id, supervisors = _get_supervisor_filter(request)
    agents = Agent.objects.filter(status='active').select_related('user', 'supervisor__user').order_by(
        'user__last_name', 'user__first_name'
    )
    agents = _apply_supervisor_filter(agents, supervisor_id)

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
        'supervisors': supervisors,
        'selected_supervisor': str(supervisor_id) if supervisor_id else '',
    })


@login_required
def payroll_export(request):
    week_start = _get_week_start(request)
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    week_end = week_dates[-1]

    supervisor_id, supervisors = _get_supervisor_filter(request)
    agents = Agent.objects.filter(status='active').select_related('user', 'supervisor__user').order_by(
        'user__last_name', 'user__first_name'
    )
    agents = _apply_supervisor_filter(agents, supervisor_id)

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
                    c.agent.five9_profiles.values_list('five9_username', flat=True).first() or '',
                    c.date.strftime('%Y-%m-%d'),
                    c.date.strftime('%A'),
                    c.start_time.strftime('%H:%M'),
                    c.end_time.strftime('%H:%M'),
                    c.total_hhmmss(),
                    c.notes,
                ])
            return response

        # Default: export payroll summary
        shift_map, record_map, coded_map, ot_map = _build_maps(agents, week_dates)
        adj_map = {
            pa.agent_id: pa.commission_deduction
            for pa in PayrollAdjustment.objects.filter(week_start=week_start, agent__in=agents)
        }

        response = HttpResponse(content_type='text/csv')
        filename = f'payroll_{week_start.isoformat()}_to_{week_end.isoformat()}.csv'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            'Legal Name', 'Agent Name', 'Employee ID', 'Five9 Username', 'Supervisor',
            'Scheduled Hours', 'Actual Login Hours', 'Coded Hours',
            'Adjusted Total Hours', 'Commission Deduction %', 'Adherence Bonus',
        ])

        for agent in agents:
            sched_total = Decimal('0')
            actual_total = Decimal('0')
            bonus = True
            bonus_determined = False

            for day_date in week_dates:
                shift = shift_map.get((agent.pk, day_date))
                record = record_map.get((agent.pk, day_date))
                ot_shift = ot_map.get((agent.pk, day_date))
                is_off = shift.is_off if shift else False
                is_scheduled_day = (shift and not is_off) or bool(ot_shift)

                if is_scheduled_day:
                    base_sched = _scheduled_hours(shift)
                    ot_hrs = _ot_hours(ot_shift)
                    raw_sched = base_sched + ot_hrs
                    status = record.status if record else ''
                    actual_hrs = record.actual_hours if record else None
                    if status in ('VTO', 'LOA'):
                        effective_sched = Decimal('0')
                    elif status in ('P+VTO', 'T+VTO') and actual_hrs is not None:
                        effective_sched = min(actual_hrs, raw_sched)
                    else:
                        effective_sched = raw_sched
                    sched_total += effective_sched
                    if status in BONUS_DISQUALIFYING:
                        bonus = False
                        bonus_determined = True
                    elif status in BONUS_QUALIFYING:
                        bonus_determined = True
                    elif status:
                        bonus = False
                        bonus_determined = True
                # Count actual hours for every day — off-day OT counts too
                if record and record.actual_hours:
                    actual_total += record.actual_hours

            coded = sum(coded_map.get((agent.pk, d), Decimal('0')) for d in week_dates)
            adjusted = actual_total + coded
            commission = adj_map.get(agent.pk, Decimal('0'))
            bonus_label = 'Yes' if (bonus and bonus_determined) else 'No'

            supervisor_name = str(agent.supervisor) if agent.supervisor else ''
            writer.writerow([
                agent.user.get_full_name(),
                agent.agent_name or '',
                agent.employee_id or '',
                agent.five9_profiles.values_list('five9_username', flat=True).first() or '',
                supervisor_name,
                _decimal_to_hhmmss(sched_total),
                _decimal_to_hhmmss(actual_total),
                _decimal_to_hhmmss(coded),
                _decimal_to_hhmmss(adjusted),
                f'{commission:.1f}%',
                bonus_label,
            ])

        return response

    # GET — show preview with editable deductions
    shift_map, record_map, coded_map, ot_map = _build_maps(agents, week_dates)
    rows = _build_rows(agents, week_dates, shift_map, record_map, coded_map, ot_map=ot_map)

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
        'supervisors': supervisors,
        'selected_supervisor': str(supervisor_id) if supervisor_id else '',
    })


# ── Daily Hours views ─────────────────────────────────────────────────────────

@login_required
def daily_hours_week(request):
    week_start = _get_week_start(request)
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    week_end = week_dates[-1]

    supervisor_id, supervisors = _get_supervisor_filter(request)

    codings_map = {}
    for c in Coding.objects.filter(date__in=week_dates):
        key = (c.agent_id, c.date)
        codings_map[key] = codings_map.get(key, 0) + c.total_seconds_count()

    upload_map = {u.date: u for u in DailyUpload.objects.filter(date__in=week_dates)}

    day_slots = []
    for day_date in week_dates:
        upload = upload_map.get(day_date)
        rows = []
        if upload:
            dah_qs = upload.rows.select_related(
                'agent__user', 'agent__supervisor__user'
            ).order_by('agent__user__last_name', 'agent__user__first_name', 'five9_username')

            if supervisor_id:
                try:
                    dah_qs = dah_qs.filter(
                        Q(agent__supervisor_id=int(supervisor_id)) | Q(agent__isnull=True)
                    )
                except (ValueError, TypeError):
                    pass

            for dah in dah_qs:
                coded_secs = codings_map.get((dah.agent_id, day_date), 0) if dah.agent_id else 0
                total_secs = dah.login_seconds + coded_secs
                allowance_secs = int(total_secs * 0.125)
                excess_secs = max(0, dah.not_ready_seconds - allowance_secs)
                final_secs = max(0, total_secs - excess_secs)
                rows.append({
                    'dah': dah,
                    'coded_seconds': coded_secs,
                    'total_seconds': total_secs,
                    'allowance_seconds': allowance_secs,
                    'excess_seconds': excess_secs,
                    'final_seconds': final_secs,
                })
        day_slots.append({'date': day_date, 'upload': upload, 'rows': rows})

    return render(request, 'adherence/daily_hours.html', {
        'day_slots': day_slots,
        'week_start': week_start,
        'week_end': week_end,
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
        'supervisors': supervisors,
        'selected_supervisor': str(supervisor_id) if supervisor_id else '',
    })


@login_required
@require_POST
def upload_daily_file(request):
    """Accept a Five9 CSV upload for one day. Replaces any existing upload for that date."""
    date_str = request.POST.get('date')
    csv_file = request.FILES.get('file')

    if not date_str or not csv_file:
        return JsonResponse({'ok': False, 'error': 'Missing date or file.'}, status=400)

    try:
        upload_date = date.fromisoformat(date_str)
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid date.'}, status=400)

    try:
        content = csv_file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'Could not read file: {e}'}, status=400)

    # Build lookup from Five9 username → Agent using Five9Profile table
    # so agents with multiple Five9 accounts all resolve to the same person
    agent_map = {
        p.five9_username.strip().lower(): p.agent
        for p in Five9Profile.objects.filter(
            five9_username__gt='', agent__status='active'
        ).select_related('agent__user', 'agent__supervisor__user')
    }

    DailyUpload.objects.filter(date=upload_date).delete()

    upload = DailyUpload.objects.create(
        date=upload_date,
        filename=csv_file.name,
        row_count=len(rows),
    )

    unmatched = 0
    dah_objects = []

    for row in rows:
        username = (row.get('AGENT') or row.get('Agent') or '').strip().lower()
        agent_group = (row.get('AGENT GROUP') or row.get('Agent Group') or '').strip()
        login_str = (row.get('LOGIN TIME') or row.get('Login Time') or '').strip()
        not_ready_str = (row.get('NOT READY TIME') or row.get('Not Ready Time') or '').strip()

        if not username:
            continue

        agent = agent_map.get(username)
        if not agent:
            unmatched += 1

        dah = DailyAgentHours(
            upload=upload,
            agent=agent,
            five9_username=username,
            agent_group=agent_group,
            login_seconds=_hhmmss_to_seconds(login_str),
            not_ready_seconds=_hhmmss_to_seconds(not_ready_str),
        )
        dah_objects.append(dah)

    DailyAgentHours.objects.bulk_create(dah_objects, ignore_conflicts=True)
    upload.unmatched_count = unmatched
    upload.save()

    for dah in dah_objects:
        if not dah.agent_id:
            continue
        coded_secs = sum(
            c.total_seconds_count()
            for c in Coding.objects.filter(agent_id=dah.agent_id, date=upload_date)
        )
        total_secs = dah.login_seconds + coded_secs
        allowance_secs = int(total_secs * 0.125)
        excess_secs = max(0, dah.not_ready_seconds - allowance_secs)
        final_secs = max(0, total_secs - excess_secs)
        final_hours = Decimal(str(round(final_secs / 3600, 6)))

        AdherenceRecord.objects.update_or_create(
            agent_id=dah.agent_id,
            date=upload_date,
            defaults={'actual_hours': final_hours},
        )

    return JsonResponse({
        'ok': True,
        'row_count': len(dah_objects),
        'unmatched_count': unmatched,
        'filename': csv_file.name,
    })


@login_required
@require_POST
def delete_daily_upload_ajax(request):
    data = json.loads(request.body)
    date_str = data.get('date')
    try:
        DailyUpload.objects.filter(date=date.fromisoformat(date_str)).delete()
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid date'}, status=400)
    return JsonResponse({'ok': True})


@login_required
@require_POST
def edit_adherence_note(request):
    note_id = request.POST.get('note_id')
    body = (request.POST.get('body') or '').strip()
    if not body:
        return JsonResponse({'error': 'Empty note'}, status=400)
    note = get_object_or_404(AdherenceNote, pk=note_id)
    note.body = body
    note.save()
    return JsonResponse({'ok': True, 'body': note.body})


@login_required
@require_POST
def delete_adherence_note(request):
    note_id = request.POST.get('note_id')
    note = get_object_or_404(AdherenceNote, pk=note_id)
    agent_id, date_val = note.agent_id, note.date
    note.delete()
    new_count = AdherenceNote.objects.filter(agent_id=agent_id, date=date_val).count()
    return JsonResponse({'ok': True, 'new_count': new_count})


@login_required
def adherence_notes(request):
    """GET: list notes for agent+date. POST: add a new note."""
    agent_id = request.GET.get('agent') or request.POST.get('agent')
    date_str = request.GET.get('date') or request.POST.get('date')

    try:
        date_val = date.fromisoformat(date_str) if date_str else None
        if not date_val:
            raise ValueError
    except (TypeError, ValueError):
        return JsonResponse({'error': 'Invalid date'}, status=400)

    if not agent_id:
        return JsonResponse({'error': 'Missing agent'}, status=400)

    if request.method == 'POST':
        body = (request.POST.get('body') or '').strip()
        if not body:
            return JsonResponse({'error': 'Empty note'}, status=400)
        note = AdherenceNote.objects.create(
            agent_id=agent_id,
            date=date_val,
            author=request.user,
            body=body,
        )
        new_count = AdherenceNote.objects.filter(agent_id=agent_id, date=date_val).count()
        return JsonResponse({
            'ok': True,
            'id': note.pk,
            'author': request.user.get_full_name() or request.user.username,
            'body': note.body,
            'created_at': note.created_at.strftime('%b %d %I:%M %p'),
            'new_count': new_count,
        })

    notes = AdherenceNote.objects.filter(
        agent_id=agent_id, date=date_val
    ).select_related('author')
    return JsonResponse({
        'notes': [
            {
                'id': n.pk,
                'author': (n.author.get_full_name() or n.author.username) if n.author else 'Unknown',
                'body': n.body,
                'created_at': n.created_at.strftime('%b %d %I:%M %p'),
            }
            for n in notes
        ]
    })


@login_required
def adherence_poll(request):
    """Return the latest update timestamp for adherence records in the given week."""
    from django.db.models import Max
    week_start_str = request.GET.get('week_start', '')
    try:
        ws = date.fromisoformat(week_start_str)
        ws -= timedelta(days=ws.weekday())
    except (ValueError, TypeError):
        today = date.today()
        ws = today - timedelta(days=today.weekday())

    week_dates = [ws + timedelta(days=i) for i in range(7)]
    result = AdherenceRecord.objects.filter(date__in=week_dates).aggregate(
        latest=Max('updated_at')
    )
    coding_result = Coding.objects.filter(date__in=week_dates).aggregate(
        latest=Max('created_at')
    )

    timestamps = [t for t in [result['latest'], coding_result['latest']] if t]
    latest = max(timestamps).isoformat() if timestamps else None
    return JsonResponse({'latest': latest})
