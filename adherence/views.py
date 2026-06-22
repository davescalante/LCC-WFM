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
from django.template.loader import render_to_string

from scheduling.models import Shift, ShiftTemplate, ShiftTemplateBlock, ShiftBlock, Agent, Five9Profile, OvertimeShift, log_action
from .models import AdherenceRecord, AdherenceNote, Coding, PayrollAdjustment, DailyUpload, DailyAgentHours


def _refresh_actual_hours(agent_id, coding_date):
    """Recalculate AdherenceRecord.actual_hours after a coding change.

    When codings are added or removed for a date that already has a daily
    upload, the not-ready allowance (12.5% of login+codings) changes, so the
    stored actual_hours must be recomputed to stay consistent with the Daily
    Hours tab.
    """
    billable_usernames = list(
        Five9Profile.objects.filter(agent_id=agent_id, billable=True)
        .values_list('five9_username', flat=True)
    )
    if billable_usernames:
        dah = DailyAgentHours.objects.filter(
            upload__date=coding_date, agent_id=agent_id,
            five9_username__in=billable_usernames
        ).first()
        if not dah:
            # Fall back to any profile (handles edge case with no billable profiles)
            dah = DailyAgentHours.objects.filter(
                upload__date=coding_date, agent_id=agent_id
            ).first()
    else:
        dah = DailyAgentHours.objects.filter(
            upload__date=coding_date, agent_id=agent_id
        ).first()
    if not dah:
        return
    coded_secs = sum(
        c.total_seconds_count()
        for c in Coding.objects.filter(agent_id=agent_id, date=coding_date)
    )
    total_secs = dah.login_seconds + coded_secs
    allowance_secs = int(total_secs * 0.125)
    excess_secs = max(0, dah.not_ready_seconds - allowance_secs)
    login_final_secs = max(0, dah.login_seconds - excess_secs)
    final_hours = Decimal(str(round(login_final_secs / 3600, 6)))
    AdherenceRecord.objects.update_or_create(
        agent_id=agent_id,
        date=coding_date,
        defaults={'actual_hours': final_hours},
    )


BONUS_QUALIFYING = {'P', 'OT', 'MUT', 'VTO', 'P+VTO', 'V'}
BONUS_DISQUALIFYING = {'Absent', 'NCNS', 'T', 'T+VTO', 'T+I', 'I', 'LOA', 'S'}
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
    'T+I':   '#fde8d8',
    'Quit':  '#eeeeee',
    'Baja':  '#eeeeee',
    'V':     '#e3f2fd',
    'IMSS':  '#e0f2f1',
    'LOA':   '#f3e5f5',
    'S':     '#ffccbc',
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


def _hours_evening(shift):
    """Hours attributed to the shift-start calendar day: start→midnight for overnight, full for same-day."""
    if not shift or getattr(shift, 'is_off', False):
        return Decimal('0')
    s, e = shift.start_time, shift.end_time
    if e < s:  # overnight — only count start→midnight
        secs = 86400 - (s.hour * 3600 + s.minute * 60 + s.second)
    else:
        secs = (e.hour * 3600 + e.minute * 60 + e.second) - (s.hour * 3600 + s.minute * 60 + s.second)
    return Decimal(str(round(max(0, secs) / 3600, 6)))


def _hours_morning(shift):
    """Spillover hours on the next calendar day: midnight→end for overnight (0 for same-day)."""
    if not shift or getattr(shift, 'is_off', False):
        return Decimal('0')
    s, e = shift.start_time, shift.end_time
    if e < s:  # overnight — only the post-midnight portion spills
        secs = e.hour * 3600 + e.minute * 60 + e.second
        return Decimal(str(round(secs / 3600, 6)))
    return Decimal('0')


def _block_hours(start_time, end_time):
    """Total hours for a time block; handles overnight."""
    s = start_time.hour * 3600 + start_time.minute * 60 + start_time.second
    e = end_time.hour * 3600 + end_time.minute * 60 + end_time.second
    secs = e - s
    if secs < 0:
        secs += 86400
    return Decimal(str(round(max(0, secs) / 3600, 6)))


# ── Cost of Schedule ──────────────────────────────────────────────────────────

COS_INCLUDE_STATUSES = frozenset({'P', 'Absent', 'NCNS', 'IMSS', 'T', 'T+I', 'OT', 'MUT', 'S'})
_DEFAULT_TARDY = Decimal('0.25')  # 15-minute default if no actual hours logged


def _cos_color(pct):
    if pct is None:
        return '#aaa'
    if pct == 0:
        return '#22c55e'
    if pct <= 10:
        return '#4ade80'
    if pct <= 20:
        return '#d97706'
    if pct <= 35:
        return '#ea580c'
    return '#dc2626'


