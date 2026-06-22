import csv
import io
import math
from datetime import date, timedelta
from django.utils import timezone

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_POST

from .calculator import (
    agents_required, service_level, occupancy,
    parse_aht, calculate_staffing, format_aht,
)
from .models import ErlangReport, ErlangActualStaff, ErlangCallRow, ErlangWeekParams
from scheduling.models import Shift, ShiftTemplate, Five9Profile, OvertimeShift, Agent

DAYS_ORDER = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def _get_week_start(request):
    """Return the Monday of the selected week from GET param, session, or today."""
    raw = request.GET.get('week_start') or request.session.get('erlang_week_start')
    try:
        ws = date.fromisoformat(raw)
        ws -= timedelta(days=ws.weekday())  # force to Monday
    except (TypeError, ValueError):
        today = date.today()
        ws = today - timedelta(days=today.weekday())
    return ws


def _build_scheduled_map(week_start):
    """Count agents covering each (day_name, hour); also return which agents and their shift times."""
    from scheduling.models import ScheduledRoleChange
    week_dates = [week_start + timedelta(days=i) for i in range(7)]

    CALL_ROLES = {'regular_agent', 'night_shift'}

    # Base call agents (current role)
    base_call_ids = set(Five9Profile.objects.filter(
        role_type__in=CALL_ROLES
    ).values_list('agent_id', flat=True).distinct())

    # Pending role changes that take effect within this week — adjust per date
    pending = list(ScheduledRoleChange.objects.filter(
        effective_date__range=(week_dates[0], week_dates[-1]),
        applied_at__isnull=True,
        cancelled_at__isnull=True,
    ).values('agent_id', 'new_role_type', 'effective_date',
             'new_shift_days', 'new_shift_start_time', 'new_shift_end_time'))

    if pending:
        affected_ids = {p['agent_id'] for p in pending}
        cur_roles = dict(Five9Profile.objects.filter(
            agent_id__in=affected_ids
        ).values_list('agent_id', 'role_type'))
    else:
        cur_roles = {}

    # Mid-week role changes made via direct edit (recorded in RoleHistory)
    from scheduling.models import RoleHistory
    rh_new = list(RoleHistory.objects.filter(
        effective_from__range=(week_dates[0], week_dates[-1]),
    ).values('agent_id', 'role_type', 'effective_from').order_by('agent_id', 'effective_from'))
    rh_transitions = {}
    if rh_new:
        changed_ids = {e['agent_id'] for e in rh_new}
        closed = list(RoleHistory.objects.filter(
            agent_id__in=changed_ids,
            effective_to__range=(week_dates[0], week_dates[-1]),
        ).values('agent_id', 'role_type').order_by('agent_id', '-effective_from'))
        old_role_map = {}
        for e in closed:
            if e['agent_id'] not in old_role_map:
                old_role_map[e['agent_id']] = e['role_type']
        for e in rh_new:
            aid = e['agent_id']
            if aid in old_role_map:
                rh_transitions[aid] = {
                    'old': old_role_map[aid],
                    'new': e['role_type'],
                    'from': e['effective_from'],
                }

    # Build per-date call-agent sets
    call_ids_by_date = {}
    for d in week_dates:
        if not pending and not rh_transitions:
            call_ids_by_date[d] = base_call_ids
        else:
            ids = set(base_call_ids)
            for p in pending:
                if p['effective_date'] <= d:
                    old, new = cur_roles.get(p['agent_id'], ''), p['new_role_type']
                    if new in CALL_ROLES and old not in CALL_ROLES:
                        ids.add(p['agent_id'])
                    elif new not in CALL_ROLES and old in CALL_ROLES:
                        ids.discard(p['agent_id'])
            # RoleHistory adjustments: for days before a mid-week change, use the old role
            for aid, t in rh_transitions.items():
                if d < t['from']:
                    if t['old'] in CALL_ROLES and t['new'] not in CALL_ROLES:
                        ids.add(aid)      # was call agent before the change
                    elif t['old'] not in CALL_ROLES and t['new'] in CALL_ROLES:
                        ids.discard(aid)  # was not a call agent before the change
            call_ids_by_date[d] = ids

    all_call_ids = set().union(*call_ids_by_date.values())

    # Pre-fetch agent display names
    agent_names = {
        a.pk: str(a)
        for a in Agent.objects.select_related('user').filter(status='active')
    }

    scheduled = {}    # {(day_name, hour): int}
    agents_map = {}   # {(day_name, hour): [{'name': str, 'time': str, 'ot': bool}]}
    seen = set()      # (day_name, hour, agent_id) — prevent double-counting

    def _add(day_name, h, agent_id, entry):
        if (day_name, h, agent_id) in seen:
            return
        seen.add((day_name, h, agent_id))
        key = (day_name, h)
        scheduled[key] = scheduled.get(key, 0) + 1
        agents_map.setdefault(key, []).append(entry)

    def _add_hours(day_name, start_hour, end_hour, agent_id, entry, next_day_name=None):
        if end_hour <= start_hour:  # overnight — split at midnight
            for h in range(start_hour, 24):
                _add(day_name, h, agent_id, entry)
            nd = next_day_name or day_name
            for h in range(0, end_hour):
                _add(nd, h, agent_id, entry)
        else:
            for h in range(start_hour, end_hour):
                _add(day_name, h, agent_id, entry)

    # Specific shift overrides — date-aware role check
    shifts = Shift.objects.filter(
        date__in=week_dates, is_off=False, agent_id__in=all_call_ids,
    ).values('agent_id', 'date', 'start_time', 'end_time')

    agents_with_shift_override = set()
    for s in shifts:
        if s['agent_id'] not in call_ids_by_date[s['date']]:
            continue
        agents_with_shift_override.add((s['agent_id'], s['date']))
        name = agent_names.get(s['agent_id'], f"Agent {s['agent_id']}")
        label = f"{s['start_time'].strftime('%H:%M')}–{s['end_time'].strftime('%H:%M')}"
        next_day = (s['date'] + timedelta(days=1)).strftime('%A')
        _add_hours(s['date'].strftime('%A'), s['start_time'].hour, s['end_time'].hour,
                   s['agent_id'], {'name': name, 'time': label, 'ot': False}, next_day)

    # Recurring templates — date-aware role check + effective_from/effective_until
    templates = ShiftTemplate.objects.filter(
        agent_id__in=all_call_ids, is_off=False,
    ).values('agent_id', 'day_of_week', 'start_time', 'end_time', 'effective_from', 'effective_until')

    for t in templates:
        for day_date in week_dates:
            if day_date.weekday() != t['day_of_week']:
                continue
            if t['agent_id'] not in call_ids_by_date[day_date]:
                continue
            if t['effective_from'] and t['effective_from'] > day_date:
                continue
            if t['effective_until'] and t['effective_until'] <= day_date:
                continue
            if (t['agent_id'], day_date) in agents_with_shift_override:
                continue
            name = agent_names.get(t['agent_id'], f"Agent {t['agent_id']}")
            label = f"{t['start_time'].strftime('%H:%M')}–{t['end_time'].strftime('%H:%M')}"
            next_day = (day_date + timedelta(days=1)).strftime('%A')
            _add_hours(day_date.strftime('%A'), t['start_time'].hour, t['end_time'].hour,
                       t['agent_id'], {'name': name, 'time': label, 'ot': False}, next_day)

    # Pending role changes with a new schedule — count them for planning before effective date applies
    # This lets coordinators see next week's staffing with graduating agents already counted.
    for p in pending:
        if not (p['new_shift_days'] and p['new_shift_start_time'] and p['new_shift_end_time']):
            continue
        start_t = p['new_shift_start_time']
        end_t = p['new_shift_end_time']
        for day_date in week_dates:
            if day_date < p['effective_date']:
                continue
            if day_date.weekday() not in p['new_shift_days']:
                continue
            if p['agent_id'] not in call_ids_by_date[day_date]:
                continue
            if (p['agent_id'], day_date) in agents_with_shift_override:
                continue
            name = agent_names.get(p['agent_id'], f"Agent {p['agent_id']}")
            label = f"{start_t.strftime('%H:%M')}–{end_t.strftime('%H:%M')}"
            next_day = (day_date + timedelta(days=1)).strftime('%A')
            _add_hours(day_date.strftime('%A'), start_t.hour, end_t.hour,
                       p['agent_id'], {'name': name, 'time': label, 'ot': False}, next_day)

    # OT shifts — all agents regardless of role; cancelled excluded
    ot_shifts = OvertimeShift.objects.filter(date__in=week_dates).exclude(status='cancelled').values(
        'agent_id', 'date', 'start_time', 'end_time'
    )
    for s in ot_shifts:
        name = agent_names.get(s['agent_id'], f"Agent {s['agent_id']}")
        label = f"{s['start_time'].strftime('%H:%M')}–{s['end_time'].strftime('%H:%M')}"
        next_day = (s['date'] + timedelta(days=1)).strftime('%A')
        _add_hours(s['date'].strftime('%A'), s['start_time'].hour, s['end_time'].hour,
                   s['agent_id'], {'name': name, 'time': label, 'ot': True}, next_day)

    return scheduled, agents_map


