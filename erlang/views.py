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
from scheduling.models import Shift, ShiftTemplate, Five9Profile, OvertimeShift, OpenOTShift, Agent, log_action

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

    # Base call agents (current role) — use Agent.role_type as the authoritative source.
    # Five9Profile.role_type is per-account and can be 'regular_agent' on a Kill Team
    # agent's OT account, which would incorrectly include them in regular scheduled staff.
    base_call_ids = set(Agent.objects.filter(
        role_type__in=CALL_ROLES,
        status='active',
    ).values_list('pk', flat=True))

    # For completed past weeks: correct base_call_ids using RoleHistory.
    # base_call_ids reflects each agent's CURRENT role, which is wrong when an agent's
    # role changed AFTER the viewed week ended (e.g. incubation→regular_agent today
    # while viewing last week — they should not count as regular_agent last week).
    today = date.today()
    if week_dates[-1] < today:
        from scheduling.models import RoleHistory as _RH
        # Agents who have any RoleHistory entry dated after this week ended
        post_week_changers = set(
            _RH.objects.filter(
                agent__status='active',
                effective_from__gt=week_dates[-1],
            ).values_list('agent_id', flat=True).distinct()
        )
        if post_week_changers:
            # Find each agent's role that was active AT the end of this week:
            # the entry whose effective_to closed AFTER week_end (i.e. was still open
            # on week_end but was subsequently closed by the post-week change).
            week_end_roles = {
                e['agent_id']: e['role_type']
                for e in _RH.objects.filter(
                    agent_id__in=post_week_changers,
                    effective_from__lte=week_dates[-1],
                    effective_to__gt=week_dates[-1],
                ).values('agent_id', 'role_type')
            }
            for aid in post_week_changers:
                role_then = week_end_roles.get(aid)
                in_call_now = aid in base_call_ids
                in_call_then = role_then in CALL_ROLES if role_then else False
                if in_call_now and not in_call_then:
                    base_call_ids.discard(aid)   # wasn't a call agent during this week
                elif not in_call_now and in_call_then:
                    base_call_ids.add(aid)        # was a call agent during this week

    # Pending role changes that take effect within this week — adjust per date
    pending = list(ScheduledRoleChange.objects.filter(
        effective_date__range=(week_dates[0], week_dates[-1]),
        applied_at__isnull=True,
        cancelled_at__isnull=True,
    ).values('agent_id', 'new_role_type', 'effective_date',
             'new_shift_days', 'new_shift_start_time', 'new_shift_end_time'))

    if pending:
        affected_ids = {p['agent_id'] for p in pending}
        cur_roles = dict(Agent.objects.filter(
            pk__in=affected_ids
        ).values_list('pk', 'role_type'))
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

    # OT shifts — all agents regardless of role; cancelled excluded. Queried
    # here (rather than at its original position further down) so its agent
    # IDs can be folded into the one adherence-status query below; Django
    # caches this queryset's rows on first evaluation (below), so moving the
    # query earlier does not run it twice — the loop further down still
    # consumes the same cached rows.
    ot_shifts = OvertimeShift.objects.filter(date__in=week_dates).exclude(status='cancelled').values(
        'agent_id', 'date', 'start_time', 'end_time'
    )
    ot_agent_ids = {s['agent_id'] for s in ot_shifts}

    # Bulk-fetch every adherence status recorded this week for any agent who
    # could appear on this grid (regular schedule or OT) — ONE query, reused
    # for both the staffing-exclusion check and the popover's status tags.
    from adherence.models import AdherenceRecord
    from wfm.constants import STAFFING_EXCLUDED_STATUSES
    adherence_status_by_agent_date = {
        (r['agent_id'], r['date']): r['status']
        for r in AdherenceRecord.objects.filter(
            date__in=week_dates, agent_id__in=(all_call_ids | ot_agent_ids),
        ).values('agent_id', 'date', 'status')
    }

    # Pre-fetch agent display names
    agent_names = {
        a.pk: str(a)
        for a in Agent.objects.select_related('user').filter(status='active')
    }

    scheduled = {}     # {(day_name, hour): int}
    agents_map = {}    # {(day_name, hour): [{'name': str, 'time': str, 'ot': bool, 'status': str|None}]}
    excluded_map = {}  # {(day_name, hour): [{'name': str, 'reason': str}]} — adherence-excluded agents
    seen = set()      # (day_name, hour, agent_id) — prevent double-counting

    def _add(day_name, h, agent_id, entry):
        if (day_name, h, agent_id) in seen:
            return
        seen.add((day_name, h, agent_id))
        key = (day_name, h)
        scheduled[key] = scheduled.get(key, 0) + 1
        agents_map.setdefault(key, []).append(entry)

    def _add_hours(date_, start_hour, end_hour, agent_id, entry_base, exclude):
        """Add hours for one shift/OT slot. entry_base has no 'status' key yet.
        exclude=True lets a STAFFING_EXCLUDED_STATUSES status divert the agent
        into excluded_map instead of counting them; exclude=False (OT) always
        counts — status is attached for display only, never used to exclude."""
        day_name = date_.strftime('%A')

        def _place(day_name_, h, d):
            status = adherence_status_by_agent_date.get((agent_id, d))
            if exclude and status in STAFFING_EXCLUDED_STATUSES:
                excluded_map.setdefault((day_name_, h), []).append(
                    {'name': entry_base['name'], 'reason': status}
                )
                return
            _add(day_name_, h, agent_id, dict(entry_base, status=status))

        if end_hour <= start_hour:  # overnight — split at midnight
            next_date = date_ + timedelta(days=1)
            next_day_name = next_date.strftime('%A')
            for h in range(start_hour, 24):
                _place(day_name, h, date_)
            for h in range(0, end_hour):
                _place(next_day_name, h, next_date)
        else:
            for h in range(start_hour, end_hour):
                _place(day_name, h, date_)

    # Specific shift overrides — date-aware role check. An override fully
    # governs its date, INCLUDING is_off=True (a one-time day off must stop
    # the recurring template from counting), so fetch all of them and only
    # add hours for working ones.
    shifts = Shift.objects.filter(
        date__in=week_dates, agent_id__in=all_call_ids,
    ).values('agent_id', 'date', 'start_time', 'end_time', 'is_off')

    agents_with_shift_override = set()
    for s in shifts:
        if s['agent_id'] not in call_ids_by_date[s['date']]:
            continue
        agents_with_shift_override.add((s['agent_id'], s['date']))
        if s['is_off'] or not s['start_time'] or not s['end_time']:
            continue
        name = agent_names.get(s['agent_id'], f"Agent {s['agent_id']}")
        label = f"{s['start_time'].strftime('%H:%M')}–{s['end_time'].strftime('%H:%M')}"
        _add_hours(s['date'], s['start_time'].hour, s['end_time'].hour,
                   s['agent_id'], {'name': name, 'time': label, 'ot': False}, exclude=True)

    # Recurring templates — resolved per (agent, date) with the same shared
    # rule the Shifts/Adherence tabs and agent portal use (_best_shift_template:
    # latest effective_from covering the date wins, is_off templates suppress
    # older working ones), instead of adding every window-matching template.
    from scheduling.views import _best_shift_template

    templates_by_agent = {}
    for t in ShiftTemplate.objects.filter(agent_id__in=all_call_ids):
        templates_by_agent.setdefault(t.agent_id, []).append(t)

    for day_date in week_dates:
        for agent_id in call_ids_by_date[day_date]:
            if (agent_id, day_date) in agents_with_shift_override:
                continue
            t = _best_shift_template(templates_by_agent.get(agent_id, []), agent_id, day_date)
            if t is None or t.is_off or not t.start_time or not t.end_time:
                continue
            name = agent_names.get(agent_id, f"Agent {agent_id}")
            label = f"{t.start_time.strftime('%H:%M')}–{t.end_time.strftime('%H:%M')}"
            _add_hours(day_date, t.start_time.hour, t.end_time.hour,
                       agent_id, {'name': name, 'time': label, 'ot': False}, exclude=True)

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
            _add_hours(day_date, start_t.hour, end_t.hour,
                       p['agent_id'], {'name': name, 'time': label, 'ot': False}, exclude=True)

    # OT shifts — all agents regardless of role; cancelled excluded. Never
    # excluded by adherence status — non-cancelled OT counts as coverage
    # exactly as it always has (the agent's status that day, if any, is still
    # attached to the entry for display only).
    for s in ot_shifts:
        name = agent_names.get(s['agent_id'], f"Agent {s['agent_id']}")
        label = f"{s['start_time'].strftime('%H:%M')}–{s['end_time'].strftime('%H:%M')}"
        _add_hours(s['date'], s['start_time'].hour, s['end_time'].hour,
                   s['agent_id'], {'name': name, 'time': label, 'ot': True}, exclude=False)

    return scheduled, agents_map, excluded_map