def _calculate_cos(rows, week_dates):
    """
    Returns (day_data list, cos_week dict) for Cost of Schedule.
    day_data has one entry per day with pct, breakdown, and color.
    Week loss = sum of per-day capped losses (OT can only offset within its own day).
    """
    day_data = []
    week_sched = Decimal('0')
    week_net_loss = Decimal('0')
    week_absent = 0
    week_tardy_count = 0
    week_tardy_loss = Decimal('0')
    week_ot_offset = Decimal('0')

    for i, day_date in enumerate(week_dates):
        sched = Decimal('0')
        raw_loss = Decimal('0')
        ot_offset = Decimal('0')
        absent_count = 0
        tardy_count = 0
        tardy_loss = Decimal('0')

        for row in rows:
            cell = row['cells'][i]
            status = cell.get('status', '') or ''
            if status not in COS_INCLUDE_STATUSES:
                continue
            sched_hrs = cell.get('sched_hrs') or Decimal('0')
            actual_hrs = cell.get('display_hrs') or Decimal('0')

            sched += sched_hrs

            if status in ('Absent', 'NCNS', 'IMSS', 'S'):
                raw_loss += sched_hrs
                absent_count += 1
            elif status in ('T', 'T+I'):
                lost = max(Decimal('0'), sched_hrs - actual_hrs) if actual_hrs > 0 else _DEFAULT_TARDY
                raw_loss += lost
                tardy_loss += lost
                tardy_count += 1
            elif status in ('OT', 'MUT'):
                if actual_hrs > sched_hrs:
                    ot_offset += actual_hrs - sched_hrs

        net_loss = max(Decimal('0'), raw_loss - ot_offset)
        cos_pct = float(net_loss / sched * 100) if sched > 0 else None

        day_data.append({
            'date': day_date,
            'cos_pct': round(cos_pct, 1) if cos_pct is not None else None,
            'cos_color': _cos_color(cos_pct),
            'has_data': sched > 0,
            'sched_hours': float(sched),
            'net_loss': float(net_loss),
            'absent_count': absent_count,
            'tardy_count': tardy_count,
            'tardy_loss': float(tardy_loss),
            'ot_offset': float(ot_offset),
        })

        week_sched += sched
        week_net_loss += net_loss
        week_absent += absent_count
        week_tardy_count += tardy_count
        week_tardy_loss += tardy_loss
        week_ot_offset += ot_offset

    week_pct = float(week_net_loss / week_sched * 100) if week_sched > 0 else None
    cos_week = {
        'cos_pct': round(week_pct, 1) if week_pct is not None else None,
        'cos_color': _cos_color(week_pct),
        'has_data': week_sched > 0,
        'sched_hours': float(week_sched),
        'net_loss': float(week_net_loss),
        'absent_count': week_absent,
        'tardy_count': week_tardy_count,
        'tardy_loss': float(week_tardy_loss),
        'ot_offset': float(week_ot_offset),
    }
    return day_data, cos_week


def _get_week_start(request):
    today = timezone.localdate()
    default = today - timedelta(days=today.weekday())
    raw = request.GET.get('week_start') or request.POST.get('week_start')
    if raw:
        try:
            ws = date.fromisoformat(raw)
            ws = ws - timedelta(days=ws.weekday())
            request.session['adh_week_start'] = ws.isoformat()
            return ws
        except ValueError:
            pass
    saved = request.session.get('adh_week_start')
    if saved:
        try:
            return date.fromisoformat(saved)
        except ValueError:
            pass
    return default


def _build_maps(agents, week_dates):
    shifts_qs = Shift.objects.filter(date__in=week_dates, agent__in=agents)
    shift_map = {(s.agent_id, s.date): s for s in shifts_qs}

    # Fill gaps with the effective recurring template for each agent on each specific date.
    # Must compare per-date (not just day-of-week) so mid-week schedule changes apply
    # correctly without touching prior days in the same week.
    all_templates = list(ShiftTemplate.objects.filter(agent__in=agents))
    # Pre-index by (agent_id, day_of_week) so template lookup is O(k) not O(M) per cell.
    tmpl_by_agent_dow = {}
    for t in all_templates:
        tmpl_by_agent_dow.setdefault((t.agent_id, t.day_of_week), []).append(t)

    agent_ids = [a.pk for a in agents]
    for day_date in week_dates:
        dow = day_date.weekday()
        for agent_id in agent_ids:
            if (agent_id, day_date) in shift_map:
                continue
            best = None
            for t in tmpl_by_agent_dow.get((agent_id, dow), []):
                if t.effective_from is not None and t.effective_from > day_date:
                    continue
                if t.effective_until is not None and t.effective_until < day_date:
                    continue
                if best is None or (t.effective_from or date.min) > (best.effective_from or date.min):
                    best = t
            if best:
                shift_map[(agent_id, day_date)] = best

    records_qs = AdherenceRecord.objects.filter(date__in=week_dates, agent__in=agents)
    record_map = {(r.agent_id, r.date): r for r in records_qs}

    codings_qs = Coding.objects.filter(date__in=week_dates, agent__in=agents)
    coded_map = {}
    for c in codings_qs:
        key = (c.agent_id, c.date)
        coded_map[key] = coded_map.get(key, Decimal('0')) + Decimal(str(c.total_hours()))

    # Multiple OT per day — list-valued map
    ot_qs = OvertimeShift.objects.filter(date__in=week_dates, agent__in=agents).exclude(status='cancelled')
    ot_map = {}
    for s in ot_qs:
        ot_map.setdefault((s.agent_id, s.date), []).append(s)

    # Extra schedule blocks — ShiftTemplate level
    tmpl_ids = list({s.pk for s in shift_map.values() if isinstance(s, ShiftTemplate)})
    tmpl_extra_hrs = {}
    tmpl_extra_labels = {}
    for block in ShiftTemplateBlock.objects.filter(shift_template_id__in=tmpl_ids):
        tid = block.shift_template_id
        tmpl_extra_hrs[tid] = tmpl_extra_hrs.get(tid, Decimal('0')) + _block_hours(block.start_time, block.end_time)
        tmpl_extra_labels.setdefault(tid, []).append(
            f"{block.start_time.strftime('%H:%M')}–{block.end_time.strftime('%H:%M')}"
        )

    # Extra schedule blocks — specific Shift level
    shift_ids_list = [s.pk for s in shifts_qs]
    shift_extra_hrs = {}
    shift_extra_labels = {}
    for block in ShiftBlock.objects.filter(shift_id__in=shift_ids_list):
        sid = block.shift_id
        shift_extra_hrs[sid] = shift_extra_hrs.get(sid, Decimal('0')) + _block_hours(block.start_time, block.end_time)
        shift_extra_labels.setdefault(sid, []).append(
            f"{block.start_time.strftime('%H:%M')}–{block.end_time.strftime('%H:%M')}"
        )

    # Combine into (agent_id, date) keyed maps
    extra_hrs_map = {}
    split_labels_map = {}
    for (agent_id, day_date), src in shift_map.items():
        if isinstance(src, ShiftTemplate):
            extra = tmpl_extra_hrs.get(src.pk, Decimal('0'))
            labels = tmpl_extra_labels.get(src.pk, [])
        else:
            extra = shift_extra_hrs.get(src.pk, Decimal('0'))
            labels = shift_extra_labels.get(src.pk, [])
        if extra:
            extra_hrs_map[(agent_id, day_date)] = extra
        if labels:
            split_labels_map[(agent_id, day_date)] = labels

    return shift_map, record_map, coded_map, ot_map, extra_hrs_map, split_labels_map, tmpl_by_agent_dow