def _build_actual_map(week_start):
    """Load saved actual agent counts for the given week."""
    return {
        (a.day, a.hour): a.actual_agents
        for a in ErlangActualStaff.objects.filter(week_start=week_start)
    }


def _parse_five9_csv(file):
    """Parse Five9 ACD Queue Quality of Service Details - Hourly CSV."""
    text = file.read().decode('utf-8-sig', errors='replace')
    reader = csv.DictReader(io.StringIO(text))

    rows = []
    for raw in reader:
        row = {k.strip(): (v.strip() if v else '') for k, v in raw.items() if k}

        day = row.get('DAY OF WEEK', '').strip()
        hour_str = row.get('HOUR OF DAY', '').strip()
        calls_str = row.get('CALLS', '0').strip().replace(',', '')

        if not day or not hour_str:
            continue
        if day not in DAYS_ORDER:
            continue

        try:
            hour = int(hour_str)
            calls = float(calls_str) if calls_str else 0
        except (ValueError, TypeError):
            continue

        if calls <= 0:
            continue

        rows.append({
            'day': day,
            'hour': hour,
            'total_calls': calls,
        })

    return rows


def _build_days(calculated_rows, params, scheduled_map, actual_map, weeks_by_day=None):
    """Group calculated rows by day and compute per-day summary stats."""
    by_day = {d: [] for d in DAYS_ORDER}
    for row in calculated_rows:
        if row['day'] in by_day:
            by_day[row['day']].append(row)

    days = []
    for day_name in DAYS_ORDER:
        rows = sorted(by_day[day_name], key=lambda r: r['hour'])
        if not rows:
            days.append({'name': day_name, 'rows': [], 'has_data': False,
                         'weeks': (weeks_by_day or {}).get(day_name, 3)})
            continue

        for row in rows:
            row['scheduled_staff'] = scheduled_map.get((day_name, row['hour']), 0)
            row['actual_agents'] = actual_map.get((day_name, row['hour']), None)

        total_shrink = sum(r['agents_shrinkage'] for r in rows)
        peak = max(rows, key=lambda r: r['agents_shrinkage'])

        days.append({
            'name': day_name,
            'rows': rows,
            'has_data': True,
            'total_hours': len(rows),
            'avg_agents': round(total_shrink / len(rows), 1),
            'peak_label': peak['hour_label'],
            'peak_agents': peak['agents_shrinkage'],
            'weeks': (weeks_by_day or {}).get(day_name, 3),
        })

    return days