def _build_open_ot_map(week_start):
    """Count open OT postings covering each (day_name, hour).

    Returns {(day_name, hour): [open_count, filled_count]}. Same hour
    semantics as _build_scheduled_map: start hour inclusive, end hour
    exclusive, overnight shifts split at midnight. Note: filled postings'
    assigned shifts are already counted inside the scheduled map — the
    filled count here is informational only and must not be subtracted
    from variance again.
    """
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    counts = {}

    def _bump(day_name, h, idx):
        key = (day_name, h)
        if key not in counts:
            counts[key] = [0, 0]
        counts[key][idx] += 1

    postings = OpenOTShift.objects.filter(
        date__in=week_dates, status__in=('open', 'filled'),
    ).values('date', 'start_time', 'end_time', 'status', 'assigned_shift__status')

    for p in postings:
        if p['status'] == 'filled':
            # A filled posting only provides coverage while its shift stands
            if p['assigned_shift__status'] in (None, 'cancelled'):
                continue
            idx = 1
        else:
            idx = 0
        day_name = p['date'].strftime('%A')
        start_h, end_h = p['start_time'].hour, p['end_time'].hour
        if end_h <= start_h:  # overnight — split at midnight
            for h in range(start_h, 24):
                _bump(day_name, h, idx)
            next_name = (p['date'] + timedelta(days=1)).strftime('%A')
            for h in range(0, end_h):
                _bump(next_name, h, idx)
        else:
            for h in range(start_h, end_h):
                _bump(day_name, h, idx)

    return counts


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


