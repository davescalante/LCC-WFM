import csv
import io
import math
from datetime import date, timedelta

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
    week_dates = [week_start + timedelta(days=i) for i in range(7)]

    call_agent_ids = set(Five9Profile.objects.filter(
        role_type__in=('regular_agent', 'night_shift')
    ).values_list('agent_id', flat=True).distinct())

    # Pre-fetch agent display names for lookups below
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

    # Specific shift overrides for this week
    shifts = Shift.objects.filter(
        date__in=week_dates, is_off=False, agent_id__in=call_agent_ids,
    ).values('agent_id', 'date', 'start_time', 'end_time')

    agents_with_shift_override = set()
    for s in shifts:
        agents_with_shift_override.add((s['agent_id'], s['date']))
        name = agent_names.get(s['agent_id'], f"Agent {s['agent_id']}")
        label = f"{s['start_time'].strftime('%H:%M')}–{s['end_time'].strftime('%H:%M')}"
        next_day = (s['date'] + timedelta(days=1)).strftime('%A')
        _add_hours(s['date'].strftime('%A'), s['start_time'].hour, s['end_time'].hour,
                   s['agent_id'], {'name': name, 'time': label, 'ot': False}, next_day)

    # Recurring templates — only for days not covered by a specific Shift
    templates = ShiftTemplate.objects.filter(
        agent_id__in=call_agent_ids, is_off=False,
    ).values('agent_id', 'day_of_week', 'start_time', 'end_time')

    for t in templates:
        for day_date in week_dates:
            if day_date.weekday() == t['day_of_week']:
                if (t['agent_id'], day_date) not in agents_with_shift_override:
                    name = agent_names.get(t['agent_id'], f"Agent {t['agent_id']}")
                    label = f"{t['start_time'].strftime('%H:%M')}–{t['end_time'].strftime('%H:%M')}"
                    next_day = (day_date + timedelta(days=1)).strftime('%A')
                    _add_hours(day_date.strftime('%A'), t['start_time'].hour, t['end_time'].hour,
                               t['agent_id'], {'name': name, 'time': label, 'ot': False}, next_day)

    # OT shifts — all agents regardless of role; cancelled shifts are not counted as coverage
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

        ErlangWeekParams.objects.update_or_create(
            week_start=week_start,
            defaults={
                'target_sl': float(request.POST.get('target_sl', 80)),
                'target_seconds': int(request.POST.get('target_seconds', 20)),
                'shrinkage': float(request.POST.get('shrinkage', 0)),
                'aht_seconds': int(request.POST.get('aht_seconds', 420)),
                'weeks': weeks_default,
                'weeks_by_day': weeks_by_day,
            },
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

    return render(request, 'erlang/calculator.html', {
        'days': days,
        'params': params,
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
def erlang_reports(request):
    reports = ErlangReport.objects.all()
    return render(request, 'erlang/reports.html', {'reports': reports})