@login_required
def erlang_calculator(request):
    error = None

    week_start = _get_week_start(request)
    request.session['erlang_week_start'] = week_start.isoformat()

    week_key = week_start.isoformat()

    if request.method == 'POST':
        csv_uploaded_now = False
        if 'csv_file' in request.FILES and request.FILES['csv_file'].name:
            try:
                rows = _parse_five9_csv(request.FILES['csv_file'])
                if not rows:
                    error = "No valid data found. Check that the file is the Five9 ACD Queue Quality of Service Details - Hourly report."
                else:
                    ErlangCallRow.objects.filter(week_start=week_start).delete()
                    ErlangCallRow.objects.bulk_create([
                        ErlangCallRow(
                            week_start=week_start,
                            day=r['day'],
                            hour=r['hour'],
                            total_calls=r['total_calls'],
                            avg_calls=r['total_calls'],  # stored raw; divided at view time
                        )
                        for r in rows
                    ])
                    csv_uploaded_now = True
            except Exception as e:
                error = f"Error reading file: {e}"

        try:
            weeks_default = max(1, int(request.POST.get('weeks', 3) or 3))
        except (ValueError, TypeError):
            weeks_default = 3
        weeks_by_day = {}
        for day in DAYS_ORDER:
            val = request.POST.get(f'weeks_{day.lower()}', '').strip()
            try:
                weeks_by_day[day] = max(1, int(val)) if val else weeks_default
            except (ValueError, TypeError):
                weeks_by_day[day] = weeks_default

        param_defaults = {
            'target_sl': float(request.POST.get('target_sl', 80)),
            'target_seconds': int(request.POST.get('target_seconds', 20)),
            'shrinkage': float(request.POST.get('shrinkage', 0)),
            'aht_seconds': int(request.POST.get('aht_seconds', 420)),
            'weeks': weeks_default,
            'weeks_by_day': weeks_by_day,
            'calculated_by': request.user,
        }
        if csv_uploaded_now:
            param_defaults['csv_uploaded_at'] = timezone.now()
            param_defaults['csv_uploaded_by'] = request.user

        ErlangWeekParams.objects.update_or_create(
            week_start=week_start,
            defaults=param_defaults,
        )

        if not error:
            return redirect(f"{request.path}?week_start={week_key}")

    _wp = ErlangWeekParams.objects.filter(week_start=week_start).first()
    _weeks_by_day = (_wp.weeks_by_day if _wp and _wp.weeks_by_day else None) or {d: 3 for d in DAYS_ORDER}
    _weeks_default = _wp.weeks if _wp else 3

    raw_rows = [
        {
            'day': r.day,
            'hour': r.hour,
            'total_calls': r.total_calls,
            'avg_calls': round(r.total_calls / _weeks_by_day.get(r.day, _weeks_default), 1),
        }
        for r in ErlangCallRow.objects.filter(week_start=week_start)
    ]
    params = {
        'target_sl': _wp.target_sl if _wp else 80,
        'target_seconds': _wp.target_seconds if _wp else 20,
        'shrinkage': _wp.shrinkage if _wp else 0,
        'aht_seconds': _wp.aht_seconds if _wp else 420,
        'weeks': _weeks_default,
        'weeks_by_day': _weeks_by_day,
    }

    days = []
    agents_map_json = '{}'
    if raw_rows:
        calculated = calculate_staffing(
            raw_rows,
            params['target_sl'],
            params['target_seconds'],
            params['shrinkage'],
            params['aht_seconds'],
        )
        scheduled_map, agents_map = _build_scheduled_map(week_start)
        days = _build_days(
            calculated, params,
            scheduled_map,
            _build_actual_map(week_start),
            weeks_by_day=_weeks_by_day,
        )
        import json
        agents_map_json = json.dumps({
            f"{day}:{hour}": sorted(entries, key=lambda e: e['name'])
            for (day, hour), entries in agents_map.items()
        })

    prev_week = week_start - timedelta(days=7)
    next_week = week_start + timedelta(days=7)
    today = date.today()
    current_week = today - timedelta(days=today.weekday())

    # Build a human-readable weeks summary for the audit line.
    # Shows "3w (all days)" when uniform, or per-day breakdown when they differ.
    _abbrev = {'Monday':'Mon','Tuesday':'Tue','Wednesday':'Wed',
               'Thursday':'Thu','Friday':'Fri','Saturday':'Sat','Sunday':'Sun'}
    weeks_audit = None
    if _wp and _wp.weeks_by_day:
        wbd = _wp.weeks_by_day
        if any(wbd.get(d, _wp.weeks) != _wp.weeks for d in DAYS_ORDER):
            weeks_audit = ' · '.join(
                f"{_abbrev[d]} {wbd.get(d, _wp.weeks)}w" for d in DAYS_ORDER
            )

    return render(request, 'erlang/calculator.html', {
        'days': days,
        'params': params,
        'week_params': _wp,
        'weeks_audit': weeks_audit,
        'has_data': bool(raw_rows),
        'error': error,
        'days_order': DAYS_ORDER,
        'week_start': week_start,
        'week_end': week_start + timedelta(days=6),
        'prev_week': prev_week,
        'next_week': next_week,
        'is_current_week': week_start == current_week,
        'current_week': current_week,
        'agents_map_json': agents_map_json,
    })


