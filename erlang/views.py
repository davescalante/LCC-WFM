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
from .models import ErlangReport, ErlangActualStaff
from scheduling.models import Shift

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
    """Count regular/nightshift agents covering each (day_name, hour) for the given week."""
    week_dates = [week_start + timedelta(days=i) for i in range(7)]

    shifts = Shift.objects.filter(
        date__in=week_dates,
        is_off=False,
        agent__role_type__in=('regular_agent', 'night_shift'),
    ).values('date', 'start_time', 'end_time')

    scheduled = {}
    for s in shifts:
        day_name = s['date'].strftime('%A')
        sh = s['start_time'].hour
        eh = s['end_time'].hour
        hours = list(range(sh, 24)) + list(range(0, eh)) if eh <= sh else list(range(sh, eh))
        for h in hours:
            key = (day_name, h)
            scheduled[key] = scheduled.get(key, 0) + 1

    return scheduled


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
            'avg_calls': round(calls / 3, 1),
        })

    return rows


def _build_days(calculated_rows, params, scheduled_map, actual_map):
    """Group calculated rows by day and compute per-day summary stats."""
    by_day = {d: [] for d in DAYS_ORDER}
    for row in calculated_rows:
        if row['day'] in by_day:
            by_day[row['day']].append(row)

    days = []
    for day_name in DAYS_ORDER:
        rows = sorted(by_day[day_name], key=lambda r: r['hour'])
        if not rows:
            days.append({'name': day_name, 'rows': [], 'has_data': False})
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
                    rows_by_week = request.session.get('erlang_rows_by_week', {})
                    rows_by_week[week_key] = rows
                    request.session['erlang_rows_by_week'] = rows_by_week
            except Exception as e:
                error = f"Error reading file: {e}"

        request.session['erlang_params'] = {
            'target_sl': float(request.POST.get('target_sl', 80)),
            'target_seconds': int(request.POST.get('target_seconds', 20)),
            'shrinkage': float(request.POST.get('shrinkage', 0)),
            'aht_seconds': int(request.POST.get('aht_seconds', 420)),
        }

        if not error:
            return redirect(f"{request.path}?week_start={week_key}")

    raw_rows = request.session.get('erlang_rows_by_week', {}).get(week_key, [])
    _p = request.session.get('erlang_params', {})
    params = {
        'target_sl': _p.get('target_sl', 80),
        'target_seconds': _p.get('target_seconds', 20),
        'shrinkage': _p.get('shrinkage', 0),
        'aht_seconds': _p.get('aht_seconds', 420),
    }

    days = []
    if raw_rows:
        calculated = calculate_staffing(
            raw_rows,
            params['target_sl'],
            params['target_seconds'],
            params['shrinkage'],
            params['aht_seconds'],
        )
        days = _build_days(
            calculated, params,
            _build_scheduled_map(week_start),
            _build_actual_map(week_start),
        )

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

    raw_rows = request.session.get('erlang_rows_by_week', {}).get(week_start.isoformat(), [])
    _p = request.session.get('erlang_params', {})
    params = {
        'target_sl': _p.get('target_sl', 80),
        'target_seconds': _p.get('target_seconds', 20),
        'shrinkage': _p.get('shrinkage', 0),
        'aht_seconds': _p.get('aht_seconds', 420),
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

    scheduled_map = _build_scheduled_map(week_start)
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
            scheduled = scheduled_map.get((day_name, row['hour']), '')
            actual = actual_map.get((day_name, row['hour']), '')
            variance = (scheduled - row['agents_shrinkage']) if isinstance(scheduled, int) else ''
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