def _build_rows(agents, week_dates, shift_map, record_map, coded_map, ot_map=None, extra_hrs_map=None, split_labels_map=None, tmpl_by_agent_dow=None):
    from scheduling.models import Five9Profile as _Five9Profile
    from finance.models import BillingSettings as _BS

    rows = []
    week_dates_set = set(week_dates)

    # Pre-compute weekly NR seconds from billable DailyAgentHours
    _agent_ids = [a.pk for a in agents]
    _billable_map = {}  # agent_id -> set of usernames (lowercase)
    for _p in _Five9Profile.objects.filter(agent__in=_agent_ids, billable=True).values('agent_id', 'five9_username'):
        _billable_map.setdefault(_p['agent_id'], set()).add(_p['five9_username'].strip().lower())

    _weekly_nr_map = {}  # agent_id -> total NR seconds
    for _row in DailyAgentHours.objects.filter(
        upload__date__in=week_dates, agent__in=_agent_ids
    ).values('agent_id', 'five9_username', 'not_ready_seconds'):
        _aid = _row['agent_id']
        if _aid is None:
            continue
        _bnames = _billable_map.get(_aid)
        if _bnames is None or _row['five9_username'].strip().lower() in _bnames:
            _weekly_nr_map[_aid] = _weekly_nr_map.get(_aid, 0) + _row['not_ready_seconds']

    _billing_settings = _BS.get()

    def _effective_template(agent_id, d):
        dow = d.weekday()
        best = None
        for t in (tmpl_by_agent_dow or {}).get((agent_id, dow), []):
            if t.effective_from is not None and t.effective_from > d:
                continue
            if t.effective_until is not None and t.effective_until < d:
                continue
            if best is None or (t.effective_from or date.min) > (best.effective_from or date.min):
                best = t
        return best

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
        bonus_reasons = []

        for day_date in week_dates:
            shift = shift_map.get((agent.pk, day_date))
            record = record_map.get((agent.pk, day_date))

            is_off = shift.is_off if shift else False
            has_shift = shift is not None

            # Split overnight shifts at midnight — attribute each portion to its calendar day
            extra_block_hrs = (extra_hrs_map or {}).get((agent.pk, day_date), Decimal('0'))
            sched_hrs = _hours_evening(shift) + extra_block_hrs
            ot_shifts = (ot_map or {}).get((agent.pk, day_date)) or []
            ot_hrs = sum(_hours_evening(s) for s in ot_shifts)

            # Previous calendar day: spillover from overnight shifts starting yesterday
            prev_date = day_date - timedelta(days=1)
            if prev_date in week_dates_set:
                prev_shift = shift_map.get((agent.pk, prev_date))
                prev_ot_shifts = (ot_map or {}).get((agent.pk, prev_date)) or []
            else:
                prev_shift = _effective_template(agent.pk, prev_date)
                prev_ot_shifts = []
            prev_not_off = prev_shift is not None and not getattr(prev_shift, 'is_off', False)
            spill_hrs = _hours_morning(prev_shift) if prev_not_off else Decimal('0')
            spill_hrs += sum(_hours_morning(s) for s in prev_ot_shifts)
            has_spillover = spill_hrs > 0

            # Calendar-day scheduled hours = this evening + yesterday's post-midnight continuation
            cal_sched = sched_hrs + ot_hrs + spill_hrs

            # Spillover label for off days: show "↳ 00:00–HH:MM" in purple
            spill_label = None
            if has_spillover and is_off:
                if prev_not_off and prev_shift and _hours_morning(prev_shift) > 0:
                    spill_label = f"00:00–{prev_shift.end_time.strftime('%H:%M')}"
                else:
                    overnight_ots = [s for s in prev_ot_shifts if _hours_morning(s) > 0]
                    if overnight_ots:
                        spill_label = f"00:00–{max(s.end_time for s in overnight_ots).strftime('%H:%M')}"

            # A day is scheduled if there's a non-off shift, OT shift, or overnight spillover
            is_scheduled_day = (has_shift and not is_off) or bool(ot_shifts) or has_spillover

            effective_sched = Decimal('0')

            # Always read status and actual hours from the record
            status = record.status if record else ''
            actual_hrs = record.actual_hours if record else None

            # Scheduled hours only apply when a shift is set up
            if is_scheduled_day:
                if status in ('VTO', 'LOA'):
                    effective_sched = Decimal('0')
                elif status in ('P+VTO', 'T+VTO') and actual_hrs is not None:
                    effective_sched = min(actual_hrs, cal_sched)
                else:
                    effective_sched = cal_sched
                sched_total += effective_sched

            # Present/A/T/I counts and bonus apply whenever a status is set
            if status:
                if status in ('P', 'OT', 'MUT', 'VTO', 'P+VTO', 'T', 'T+VTO', 'T+I', 'I'):
                    total_present += 1
                elif status in ('Absent', 'NCNS', 'S'):
                    total_absent += 1
                if status in ('T', 'T+VTO', 'T+I'):
                    total_tardy += 1
                if status in ('I', 'T+I'):
                    total_incomplete += 1

                if status in BONUS_DISQUALIFYING:
                    bonus = False
                    bonus_determined = True
                    bonus_reasons.append(f"{day_date.strftime('%a %b %d')}: {status}")
                elif status in BONUS_QUALIFYING:
                    bonus_determined = True
                else:
                    bonus = False
                    bonus_determined = True
                    bonus_reasons.append(f"{day_date.strftime('%a %b %d')}: {status}")

            # OT No Show disqualifies the bonus
            for ot in ot_shifts:
                if ot.status == 'no_show':
                    bonus = False
                    bonus_determined = True
                    bonus_reasons.append(
                        f"{day_date.strftime('%a %b %d')}: OT No Show "
                        f"({ot.start_time.strftime('%H:%M')}–{ot.end_time.strftime('%H:%M')})"
                    )

            # Login hours accumulate whenever actual hours exist
            if actual_hrs:
                actual_total += actual_hrs

            cell_coded_hrs = coded_map.get((agent.pk, day_date), Decimal('0'))

            # Cell color — use total (login + codings) vs effective scheduled for color logic
            cell_total = (actual_hrs or Decimal('0')) + cell_coded_hrs
            # Off day with spillover is treated as a partial working day (not grey)
            effective_is_off = is_off and not ot_shifts and not has_spillover
            if not has_shift and not ot_shifts and not has_spillover:
                cell_color = '#fafafa'
            elif effective_is_off:
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
                    'Off' if effective_is_off
                    else f"{shift.start_time.strftime('%H:%M')}–{shift.end_time.strftime('%H:%M')}" if shift and not is_off
                    else ''
                ),
                'shift_start': shift.start_time.strftime('%H:%M') if (shift and not is_off and shift.start_time) else '',
                'shift_end': shift.end_time.strftime('%H:%M') if (shift and not is_off and shift.end_time) else '',
                'is_shift_override': isinstance(shift, Shift),
                'ot_times': [f"{s.start_time.strftime('%H:%M')}–{s.end_time.strftime('%H:%M')}" for s in ot_shifts],
                'split_block_labels': (split_labels_map or {}).get((agent.pk, day_date), []),
                'sched_hrs': cell_sched_hrs,
                'missing_hrs': missing_hrs,
                'is_off': effective_is_off,
                'has_shift': is_scheduled_day or (has_shift and is_off),
                'has_spillover': has_spillover,
                'spill_label': spill_label,
                'status': status,
                'display_hrs': display_hrs,
                'color': cell_color,
                'key': f'status_{agent.pk}_{day_date.isoformat()}',
                'hours_key': f'hours_{agent.pk}_{day_date.isoformat()}',
            })

        coded = sum(coded_map.get((agent.pk, d), Decimal('0')) for d in week_dates)
        adjusted = actual_total + coded

        # Weekly NR cap adjustment
        _nr_secs = _weekly_nr_map.get(agent.pk, 0)
        _nr_hrs = Decimal(str(_nr_secs)) / Decimal('3600')
        _nr_cap = _billing_settings.nr_cap_kill_team_hours if agent.role_type == 'kill_team' else _billing_settings.nr_cap_regular_hours
        _nr_cap_adj = max(Decimal('0'), _nr_hrs - _nr_cap)
        final_adjusted = max(Decimal('0'), adjusted - _nr_cap_adj)

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
            'nr_cap_adj': _nr_cap_adj,
            'final_adjusted': final_adjusted,
            'bonus': bonus_display,
            'bonus_reasons': bonus_reasons,
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
        role_type__in=('supervisor', 'coordinator'), status='active'
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

    # Normalize H:MM:SS → HH:MM:SS so leading zero is optional
    def _pad_time(s):
        parts = s.strip().split(':')
        if parts:
            parts[0] = parts[0].zfill(2)
        return ':'.join(parts)
    start_time = _pad_time(start_time)
    end_time   = _pad_time(end_time)

    # Validate time format and end > start
    from datetime import time as time_cls
    try:
        start_t = time_cls.fromisoformat(start_time)
        end_t   = time_cls.fromisoformat(end_time)
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid time format. Use H:MM:SS or HH:MM:SS (e.g. 8:00:00)'}, status=400)

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

    _refresh_actual_hours(agent_id, coding.date)

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
def edit_coding_ajax(request):
    data = json.loads(request.body)
    coding_id = data.get('coding_id')
    start_time = data.get('start_time', '').strip()
    end_time   = data.get('end_time', '').strip()
    notes      = data.get('notes', '')

    coding = Coding.objects.filter(pk=coding_id).select_related('agent').first()
    if not coding:
        return JsonResponse({'ok': False, 'error': 'Not found'}, status=404)

    def _pad(s):
        parts = s.split(':')
        if parts:
            parts[0] = parts[0].zfill(2)
        return ':'.join(parts)

    start_time = _pad(start_time)
    end_time   = _pad(end_time)

    from datetime import time as time_cls
    try:
        start_t = time_cls.fromisoformat(start_time)
        end_t   = time_cls.fromisoformat(end_time)
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid time format. Use H:MM:SS (e.g. 8:00:00)'}, status=400)

    if end_t <= start_t:
        return JsonResponse({'ok': False, 'error': 'End time must be after start time.'}, status=400)

    coding.start_time = start_t
    coding.end_time   = end_t
    coding.notes      = notes
    coding.save()
    _refresh_actual_hours(coding.agent_id, coding.date)

    log_action(request.user, 'Edited coding',
               f'{coding.agent} — {coding.date}: {coding.start_time.strftime("%H:%M")}–{coding.end_time.strftime("%H:%M")}',
               agent=coding.agent)

    return JsonResponse({
        'ok': True,
        'id': coding.pk,
        'hhmmss': coding.total_hhmmss(),
        'start': coding.start_time.strftime('%H:%M'),
        'start_full': coding.start_time.strftime('%H:%M:%S'),
        'end': coding.end_time.strftime('%H:%M'),
        'end_full': coding.end_time.strftime('%H:%M:%S'),
        'notes': coding.notes,
    })