@login_required
@require_POST
def erlang_save_actual(request):
    """AJAX endpoint: save or clear an actual-agents value for a specific week/day/hour."""
    week_start_str = request.POST.get('week_start', '')
    day = request.POST.get('day', '')
    hour_str = request.POST.get('hour', '')
    actual_str = request.POST.get('actual_agents', '').strip()

    try:
        ws = date.fromisoformat(week_start_str)
        hour = int(hour_str)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid parameters'}, status=400)

    if day not in DAYS_ORDER:
        return JsonResponse({'error': 'Invalid day'}, status=400)

    if actual_str == '':
        ErlangActualStaff.objects.filter(week_start=ws, day=day, hour=hour).delete()
    else:
        try:
            actual_agents = int(actual_str)
        except ValueError:
            return JsonResponse({'error': 'Invalid value'}, status=400)
        ErlangActualStaff.objects.update_or_create(
            week_start=ws, day=day, hour=hour,
            defaults={'actual_agents': actual_agents},
        )

    return JsonResponse({'ok': True})


@login_required
def erlang_download(request):
    if request.method != 'POST':
        return redirect('erlang_calculator')

    week_start_str = request.session.get('erlang_week_start', '')
    try:
        week_start = date.fromisoformat(week_start_str)
    except (ValueError, TypeError):
        today = date.today()
        week_start = today - timedelta(days=today.weekday())

    _wp = ErlangWeekParams.objects.filter(week_start=week_start).first()
    _weeks_by_day = (_wp.weeks_by_day if _wp and _wp.weeks_by_day else None) or {d: 3 for d in DAYS_ORDER}
    _weeks_default = _wp.weeks if _wp else 3

    raw_rows = [
        {
            'day': r.day,
            'hour': r.hour,
            'total_calls': r.total_calls,
            'avg_calls': round(r.total_calls / _weeks_by_day.get(r.day, _weeks_default), 1),
        }
        for r in ErlangCallRow.objects.filter(week_start=week_start)
    ]
    params = {
        'target_sl': _wp.target_sl if _wp else 80,
        'target_seconds': _wp.target_seconds if _wp else 20,
        'shrinkage': _wp.shrinkage if _wp else 0,
        'aht_seconds': _wp.aht_seconds if _wp else 420,
    }

    if not raw_rows:
        return redirect('erlang_calculator')

    calculated = calculate_staffing(
        raw_rows,
        params['target_sl'],
        params['target_seconds'],
        params['shrinkage'],
        params['aht_seconds'],
    )

    scheduled_map, _ = _build_scheduled_map(week_start)
    actual_map = _build_actual_map(week_start)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="staffing_plan.csv"'
    writer = csv.writer(response)
    aht_display = format_aht(params['aht_seconds'])
    writer.writerow([
        'Day', 'Hour', 'Avg Calls', f'Avg Handle Time ({aht_display})',
        'Agents Required', f'Agents w/ {params["shrinkage"]}% Shrinkage',
        'Scheduled Staff', 'Actual Agents', 'Variance (Sched vs Req)', 'Service Level %',
    ])

    by_day = {d: [] for d in DAYS_ORDER}
    for row in calculated:
        if row['day'] in by_day:
            by_day[row['day']].append(row)

    for day_name in DAYS_ORDER:
        for row in sorted(by_day[day_name], key=lambda r: r['hour']):
            scheduled = scheduled_map.get((day_name, row['hour']), 0)
            actual = actual_map.get((day_name, row['hour']), '')
            variance = scheduled - row['agents_shrinkage']
            writer.writerow([
                row['day'],
                row['hour_label'],
                row['avg_calls'],
                aht_display,
                row['agents_required'],
                row['agents_shrinkage'],
                scheduled,
                actual,
                variance,
                f"{row['service_level_achieved']}%",
            ])

    return response


