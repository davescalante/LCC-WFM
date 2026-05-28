import csv
import io
import math
from datetime import date, timedelta

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse

from .calculator import (
    agents_required, service_level, occupancy,
    parse_aht, calculate_staffing,
)
from .models import ErlangReport
from scheduling.models import Shift

DAYS_ORDER = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def _build_scheduled_map():
    """Count regular/nightshift agents covering each (day_name, hour) for the current week."""
    today = date.today()
    week_mon = today - timedelta(days=today.weekday())
    week_dates = [week_mon + timedelta(days=i) for i in range(7)]

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


def _build_days(calculated_rows, params, scheduled_map):
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

    if request.method == 'POST':
        # Parse new file if uploaded, otherwise keep existing session data
        if 'csv_file' in request.FILES and request.FILES['csv_file'].name:
            try:
                rows = _parse_five9_csv(request.FILES['csv_file'])
                if not rows:
                    error = "No valid data found. Check that the file is the Five9 ACD Queue Quality of Service Details - Hourly report."
                else:
                    request.session['erlang_rows'] = rows
            except Exception as e:
                error = f"Error reading file: {e}"

        request.session['erlang_params'] = {
            'target_sl': float(request.POST.get('target_sl', 80)),
            'target_seconds': int(request.POST.get('target_seconds', 20)),
            'shrinkage': float(request.POST.get('shrinkage', 0)),
            'aht_seconds': int(request.POST.get('aht_seconds', 420)),
        }

        if not error:
            return redirect('erlang_calculator')

    raw_rows = request.session.get('erlang_rows', [])
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
        days = _build_days(calculated, params, _build_scheduled_map())

    return render(request, 'erlang/calculator.html', {
        'days': days,
        'params': params,
        'has_data': bool(raw_rows),
        'error': error,
        'days_order': DAYS_ORDER,
    })


@login_required
def erlang_download(request):
    if request.method != 'POST':
        return redirect('erlang_calculator')

    raw_rows = request.session.get('erlang_rows', [])
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

    # Collect current staffing values from POST
    current_staffing = {}
    for key, val in request.POST.items():
        if key.startswith('staffing_'):
            try:
                current_staffing[key] = int(val)
            except (ValueError, TypeError):
                pass

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="staffing_plan.csv"'
    writer = csv.writer(response)
    from .calculator import format_aht
    aht_display = format_aht(params['aht_seconds'])
    scheduled_map = _build_scheduled_map()
    writer.writerow([
        'Day', 'Hour', 'Avg Calls', f'Avg Handle Time ({aht_display})',
        'Agents Required', f'Agents w/ {params["shrinkage"]}% Shrinkage',
        'Scheduled Staff', 'Variance', 'Service Level %',
    ])

    by_day = {d: [] for d in DAYS_ORDER}
    for row in calculated:
        if row['day'] in by_day:
            by_day[row['day']].append(row)

    for day_name in DAYS_ORDER:
        for row in sorted(by_day[day_name], key=lambda r: r['hour']):
            key = f"staffing_{day_name}_{row['hour']}"
            # Use override from POST if provided, else fall back to scheduled count
            if key in current_staffing:
                current = current_staffing[key]
            else:
                current = scheduled_map.get((day_name, row['hour']), '')
            variance = (current - row['agents_shrinkage']) if isinstance(current, int) else ''
            writer.writerow([
                row['day'],
                row['hour_label'],
                row['avg_calls'],
                aht_display,
                row['agents_required'],
                row['agents_shrinkage'],
                current,
                variance,
                f"{row['service_level_achieved']}%",
            ])

    return response


@login_required
def erlang_reports(request):
    reports = ErlangReport.objects.all()
    return render(request, 'erlang/reports.html', {'reports': reports})