@login_required
@require_POST
def delete_coding_ajax(request):
    data = json.loads(request.body)
    coding_id = data.get('coding_id')
    coding = Coding.objects.filter(pk=coding_id).select_related('agent').first()
    if coding:
        agent_id = coding.agent_id
        coding_date = coding.date
        log_action(request.user, 'Deleted coding',
                   f'{coding.agent} — {coding.date}: {coding.start_time.strftime("%H:%M")}–{coding.end_time.strftime("%H:%M")}',
                   agent=coding.agent)
        coding.delete()
        _refresh_actual_hours(agent_id, coding_date)
    return JsonResponse({'ok': True})


# ── Page views ────────────────────────────────────────────────────────────────

@login_required
def adherence_week(request):
    week_start = _get_week_start(request)
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    week_end = week_dates[-1]

    supervisor_id, supervisors = _get_supervisor_filter(request)

    if request.method == 'POST':
        agents = Agent.objects.filter(
            Q(status='active', track_attendance=True) |
            Q(status='inactive', separations__status='finalized', separations__remove_from_adherence_date__gt=week_start)
        ).select_related('user', 'supervisor__user').order_by(
            'supervisor__user__last_name', 'supervisor__user__first_name',
            'user__last_name', 'user__first_name'
        )
        agents = _apply_supervisor_filter(agents, supervisor_id)
        agents = agents.filter(
            Q(shifts__date__in=week_dates) |
            Q(overtime_shifts__date__in=week_dates) |
            Q(shift_templates__isnull=False) |
            Q(adherence_records__date__in=week_dates)
        ).distinct()
        shift_map, record_map, *_ = _build_maps(agents, week_dates)
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

    # GET: return the page shell immediately; rows are fetched async via /adherence/rows/
    return render(request, 'adherence/dashboard.html', {
        'week_dates': week_dates,
        'week_start': week_start,
        'week_end': week_end,
        'today': timezone.localdate(),
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
        'status_choices': AdherenceRecord.STATUS_CHOICES,
        'supervisors': supervisors,
        'selected_supervisor': str(supervisor_id) if supervisor_id else '',
    })