@login_required
@require_POST
def erlang_save_report(request):
    week_start_str = request.POST.get('week_start', '')
    name = request.POST.get('report_name', '').strip()
    try:
        week_start = date.fromisoformat(week_start_str)
    except (ValueError, TypeError):
        from django.contrib import messages
        messages.error(request, 'Invalid week.')
        return redirect('erlang_calculator')

    _wp = ErlangWeekParams.objects.filter(week_start=week_start).first()
    if not _wp:
        from django.contrib import messages
        messages.error(request, 'No calculation data found for this week.')
        return redirect('erlang_calculator')

    _weeks_by_day = (_wp.weeks_by_day or {d: 3 for d in DAYS_ORDER})
    raw_rows = [
        {
            'day': r.day,
            'hour': r.hour,
            'total_calls': r.total_calls,
            'avg_calls': round(r.total_calls / _weeks_by_day.get(r.day, _wp.weeks), 1),
        }
        for r in ErlangCallRow.objects.filter(week_start=week_start)
    ]
    if not raw_rows:
        from django.contrib import messages
        messages.error(request, 'No call data found for this week.')
        return redirect('erlang_calculator')

    calculated = calculate_staffing(
        raw_rows, _wp.target_sl, _wp.target_seconds, _wp.shrinkage, _wp.aht_seconds,
    )

    peak = max(calculated, key=lambda r: r['agents_shrinkage'])
    avg_sl = round(sum(r['service_level_achieved'] for r in calculated) / len(calculated), 1)
    avg_occ = round(sum(r['occupancy'] for r in calculated if r['occupancy']) / max(1, sum(1 for r in calculated if r['occupancy'])), 1)

    if not name:
        week_end = week_start + timedelta(days=6)
        name = f"Week of {week_start.strftime('%b %-d')}–{week_end.strftime('%b %-d, %Y')}"

    ErlangReport.objects.create(
        name=name,
        calls_per_hour=peak['avg_calls'],
        avg_handle_time=_wp.aht_seconds,
        target_service_level=_wp.target_sl,
        target_answer_time=_wp.target_seconds,
        shrinkage=_wp.shrinkage,
        agents_required=peak['agents_required'],
        agents_scheduled=peak['agents_shrinkage'],
        service_level_achieved=avg_sl,
        occupancy=avg_occ,
    )

    from django.contrib import messages
    from django.urls import reverse
    messages.success(request, f'Report "{name}" saved.')
    return redirect(f"{reverse('erlang_calculator')}?week_start={week_start_str}")


@login_required
@require_POST
def erlang_delete_report(request, pk):
    from django.shortcuts import get_object_or_404
    report = get_object_or_404(ErlangReport, pk=pk)
    report.delete()
    from django.contrib import messages
    messages.success(request, 'Report deleted.')
    return redirect('erlang_reports')


@login_required
def erlang_reports(request):
    reports = ErlangReport.objects.all()
    return render(request, 'erlang/reports.html', {'reports': reports})