def _summarize_statuses(entries):
    """Count non-blank, non-'P' status codes among a list of agents_map
    entries (each with a 'status' key). Returns (status, count) pairs, most
    frequent first, ties broken by status name. Used only for the staffing
    popover's header summary — never affects the scheduled count."""
    from collections import Counter
    counts = Counter(e['status'] for e in entries if e.get('status') and e['status'] != 'P')
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


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
                    log_action(request.user, 'Uploaded Erlang CSV',
                               f'{request.FILES["csv_file"].name} for week of {week_start} — {len(rows)} rows')
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
    excluded_map_json = '{}'
    status_summary_json = '{}'
    if raw_rows:
        calculated = calculate_staffing(
            raw_rows,
            params['target_sl'],
            params['target_seconds'],
            params['shrinkage'],
            params['aht_seconds'],
        )
        scheduled_map, agents_map, excluded_map = _build_scheduled_map(week_start)
        days = _build_days(
            calculated, params,
            scheduled_map,
            _build_actual_map(week_start),
            weeks_by_day=_weeks_by_day,
        )

        # OT posting visibility: open/filled counts per hour and the real
        # remaining gap. Filled postings are already inside scheduled_staff
        # (their created OvertimeShift counts as coverage), so the net gap
        # only subtracts open (not yet claimed/approved) postings.
        open_ot_map = _build_open_ot_map(week_start)
        day_date_by_name = {(week_start + timedelta(days=i)).strftime('%A'): week_start + timedelta(days=i)
                            for i in range(7)}
        for day in days:
            day['date'] = day_date_by_name[day['name']]
            for row in day['rows']:
                ot_open, ot_filled = open_ot_map.get((day['name'], row['hour']), (0, 0))
                row['ot_open'] = ot_open
                row['ot_filled'] = ot_filled
                shortage = max(0, row['agents_shrinkage'] - row['scheduled_staff'])
                row['net_gap'] = max(0, shortage - ot_open)
                if shortage > ot_open:
                    row['net_state'] = 'short'    # still need to post more
                elif shortage > 0:
                    row['net_state'] = 'pending'  # covered only by unclaimed postings
                else:
                    row['net_state'] = 'ok'       # covered by scheduled + filled OT
        import json
        agents_map_json = json.dumps({
            f"{day}:{hour}": sorted(entries, key=lambda e: e['name'])
            for (day, hour), entries in agents_map.items()
        })
        excluded_map_json = json.dumps({
            f"{day}:{hour}": sorted(entries, key=lambda e: e['name'])
            for (day, hour), entries in excluded_map.items()
        })
        status_summary = {}
        for (day, hour), entries in agents_map.items():
            pairs = _summarize_statuses(entries)
            if pairs:
                status_summary[f"{day}:{hour}"] = pairs
        status_summary_json = json.dumps(status_summary)

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

    from scheduling.views import _viewer_agent, _is_ot_approver

    return render(request, 'erlang/calculator.html', {
        'days': days,
        'params': params,
        'is_ot_approver': _is_ot_approver(_viewer_agent(request), request.user),
        'today': today,
        'incentive_choices': OvertimeShift.INCENTIVE_CHOICES,
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
        'excluded_map_json': excluded_map_json,
        'status_summary_json': status_summary_json,
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

    scheduled_map, _, _ = _build_scheduled_map(week_start)
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
        week_start=week_start,
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

    log_action(request.user, 'Saved Erlang report', f'"{name}"')
    from django.contrib import messages
    from django.urls import reverse
    messages.success(request, f'Report "{name}" saved.')
    return redirect(f"{reverse('erlang_calculator')}?week_start={week_start_str}")


@login_required
@require_POST
def erlang_delete_report(request, pk):
    from django.shortcuts import get_object_or_404
    report = get_object_or_404(ErlangReport, pk=pk)
    name = report.name
    report.delete()
    log_action(request.user, 'Deleted Erlang report', f'"{name}"')
    from django.contrib import messages
    messages.success(request, 'Report deleted.')
    return redirect('erlang_reports')


@login_required
def erlang_reports(request):
    reports = ErlangReport.objects.all()
    return render(request, 'erlang/reports.html', {'reports': reports})