@login_required
def adherence_rows_fragment(request):
    """Return rendered tbody rows + COS tfoot as JSON for async loading."""
    week_start = _get_week_start(request)
    week_dates = [week_start + timedelta(days=i) for i in range(7)]

    week_end = week_dates[-1]
    supervisor_id, _ = _get_supervisor_filter(request)
    # Include: active tracked agents + separated agents whose remove_from_adherence_date is
    # after this week's start (so they appear in historical and separation weeks)
    agents = Agent.objects.filter(
        Q(status='active', track_attendance=True) |
        Q(status='inactive', separations__status='finalized', separations__remove_from_adherence_date__gt=week_start)
    ).select_related(
        'user', 'supervisor__user'
    ).prefetch_related('five9_profiles').order_by(
        'supervisor__user__last_name', 'supervisor__user__first_name',
        'user__last_name', 'user__first_name'
    )
    agents = _apply_supervisor_filter(agents, supervisor_id)
    agents = list(agents.filter(
        Q(shifts__date__in=week_dates) |
        Q(overtime_shifts__date__in=week_dates) |
        Q(shift_templates__isnull=False) |
        Q(adherence_records__date__in=week_dates)
    ).distinct())

    shift_map, record_map, coded_map, ot_map, extra_hrs_map, split_labels_map, tmpl_by_agent_dow = _build_maps(agents, week_dates)
    rows = _build_rows(agents, week_dates, shift_map, record_map, coded_map, ot_map=ot_map,
                       extra_hrs_map=extra_hrs_map, split_labels_map=split_labels_map,
                       tmpl_by_agent_dow=tmpl_by_agent_dow)

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

    show_cos = True
    cos_days, cos_week = _calculate_cos(rows, week_dates) if show_cos else ([], {})

    ctx = {
        'rows': rows,
        'week_start': week_start,
        'today': timezone.localdate(),
        'status_choices': AdherenceRecord.STATUS_CHOICES,
        'selected_supervisor': str(supervisor_id) if supervisor_id else '',
        'show_cos': show_cos,
        'cos_days': cos_days,
        'cos_week': cos_week,
    }

    tbody_html = render_to_string('adherence/rows_tbody.html', ctx, request=request)
    tfoot_html = render_to_string('adherence/rows_tfoot.html', ctx, request=request)

    return JsonResponse({'tbody_html': tbody_html, 'tfoot_html': tfoot_html})


@login_required
def codings_week(request):
    week_start = _get_week_start(request)
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    week_end = week_dates[-1]

    supervisor_id, supervisors = _get_supervisor_filter(request)
    agents = Agent.objects.filter(status='active').select_related('user', 'supervisor__user').order_by(
        'supervisor__user__last_name', 'supervisor__user__first_name',
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
                _refresh_actual_hours(agent_id, coding_date)

        elif action == 'delete':
            coding_id = request.POST.get('coding_id')
            coding = Coding.objects.filter(pk=coding_id).first()
            if coding:
                agent_id, coding_date = coding.agent_id, coding.date
                coding.delete()
                _refresh_actual_hours(agent_id, coding_date)

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
        'today': timezone.localdate(),
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
    agents = Agent.objects.filter(status='active', track_attendance=True).select_related(
        'user', 'supervisor__user'
    ).prefetch_related('five9_profiles').order_by(
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
                .prefetch_related('agent__five9_profiles')
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
        shift_map, record_map, coded_map, ot_map, *_ = _build_maps(agents, week_dates)
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
            'Employer', 'Billing Status',
            'Scheduled Hours', 'Actual Login Hours', 'Coded Hours',
            'Adjusted Total Hours', 'Commission Deduction %', 'Adherence Bonus',
            'Suspended Days',
        ])

        for agent in agents:
            sched_total = Decimal('0')
            actual_total = Decimal('0')
            bonus = True
            bonus_determined = False
            suspended_days = 0

            for day_date in week_dates:
                shift = shift_map.get((agent.pk, day_date))
                record = record_map.get((agent.pk, day_date))
                ot_shifts_day = ot_map.get((agent.pk, day_date), [])
                is_off = shift.is_off if shift else False
                is_scheduled_day = (shift and not is_off) or bool(ot_shifts_day)

                if is_scheduled_day:
                    base_sched = _scheduled_hours(shift)
                    ot_hrs = sum(_ot_hours(s) for s in ot_shifts_day)
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
                    if status == 'S':
                        suspended_days += 1
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
                agent.employer,
                agent.billing_status,
                _decimal_to_hhmmss(sched_total),
                _decimal_to_hhmmss(actual_total),
                _decimal_to_hhmmss(coded),
                _decimal_to_hhmmss(adjusted),
                f'{commission:.1f}%',
                bonus_label,
                suspended_days,
            ])

        return response

    # GET — show preview with editable deductions
    shift_map, record_map, coded_map, ot_map, extra_hrs_map, split_labels_map, tmpl_by_agent_dow = _build_maps(agents, week_dates)
    rows = _build_rows(agents, week_dates, shift_map, record_map, coded_map, ot_map=ot_map, extra_hrs_map=extra_hrs_map, split_labels_map=split_labels_map, tmpl_by_agent_dow=tmpl_by_agent_dow)

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


def _zero_missing_scheduled(upload_date, matched_agent_ids):
    """
    After a daily upload, agents who are scheduled on upload_date but absent
    from the file get actual_hours=0 so the grid shows their missing hours.
    """
    matched = set(matched_agent_ids)

    # OT shifts on this date (cancelled shifts don't count as scheduled)
    scheduled = set(
        OvertimeShift.objects.filter(date=upload_date, agent__status='active').exclude(status='cancelled')
        .values_list('agent_id', flat=True)
    )
    # Non-off shift overrides
    scheduled |= set(
        Shift.objects.filter(date=upload_date, is_off=False, agent__status='active')
        .values_list('agent_id', flat=True)
    )
    # ShiftTemplate for this weekday, not overridden by any Shift — use effective date filter
    overridden = set(Shift.objects.filter(date=upload_date).values_list('agent_id', flat=True))
    scheduled |= set(
        ShiftTemplate.objects.filter(
            day_of_week=upload_date.weekday(),
            is_off=False,
            agent__status='active',
        ).filter(
            Q(effective_from__isnull=True) | Q(effective_from__lte=upload_date)
        ).filter(
            Q(effective_until__isnull=True) | Q(effective_until__gte=upload_date)
        ).exclude(agent_id__in=overridden)
        .values_list('agent_id', flat=True)
    )

    for agent_id in (scheduled - matched):
        AdherenceRecord.objects.update_or_create(
            agent_id=agent_id,
            date=upload_date,
            defaults={'actual_hours': Decimal('0')},
        )


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

    billable_usernames_cache = {}
    for dah in dah_objects:
        if not dah.agent_id:
            continue
        # Only update actual_hours from billable profiles
        agent_id = dah.agent_id
        if agent_id not in billable_usernames_cache:
            billable_usernames_cache[agent_id] = set(
                Five9Profile.objects.filter(agent_id=agent_id, billable=True)
                .values_list('five9_username', flat=True)
            )
        billable_names = billable_usernames_cache[agent_id]
        if billable_names and dah.five9_username not in billable_names:
            continue  # Skip non-billable profile rows

        coded_secs = sum(
            c.total_seconds_count()
            for c in Coding.objects.filter(agent_id=dah.agent_id, date=upload_date)
        )
        total_secs = dah.login_seconds + coded_secs
        allowance_secs = int(total_secs * 0.125)
        excess_secs = max(0, dah.not_ready_seconds - allowance_secs)
        login_final_secs = max(0, dah.login_seconds - excess_secs)
        final_hours = Decimal(str(round(login_final_secs / 3600, 6)))

        AdherenceRecord.objects.update_or_create(
            agent_id=dah.agent_id,
            date=upload_date,
            defaults={'actual_hours': final_hours},
        )

    matched_agent_ids = {dah.agent_id for dah in dah_objects if dah.agent_id}
    _zero_missing_scheduled(upload_date, matched_agent_ids)

    log_action(request.user, 'Uploaded daily login file',
               f'{csv_file.name} for {upload_date} — {len(dah_objects)} rows matched, {unmatched} unmatched')

    return JsonResponse({
        'ok': True,
        'row_count': len(dah_objects),
        'unmatched_count': unmatched,
        'filename': csv_file.name,
    })


@login_required
@require_POST
def rematch_daily_upload(request):
    """Re-match unmatched DailyAgentHours rows against the current Five9Profile list."""
    data = json.loads(request.body)
    date_str = data.get('date')
    try:
        upload_date = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'Invalid date'}, status=400)

    upload = DailyUpload.objects.filter(date=upload_date).first()
    if not upload:
        return JsonResponse({'ok': False, 'error': 'No upload found for this date'}, status=404)

    agent_map = {
        p.five9_username.strip().lower(): p.agent
        for p in Five9Profile.objects.filter(
            five9_username__gt='', agent__status='active'
        ).select_related('agent')
    }

    newly_matched = 0
    billable_usernames_cache = {}
    for dah in DailyAgentHours.objects.filter(upload=upload, agent__isnull=True):
        agent = agent_map.get(dah.five9_username.strip().lower())
        if not agent:
            continue
        dah.agent = agent
        dah.save(update_fields=['agent'])
        newly_matched += 1

        # Only update actual_hours from billable profiles
        if agent.pk not in billable_usernames_cache:
            billable_usernames_cache[agent.pk] = set(
                Five9Profile.objects.filter(agent=agent, billable=True)
                .values_list('five9_username', flat=True)
            )
        billable_names = billable_usernames_cache[agent.pk]
        if billable_names and dah.five9_username not in billable_names:
            continue  # Skip non-billable profile rows

        coded_secs = sum(
            c.total_seconds_count()
            for c in Coding.objects.filter(agent=agent, date=upload_date)
        )
        total_secs = dah.login_seconds + coded_secs
        allowance_secs = int(total_secs * 0.125)
        excess_secs = max(0, dah.not_ready_seconds - allowance_secs)
        login_final_secs = max(0, dah.login_seconds - excess_secs)
        final_hours = Decimal(str(round(login_final_secs / 3600, 6)))

        AdherenceRecord.objects.update_or_create(
            agent=agent,
            date=upload_date,
            defaults={'actual_hours': final_hours},
        )

    still_unmatched = DailyAgentHours.objects.filter(upload=upload, agent__isnull=True).count()
    upload.unmatched_count = still_unmatched
    upload.save(update_fields=['unmatched_count'])

    matched_agent_ids = set(
        DailyAgentHours.objects.filter(upload=upload, agent__isnull=False)
        .values_list('agent_id', flat=True)
    )
    _zero_missing_scheduled(upload_date, matched_agent_ids)

    return JsonResponse({'ok': True, 'newly_matched': newly_matched, 'still_unmatched': still_unmatched})


@login_required
@require_POST
def delete_daily_upload_ajax(request):
    data = json.loads(request.body)
    date_str = data.get('date')
    try:
        DailyUpload.objects.filter(date=date.fromisoformat(date_str)).delete()
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid date'}, status=400)
    log_action(request.user, 'Deleted daily upload', f'Deleted upload for {date_str}')
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


# ── Agent self-service: My Adherence ──────────────────────────────────────────

@login_required
def agent_my_adherence(request):
    try:
        agent = request.user.agent
    except Exception:
        return redirect('agent_my_shifts')
    if agent.role != 'agent':
        return redirect('adherence_dashboard')

    today = timezone.localdate()
    default_week_start = today - timedelta(days=today.weekday())
    raw = request.GET.get('week_start')
    try:
        week_start = date.fromisoformat(raw) if raw else default_week_start
        week_start -= timedelta(days=week_start.weekday())
    except (ValueError, TypeError):
        week_start = default_week_start

    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    week_end = week_dates[-1]

    from scheduling.models import Shift, ShiftTemplate

    overrides = {s.date: s for s in Shift.objects.filter(agent=agent, date__in=week_dates)}
    all_templates = list(ShiftTemplate.objects.filter(agent=agent))
    records = {r.date: r for r in AdherenceRecord.objects.filter(agent=agent, date__in=week_dates)}
    codings_per_day = {}
    for c in Coding.objects.filter(agent=agent, date__in=week_dates):
        codings_per_day[c.date] = codings_per_day.get(c.date, Decimal('0')) + Decimal(str(c.total_hours()))

    def _best_tmpl(d):
        dow = d.weekday()
        best = None
        for t in all_templates:
            if t.day_of_week != dow:
                continue
            if t.effective_from is not None and t.effective_from > d:
                continue
            if t.effective_until is not None and t.effective_until < d:
                continue
            if best is None or (t.effective_from or date.min) > (best.effective_from or date.min):
                best = t
        return best

    def _fmt(d):
        if d is None or d == Decimal('0'):
            return '—'
        total_min = int(round(float(d) * 60))
        h, m = divmod(total_min, 60)
        return f'{h}:{m:02d}'

    _DAY_ABBR = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    cells = []
    sched_total = Decimal('0')
    logged_total = Decimal('0')
    coded_total = Decimal('0')
    bonus = True
    bonus_determined = False
    bonus_reasons = []

    for i, d in enumerate(week_dates):
        override = overrides.get(d)
        tmpl = None if override else _best_tmpl(d)
        src = override or tmpl
        record = records.get(d)
        is_off = src.is_off if src else False

        status = record.status if record else ''

        if src and not is_off and getattr(src, 'start_time', None) and getattr(src, 'end_time', None) and status not in ('VTO', 'LOA'):
            sched_hrs = _scheduled_hours(src)
            sched_total += sched_hrs
        else:
            sched_hrs = None
        actual_hrs = record.actual_hours if record else None
        coded_hrs = codings_per_day.get(d) or None

        if actual_hrs:
            logged_total += actual_hrs
        if coded_hrs:
            coded_total += coded_hrs

        if status in BONUS_DISQUALIFYING:
            bonus = False
            bonus_determined = True
            bonus_reasons.append(f"{d.strftime('%a %b %d')}: {status}")
        elif status in BONUS_QUALIFYING:
            bonus_determined = True
        elif status:
            bonus = False
            bonus_determined = True
            bonus_reasons.append(f"{d.strftime('%a %b %d')}: {status}")

        total_hrs = (actual_hrs or Decimal('0')) + (coded_hrs or Decimal('0'))

        cells.append({
            'day': _DAY_ABBR[i],
            'date': d,
            'is_today': d == today,
            'status': status,
            'status_color': STATUS_COLORS.get(status, '') if status else '',
            'is_off': is_off,
            'sched_display': _fmt(sched_hrs),
            'logged_display': _fmt(actual_hrs),
            'coded_display': _fmt(coded_hrs),
            'total_display': _fmt(total_hrs) if (actual_hrs or coded_hrs) else '—',
        })

    if bonus is False:
        bonus_display = 'No'
    elif bonus_determined:
        bonus_display = 'Yes'
    else:
        bonus_display = '—'

    return render(request, 'agent/my_adherence.html', {
        'agent': agent,
        'cells': cells,
        'week_start': week_start,
        'week_end': week_end,
        'today': today,
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
        'sched_total': _fmt(sched_total),
        'logged_total': _fmt(logged_total),
        'coded_total': _fmt(coded_total),
        'adjusted_total': _fmt(logged_total + coded_total),
        'bonus': bonus_display,
        'bonus_reasons': bonus_reasons,
    })

