from datetime import date, timedelta
from django.db.models import Q, F
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.urls import reverse
from django.utils import timezone
from .models import Agent, Shift, ShiftBlock, Break, EmploymentPeriod, Five9Profile, ShiftTemplate, ShiftTemplateBlock, OvertimeShift, RoleHistory, ScheduledRoleChange, LoginLogoutUpload, AgentLoginSession, OTShiftVerification, log_action
from .forms import AgentUserForm, AgentForm, ShiftForm, BreakForm


_ADMIN_ROLE_TYPES = {'supervisor', 'qa', 'cs', 'testing', 'sms_email', 'admin_training', 'coordinator'}


def _sync_pending_schedule(src):
    """
    Immediately create ShiftTemplates for a pending role change's new schedule
    so that the Shifts view and Adherence tab reflect the upcoming schedule
    before the effective date arrives. Idempotent — safe to call repeatedly.
    Also expires old open templates so they don't bleed past the effective date.
    """
    if not (src.new_shift_days and src.new_shift_start_time and src.new_shift_end_time):
        return
    ag = src.agent
    eff = src.effective_date
    # Expire old open templates so they stop at the effective date
    ShiftTemplate.objects.filter(
        agent=ag, effective_until__isnull=True
    ).exclude(effective_from=eff).update(effective_until=eff)
    # Create new templates for all 7 days — working days with times, rest as off
    existing_days = set(
        ShiftTemplate.objects.filter(agent=ag, effective_from=eff)
        .values_list('day_of_week', flat=True)
    )
    working_days = set(src.new_shift_days)
    for day_num in range(7):
        if day_num in existing_days:
            continue
        if day_num in working_days:
            ShiftTemplate.objects.create(
                agent=ag, day_of_week=day_num,
                start_time=src.new_shift_start_time,
                end_time=src.new_shift_end_time,
                is_off=False, effective_from=eff,
            )
        else:
            ShiftTemplate.objects.create(
                agent=ag, day_of_week=day_num,
                start_time=None, end_time=None,
                is_off=True, effective_from=eff,
            )
    # Pre-apply supervisor so all tabs reflect the upcoming change immediately
    if src.new_supervisor_id and ag.supervisor_id != src.new_supervisor_id:
        ag.supervisor_id = src.new_supervisor_id
        ag.save(update_fields=['supervisor'])


def apply_due_role_changes(agent=None):
    """
    Apply all pending ScheduledRoleChanges whose effective_date has arrived.
    Pass agent= to limit to a single agent (used lazily on profile load).
    Returns the number of changes applied.
    """
    today = timezone.localdate()
    qs = ScheduledRoleChange.objects.filter(
        effective_date__lte=today,
        applied_at__isnull=True,
        cancelled_at__isnull=True,
    ).select_related('agent__supervisor')
    if agent is not None:
        qs = qs.filter(agent=agent)

    count = 0
    for src in qs:
        ag = src.agent
        old_role_type = ag.role_type
        new_role_type = src.new_role_type
        new_role = 'admin' if new_role_type in _ADMIN_ROLE_TYPES else 'agent'

        # Update Agent
        ag.role_type = new_role_type
        ag.role = new_role
        update_fields = ['role_type', 'role']
        if src.new_supervisor is not None:
            ag.supervisor = src.new_supervisor
            update_fields.append('supervisor')
        ag.save(update_fields=update_fields)

        # Update matching Five9Profiles
        ag.five9_profiles.filter(role_type=old_role_type).update(role_type=new_role_type)

        # Close open RoleHistory entry and open a new one
        open_entry = ag.role_history.filter(effective_to__isnull=True).first()
        if open_entry:
            open_entry.effective_to = src.effective_date
            open_entry.save(update_fields=['effective_to'])
        RoleHistory.objects.create(
            agent=ag,
            role=new_role,
            role_type=new_role_type,
            supervisor=src.new_supervisor if src.new_supervisor else ag.supervisor,
            employer=ag.employer,
            billing_status=ag.billing_status,
            effective_from=src.effective_date,
            changed_by=src.scheduled_by,
        )

        # Apply new schedule if provided — idempotent, _sync_pending_schedule may have run already
        if src.new_shift_days and src.new_shift_start_time and src.new_shift_end_time:
            ShiftTemplate.objects.filter(
                agent=ag, effective_until__isnull=True
            ).exclude(effective_from=src.effective_date).update(effective_until=src.effective_date)
            existing_days = set(
                ShiftTemplate.objects.filter(agent=ag, effective_from=src.effective_date)
                .values_list('day_of_week', flat=True)
            )
            for day_num in src.new_shift_days:
                if day_num not in existing_days:
                    ShiftTemplate.objects.create(
                        agent=ag,
                        day_of_week=day_num,
                        start_time=src.new_shift_start_time,
                        end_time=src.new_shift_end_time,
                        is_off=False,
                        effective_from=src.effective_date,
                    )

        log_action(
            src.scheduled_by,
            'Scheduled role change applied',
            f'Automatically applied: role changed to {src.get_new_role_type_display()} effective {src.effective_date}',
            agent=ag,
        )

        src.applied_at = timezone.now()
        src.save(update_fields=['applied_at'])
        count += 1

    return count


@login_required
def dashboard(request):
    today = timezone.localdate()
    shifts_today = Shift.objects.filter(date=today).select_related('agent__user')
    agents = Agent.objects.select_related('user').order_by('user__last_name')
    return render(request, 'scheduling/dashboard.html', {
        'shifts_today': shifts_today,
        'agents': agents,
        'today': today,
    })


@login_required
def agent_list(request):
    supervisors = Agent.objects.filter(
        role_type__in=('supervisor', 'coordinator'), status='active'
    ).select_related('user').order_by('user__last_name', 'user__first_name')

    if 'supervisor' in request.GET:
        supervisor_id = request.GET.get('supervisor', '')
        request.session['supervisor_filter'] = supervisor_id
    else:
        supervisor_id = request.session.get('supervisor_filter', '')

    agents = Agent.objects.select_related('user', 'supervisor__user').order_by(
        'user__last_name', 'user__first_name'
    )
    if supervisor_id:
        try:
            agents = agents.filter(supervisor_id=int(supervisor_id))
        except (ValueError, TypeError):
            pass

    return render(request, 'scheduling/agent_list.html', {
        'agents': agents,
        'supervisors': supervisors,
        'selected_supervisor': str(supervisor_id) if supervisor_id else '',
    })


@login_required
def agent_detail(request, pk):
    agent = get_object_or_404(Agent, pk=pk)
    apply_due_role_changes(agent=agent)
    shifts = Shift.objects.filter(agent=agent).order_by('-date')[:30]
    pending_role_change = agent.scheduled_role_changes.filter(
        applied_at__isnull=True, cancelled_at__isnull=True
    ).select_related('new_supervisor__user').first()
    if pending_role_change:
        _sync_pending_schedule(pending_role_change)
        agent.refresh_from_db(fields=['supervisor'])
    supervisors = Agent.objects.filter(
        role_type__in=('supervisor', 'coordinator'), status='active'
    ).select_related('user').order_by('user__last_name', 'user__first_name')
    return render(request, 'scheduling/agent_detail.html', {
        'agent': agent,
        'shifts': shifts,
        'pending_role_change': pending_role_change,
        'role_type_choices': Agent.ROLE_TYPE_CHOICES,
        'day_choices': ShiftTemplate.DAY_CHOICES,
        'supervisors': supervisors,
    })


def _save_five9_profiles(request, agent):
    """Process Five9Profile rows from POST: update existing, delete flagged, create new."""
    for profile in list(agent.five9_profiles.all()):
        if request.POST.get(f'five9_{profile.pk}_delete'):
            profile.delete()
            continue
        username = request.POST.get(f'five9_{profile.pk}_username', '').strip()
        if username:
            profile.label = request.POST.get(f'five9_{profile.pk}_label', '').strip()
            profile.five9_username = username
            profile.five9_password = request.POST.get(f'five9_{profile.pk}_password', '').strip()
            profile.role_type = request.POST.get(f'five9_{profile.pk}_role_type', '')
            profile.save()

    i = 0
    while f'new_five9_{i}_username' in request.POST:
        username = request.POST.get(f'new_five9_{i}_username', '').strip()
        if username:
            Five9Profile.objects.create(
                agent=agent,
                label=request.POST.get(f'new_five9_{i}_label', '').strip(),
                five9_username=username,
                five9_password=request.POST.get(f'new_five9_{i}_password', '').strip(),
                role_type=request.POST.get(f'new_five9_{i}_role_type', ''),
            )
        i += 1


@login_required
def agent_create(request):
    user_form = AgentUserForm(request.POST or None)
    agent_form = AgentForm(request.POST or None)
    if request.method == 'POST' and user_form.is_valid() and agent_form.is_valid():
        user = user_form.save(commit=False)
        password = user_form.cleaned_data.get('password')
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        agent = agent_form.save(commit=False)
        agent.user = user
        agent.save()
        from django.utils import timezone as _tz
        RoleHistory.objects.create(
            agent=agent,
            role=agent.role,
            role_type=agent.role_type or '',
            supervisor=agent.supervisor,
            employer=agent.employer,
            billing_status=agent.billing_status,
            effective_from=agent.start_date or _tz.localdate(),
            changed_by=request.user,
        )
        _save_five9_profiles(request, agent)
        if not (agent.role == 'admin' and agent.role_type in ('supervisor', 'coordinator')):
            user.set_unusable_password()
            user.save()
        start_date = request.POST.get('start_date', '').strip()
        if start_date:
            from .models import EmploymentPeriod
            EmploymentPeriod.objects.create(agent=agent, start_date=start_date)
        log_action(request.user, 'Created agent profile', f'Created {user.get_full_name()}', agent=agent)
        messages.success(request, f"User {user.get_full_name()} created successfully.")
        return redirect('agent_list')
    return render(request, 'scheduling/agent_form.html', {
        'user_form': user_form,
        'agent_form': agent_form,
        'title': 'Add User',
        'five9_profiles': [],
        'role_type_choices': Agent.ROLE_TYPE_CHOICES,
        'is_own_profile': False,
    })


@login_required
def agent_edit(request, pk):
    agent = get_object_or_404(Agent, pk=pk)

    # Auto-seed period from legacy start_date if no periods exist yet
    if agent.start_date and not agent.employment_periods.exists():
        EmploymentPeriod.objects.create(
            agent=agent,
            start_date=agent.start_date,
            end_date=agent.termination_date,
            reason_ended='terminated' if agent.termination_date else '',
        )

    if request.method == 'POST':
        user_form = AgentUserForm(request.POST, instance=agent.user)
        agent_form = AgentForm(request.POST, instance=agent)
        if user_form.is_valid() and agent_form.is_valid():
            user = user_form.save(commit=False)
            password = user_form.cleaned_data.get('password')
            if password:
                user.set_password(password)
            user.save()
            # Capture before save
            _old = {
                'role': agent.role, 'role_type': agent.role_type,
                'supervisor_id': agent.supervisor_id,
                'employer': agent.employer, 'billing_status': agent.billing_status,
            }
            agent = agent_form.save()
            # Record role history if tracked fields changed
            _new = {
                'role': agent.role, 'role_type': agent.role_type,
                'supervisor_id': agent.supervisor_id,
                'employer': agent.employer, 'billing_status': agent.billing_status,
            }
            if _old != _new:
                from django.utils import timezone as _tz
                today = _tz.localdate()
                if not agent.role_history.exists():
                    # Seed initial entry from old values
                    RoleHistory.objects.create(
                        agent=agent, role=_old['role'], role_type=_old['role_type'] or '',
                        supervisor_id=_old['supervisor_id'], employer=_old['employer'],
                        billing_status=_old['billing_status'],
                        effective_from=agent.start_date or today,
                        effective_to=today, changed_by=request.user,
                    )
                else:
                    open_entry = agent.role_history.filter(effective_to__isnull=True).first()
                    if open_entry:
                        open_entry.effective_to = today
                        open_entry.save(update_fields=['effective_to'])
                RoleHistory.objects.create(
                    agent=agent, role=agent.role, role_type=agent.role_type or '',
                    supervisor=agent.supervisor, employer=agent.employer,
                    billing_status=agent.billing_status,
                    effective_from=today, changed_by=request.user,
                )
            if not (agent.role == 'admin' and agent.role_type in ('supervisor', 'coordinator')):
                user.set_unusable_password()
                user.save()

            # Update or delete existing periods
            for period in list(agent.employment_periods.all()):
                if request.POST.get(f'period_{period.pk}_delete'):
                    period.delete()
                    continue
                start = request.POST.get(f'period_{period.pk}_start', '').strip()
                if start:
                    period.start_date = start
                    period.end_date = request.POST.get(f'period_{period.pk}_end', '').strip() or None
                    period.reason_ended = request.POST.get(f'period_{period.pk}_reason', '')
                    period.notes = request.POST.get(f'period_{period.pk}_notes', '')
                    period.save()

            # Create new periods (indexed rows added via JS)
            i = 0
            while f'new_{i}_start' in request.POST:
                start = request.POST.get(f'new_{i}_start', '').strip()
                if start:
                    EmploymentPeriod.objects.create(
                        agent=agent,
                        start_date=start,
                        end_date=request.POST.get(f'new_{i}_end', '').strip() or None,
                        reason_ended=request.POST.get(f'new_{i}_reason', ''),
                        notes=request.POST.get(f'new_{i}_notes', ''),
                    )
                i += 1

            _save_five9_profiles(request, agent)
            log_action(request.user, 'Edited agent profile', f'Edited {user.get_full_name()}', agent=agent)
            messages.success(request, f"User {user.get_full_name()} updated successfully.")
            return redirect('agent_detail', pk=agent.pk)
    else:
        user_form = AgentUserForm(instance=agent.user)
        agent_form = AgentForm(instance=agent)

    return render(request, 'scheduling/agent_form.html', {
        'user_form': user_form,
        'agent_form': agent_form,
        'title': 'Edit User',
        'agent': agent,
        'periods': agent.employment_periods.all(),
        'reason_choices': EmploymentPeriod.REASON_CHOICES,
        'five9_profiles': agent.five9_profiles.all(),
        'role_type_choices': Agent.ROLE_TYPE_CHOICES,
        'is_own_profile': (agent.user == request.user),
    })


@login_required
def agent_delete(request, pk):
    agent = get_object_or_404(Agent, pk=pk)
    if request.method == 'POST':
        name = agent.user.get_full_name()
        log_action(request.user, 'Deleted agent profile', f'Deleted {name}')
        agent.user.delete()
        messages.success(request, f"User {name} deleted.")
        return redirect('agent_list')
    return render(request, 'scheduling/confirm_delete.html', {
        'object': agent,
        'cancel_url': reverse('agent_list'),
    })


def _save_shift_template(agent, day_of_week, effective_date, is_off, start, end, notes):
    """
    Cap any existing active template before effective_date and create a new one from effective_date.
    If an active template already starts on this exact date, just update it in place.
    Any templates that start AFTER effective_date are deleted — they are fully superseded.
    effective_date is the specific date the change takes effect (not necessarily week Monday).
    Returns the (template, created) tuple.
    """
    active = (
        ShiftTemplate.objects
        .filter(agent=agent, day_of_week=day_of_week)
        .filter(Q(effective_from__isnull=True) | Q(effective_from__lte=effective_date))
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gte=effective_date))
        .order_by(F('effective_from').desc(nulls_last=True))
        .first()
    )
    if active and active.effective_from == effective_date:
        # Same date — update in place; no history to preserve
        active.start_time = start or '09:00'
        active.end_time = end or '17:00'
        active.is_off = is_off
        active.notes = notes
        active.effective_until = None
        active.save()
        new_tmpl = active
        created = False
    else:
        if active:
            # Earlier date — cap it so prior days keep their correct schedule
            active.effective_until = effective_date - timedelta(days=1)
            active.save(update_fields=['effective_until'])
        new_tmpl = ShiftTemplate.objects.create(
            agent=agent, day_of_week=day_of_week,
            start_time=start or '09:00',
            end_time=end or '17:00',
            is_off=is_off, notes=notes,
            effective_from=effective_date, effective_until=None,
        )
        created = True
    # Delete any templates that start after effective_date — the new template supersedes them.
    ShiftTemplate.objects.filter(
        agent=agent, day_of_week=day_of_week, effective_from__gt=effective_date,
    ).exclude(pk=new_tmpl.pk).delete()
    return new_tmpl, created


@login_required
def shift_list(request):
    today = timezone.localdate()
    default_week_start = today - timedelta(days=today.weekday())

    week_start_str = request.GET.get('week_start')
    if week_start_str:
        try:
            week_start = date.fromisoformat(week_start_str)
            week_start = week_start - timedelta(days=week_start.weekday())
            request.session['shift_list_week_start'] = week_start.isoformat()
        except ValueError:
            week_start = default_week_start
    else:
        saved = request.session.get('shift_list_week_start')
        if saved:
            try:
                week_start = date.fromisoformat(saved)
            except ValueError:
                week_start = default_week_start
        else:
            week_start = default_week_start

    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    week_end = week_dates[-1]

    supervisors = Agent.objects.filter(
        role_type__in=('supervisor', 'coordinator'), status='active'
    ).select_related('user').order_by('user__last_name', 'user__first_name')

    if 'supervisor' in request.GET:
        supervisor_id = request.GET.get('supervisor', '')
        request.session['shift_supervisor_filter'] = supervisor_id
    else:
        supervisor_id = request.session.get('shift_supervisor_filter', '')

    if 'section' in request.GET:
        section_filter = request.GET.get('section', '')
        request.session['shift_section_filter'] = section_filter
    else:
        section_filter = request.session.get('shift_section_filter', '')

    agents = Agent.objects.filter(status='active').select_related('user', 'supervisor__user').order_by('user__last_name', 'user__first_name')
    if supervisor_id:
        try:
            agents = agents.filter(supervisor_id=int(supervisor_id))
        except (ValueError, TypeError):
            pass

    prev_week_start = week_start - timedelta(days=7)
    prev_week_dates = [prev_week_start + timedelta(days=i) for i in range(7)]

    # Override Shift records (specific dates)
    shifts_qs = Shift.objects.filter(date__in=week_dates, agent__in=agents)
    shift_map = {(s.agent_id, s.date): s for s in shifts_qs}

    # Recurring templates — per-day effective lookup so mid-week changes show correctly
    # and the Shifts tab stays consistent with the Attendance tab.
    all_templates_ls = list(ShiftTemplate.objects.filter(agent__in=agents))
    # Pre-group by (agent_id, day_of_week) for fast per-day iteration
    from collections import defaultdict as _dd
    _tmpl_by_key = _dd(list)
    for _t in all_templates_ls:
        _tmpl_by_key[(_t.agent_id, _t.day_of_week)].append(_t)

    templates_qs = ShiftTemplate.objects.filter(agent__in=agents)  # kept for .exists() checks below
    template_map = {}  # (agent_id, day_date) -> ShiftTemplate
    for day_date in week_dates:
        dow = day_date.weekday()
        for _ag in agents:
            candidates = _tmpl_by_key.get((_ag.pk, dow), [])
            best = None
            for _t in candidates:
                if _t.effective_from is not None and _t.effective_from > day_date:
                    continue
                if _t.effective_until is not None and _t.effective_until < day_date:
                    continue
                if best is None or (_t.effective_from or date.min) > (best.effective_from or date.min):
                    best = _t
            if best:
                template_map[(_ag.pk, day_date)] = best

    has_prev_week = Shift.objects.filter(date__in=prev_week_dates).exists()
    has_this_week = shifts_qs.exists() or templates_qs.exists()

    prev_week_agent_ids = set(
        Shift.objects.filter(date__in=prev_week_dates).values_list('agent_id', flat=True)
    )
    this_week_override_ids = {s.agent_id for s in shifts_qs}

    rows = []
    for agent in agents:
        cells = []
        for day_date in week_dates:
            override = shift_map.get((agent.pk, day_date))
            t = template_map.get((agent.pk, day_date))
            cells.append({
                'date': day_date,
                'shift': override,
                'template': t if not override else None,
            })
        rows.append({
            'agent': agent,
            'cells': cells,
            'has_prev_week_shifts': agent.pk in prev_week_agent_ids,
            'has_this_week_shifts': agent.pk in this_week_override_ids,
            'has_template': any(
                template_map.get((agent.pk, d)) is not None
                for d in week_dates
            ),
        })

    # Classify each agent into one of four sections
    _GROUP_ORDER = {'morning': 0, 'afternoon': 1, 'kill_team': 2, 'admin': 3}
    # Fixed sort priority for admin supervisors; others sort alphabetically after
    _ADMIN_SUP_PRIORITY = {'Jesus Urbina': 0, 'Andrea Jones': 1}

    def _shift_group(agent, tmpl_map, cells):
        if agent.role == 'admin':
            return 'admin'
        if agent.role_type == 'kill_team':
            return 'kill_team'
        for d in week_dates:
            t = tmpl_map.get((agent.pk, d))
            if t and not t.is_off and t.start_time:
                return 'morning' if t.start_time.hour < 10 else 'afternoon'
        for cell in cells:
            s = cell['shift']
            if s and not s.is_off and s.start_time:
                return 'morning' if s.start_time.hour < 10 else 'afternoon'
        return 'afternoon'

    for row in rows:
        row['group'] = _shift_group(row['agent'], template_map, row['cells'])

    def _sort_key(r):
        g = _GROUP_ORDER[r['group']]
        a = r['agent']
        if r['group'] == 'admin':
            if not a.supervisor:
                sup_sort = (2, '', '')
            else:
                name = a.supervisor.user.get_full_name()
                if name in _ADMIN_SUP_PRIORITY:
                    sup_sort = (_ADMIN_SUP_PRIORITY[name], '', '')
                else:
                    sup_sort = (3, a.supervisor.user.last_name, a.supervisor.user.first_name)
        else:
            sup_sort = (0, '', '')
        return (g,) + sup_sort + (a.user.last_name, a.user.first_name)

    rows.sort(key=_sort_key)

    # Apply section filter
    if section_filter:
        rows = [r for r in rows if r['group'] == section_filter]

    # Flag first admin row per supervisor so template can draw sub-dividers
    prev_admin_sup_id = None
    for row in rows:
        if row['group'] == 'admin':
            row['show_supervisor_header'] = (row['agent'].supervisor_id != prev_admin_sup_id)
            prev_admin_sup_id = row['agent'].supervisor_id
        else:
            row['show_supervisor_header'] = False

    return render(request, 'scheduling/shift_list.html', {
        'rows': rows,
        'week_dates': week_dates,
        'week_start': week_start,
        'week_end': week_end,
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
        'has_prev_week': has_prev_week,
        'has_this_week': has_this_week,
        'supervisors': supervisors,
        'selected_supervisor': str(supervisor_id) if supervisor_id else '',
        'section_filter': section_filter,
        'today': today,
    })


@login_required
def shift_copy_from_prev(request):
    if request.method != 'POST':
        return redirect('shift_list')

    week_start_str = request.POST.get('week_start')
    try:
        week_start = date.fromisoformat(week_start_str)
        week_start = week_start - timedelta(days=week_start.weekday())
    except (ValueError, TypeError):
        messages.error(request, "Invalid week.")
        return redirect('shift_list')

    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    prev_week_start = week_start - timedelta(days=7)
    prev_week_dates = [prev_week_start + timedelta(days=i) for i in range(7)]

    prev_shifts = list(Shift.objects.filter(date__in=prev_week_dates).select_related('agent'))
    if not prev_shifts:
        messages.warning(request, "No shifts found in the previous week to copy.")
        return redirect(f"{reverse('shift_list')}?week_start={week_start.isoformat()}")

    # Delete existing shifts for this week, then bulk-create from previous week
    Shift.objects.filter(date__in=week_dates).delete()
    day_offset_map = {src: tgt for src, tgt in zip(prev_week_dates, week_dates)}
    Shift.objects.bulk_create([
        Shift(
            agent=s.agent,
            date=day_offset_map[s.date],
            start_time=s.start_time,
            end_time=s.end_time,
            is_off=s.is_off,
            notes=s.notes,
        )
        for s in prev_shifts
    ])

    messages.success(request, f"Schedule copied from week of {prev_week_start.strftime('%B %d')} to {week_start.strftime('%B %d, %Y')}.")
    return redirect(f"{reverse('shift_list')}?week_start={week_start.isoformat()}")


@login_required
def shift_copy_agent_from_prev(request):
    """Copy one specific agent's shifts from the previous week to the target week."""
    if request.method != 'POST':
        return redirect('shift_list')

    week_start_str = request.POST.get('week_start')
    agent_id = request.POST.get('agent_id')
    try:
        week_start = date.fromisoformat(week_start_str)
        week_start = week_start - timedelta(days=week_start.weekday())
        agent = get_object_or_404(Agent, pk=agent_id)
    except (ValueError, TypeError):
        messages.error(request, "Invalid parameters.")
        return redirect('shift_list')

    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    prev_week_start = week_start - timedelta(days=7)
    prev_week_dates = [prev_week_start + timedelta(days=i) for i in range(7)]

    prev_shifts = list(Shift.objects.filter(agent=agent, date__in=prev_week_dates))
    if not prev_shifts:
        messages.warning(request, f"No shifts found for {agent} in the previous week.")
        return redirect(f"{reverse('shift_list')}?week_start={week_start.isoformat()}")

    Shift.objects.filter(agent=agent, date__in=week_dates).delete()
    day_offset_map = {src: tgt for src, tgt in zip(prev_week_dates, week_dates)}
    Shift.objects.bulk_create([
        Shift(
            agent=agent,
            date=day_offset_map[s.date],
            start_time=s.start_time,
            end_time=s.end_time,
            is_off=s.is_off,
            notes=s.notes,
        )
        for s in prev_shifts
    ])

    messages.success(request, f"Copied last week's schedule for {agent} to {week_start.strftime('%B %d, %Y')}.")
    return redirect(f"{reverse('shift_list')}?week_start={week_start.isoformat()}")


@login_required
def shift_week(request):
    agents = Agent.objects.select_related('user').order_by('user__last_name')
    DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

    today = timezone.localdate()
    default_week_start = today - timedelta(days=today.weekday())

    selected_agent_id = request.GET.get('agent') or request.POST.get('agent')
    week_start_str = request.GET.get('week_start') or request.POST.get('week_start')

    try:
        week_start = date.fromisoformat(week_start_str) if week_start_str else default_week_start
        week_start = week_start - timedelta(days=week_start.weekday())
    except ValueError:
        week_start = default_week_start

    week_dates = [week_start + timedelta(days=i) for i in range(7)]

    if request.method == 'POST' and selected_agent_id:
        agent = get_object_or_404(Agent, pk=selected_agent_id)
        edit_type = request.POST.get('edit_type', 'permanent')

        def _day_configs():
            for i, day_date in enumerate(week_dates):
                is_off = request.POST.get(f'day_{i}_off') == 'on'
                start = request.POST.get(f'day_{i}_start', '').strip()
                end = request.POST.get(f'day_{i}_end', '').strip()
                notes = request.POST.get(f'day_{i}_notes', '')
                yield i, day_date, is_off, start, end, notes

        if edit_type == 'permanent':
            eff_str = request.POST.get('effective_date', '').strip()
            if not eff_str or eff_str == 'today':
                eff_date = today
            else:
                try:
                    eff_date = date.fromisoformat(eff_str)
                except (ValueError, TypeError):
                    eff_date = today
            partial_days = []
            for i, day_date, is_off, start, end, notes in _day_configs():
                if is_off or (start and end):
                    tmpl, _ = _save_shift_template(agent, i, eff_date, is_off, start, end, notes)
                    ShiftTemplateBlock.objects.filter(shift_template=tmpl).delete()
                    for n in (2, 3):
                        b_start = request.POST.get(f'day_{i}_block{n}_start', '').strip()
                        b_end = request.POST.get(f'day_{i}_block{n}_end', '').strip()
                        if b_start and b_end:
                            ShiftTemplateBlock.objects.create(
                                shift_template=tmpl, block_number=n,
                                start_time=b_start, end_time=b_end
                            )
                elif (start and not end) or (end and not start):
                    partial_days.append(DAYS[i])
            if partial_days:
                messages.warning(request, f"⚠ {', '.join(partial_days)} not saved — both a start and end time are required. Please re-enter those days.")
            log_action(request.user, 'Saved recurring schedule', f'Permanent schedule for {agent} week of {week_start}', agent=agent)
            messages.success(request, f"Recurring schedule saved for {agent}. This schedule will appear every week.")

        elif edit_type == 'one_time':
            partial_days = []
            for i, day_date, is_off, start, end, notes in _day_configs():
                if is_off or (start and end):
                    shift_obj, _ = Shift.objects.update_or_create(
                        agent=agent, date=day_date,
                        defaults={
                            'start_time': start or '09:00',
                            'end_time': end or '17:00',
                            'is_off': is_off,
                            'notes': notes,
                        }
                    )
                    ShiftBlock.objects.filter(shift=shift_obj).delete()
                    for n in (2, 3):
                        b_start = request.POST.get(f'day_{i}_block{n}_start', '').strip()
                        b_end = request.POST.get(f'day_{i}_block{n}_end', '').strip()
                        if b_start and b_end:
                            ShiftBlock.objects.create(
                                shift=shift_obj, block_number=n,
                                start_time=b_start, end_time=b_end
                            )
                elif (start and not end) or (end and not start):
                    partial_days.append(DAYS[i])
            if partial_days:
                messages.warning(request, f"⚠ {', '.join(partial_days)} not saved — both a start and end time are required. Please re-enter those days.")
            log_action(request.user, 'Saved one-time schedule', f'One-time schedule for {agent} week of {week_start}', agent=agent)
            messages.success(request, f"One-time schedule saved for week of {week_start.strftime('%B %d, %Y')} for {agent}.")

        elif edit_type == 'date_range':
            range_start_str = request.POST.get('range_start', '').strip()
            range_end_str = request.POST.get('range_end', '').strip()
            try:
                range_start = date.fromisoformat(range_start_str)
                range_start -= timedelta(days=range_start.weekday())
                range_end = date.fromisoformat(range_end_str)
                range_end -= timedelta(days=range_end.weekday())
            except (ValueError, TypeError):
                messages.error(request, "Please select a valid start and end date for the range.")
                return redirect(f"{reverse('shift_week')}?agent={selected_agent_id}&week_start={week_start.isoformat()}")

            configs = list(_day_configs())
            current_week = range_start
            week_count = 0
            while current_week <= range_end:
                for i, day_date, is_off, start, end, notes in configs:
                    target = current_week + timedelta(days=i)
                    if is_off or (start and end):
                        shift_obj, _ = Shift.objects.update_or_create(
                            agent=agent, date=target,
                            defaults={
                                'start_time': start or '09:00',
                                'end_time': end or '17:00',
                                'is_off': is_off,
                                'notes': notes,
                            }
                        )
                        ShiftBlock.objects.filter(shift=shift_obj).delete()
                        for n in (2, 3):
                            b_start = request.POST.get(f'day_{i}_block{n}_start', '').strip()
                            b_end = request.POST.get(f'day_{i}_block{n}_end', '').strip()
                            if b_start and b_end:
                                ShiftBlock.objects.create(
                                    shift=shift_obj, block_number=n,
                                    start_time=b_start, end_time=b_end
                                )
                current_week += timedelta(days=7)
                week_count += 1
            log_action(request.user, 'Saved date-range schedule',
                       f'Schedule for {agent} from {range_start} to {range_end} ({week_count} weeks)', agent=agent)
            messages.success(
                request,
                f"Schedule applied to {week_count} week(s) "
                f"({range_start.strftime('%b %d')} – {range_end.strftime('%b %d, %Y')}) for {agent}."
            )

        return redirect(f"{reverse('shift_list')}?week_start={week_start.isoformat()}")

    # ── GET: pre-fill from overrides first, then templates ───────────────────
    overrides = {}
    templates = {}
    if selected_agent_id:
        for s in Shift.objects.filter(agent_id=selected_agent_id, date__in=week_dates):
            overrides[s.date] = s
        for t in ShiftTemplate.objects.filter(agent_id=selected_agent_id):
            in_range = (
                (t.effective_from is None or t.effective_from <= week_start)
                and (t.effective_until is None or t.effective_until >= week_start)
            )
            if not in_range:
                continue
            existing = templates.get(t.day_of_week)
            if existing is None or (t.effective_from or date.min) > (existing.effective_from or date.min):
                templates[t.day_of_week] = t

    # Fetch extra blocks for pre-filling
    tmpl_extra_map = {}  # day_of_week -> list of ShiftTemplateBlock
    for tmpl in templates.values():
        blocks = list(tmpl.extra_blocks.all())
        if blocks:
            tmpl_extra_map[tmpl.day_of_week] = blocks

    shift_extra_map = {}  # date -> list of ShiftBlock
    for shift_obj in overrides.values():
        blocks = list(shift_obj.extra_blocks.all())
        if blocks:
            shift_extra_map[shift_obj.date] = blocks

    days = []
    for i, day_date in enumerate(week_dates):
        override = overrides.get(day_date)
        tmpl = templates.get(i)
        src = override or tmpl
        # Extra blocks for this day
        if override:
            raw_extra = shift_extra_map.get(day_date, [])
        elif tmpl:
            raw_extra = tmpl_extra_map.get(i, [])
        else:
            raw_extra = []
        extra_blocks = [
            {'n': b.block_number, 'start': b.start_time.strftime('%H:%M'), 'end': b.end_time.strftime('%H:%M')}
            for b in raw_extra
        ]
        days.append({
            'index': i,
            'name': DAYS[i],
            'date': day_date,
            'start': src.start_time.strftime('%H:%M') if src and src.start_time and not src.is_off else '',
            'end': src.end_time.strftime('%H:%M') if src and src.end_time and not src.is_off else '',
            'is_off': src.is_off if src else False,
            'notes': src.notes if src else '',
            'from_template': bool(tmpl and not override),
            'has_override': bool(override),
            'extra_blocks': extra_blocks,
        })

    has_any_template = bool(templates)

    return render(request, 'scheduling/shift_week.html', {
        'agents': agents,
        'selected_agent_id': int(selected_agent_id) if selected_agent_id else None,
        'week_start': week_start,
        'week_end': week_dates[-1],
        'days': days,
        'has_any_template': has_any_template,
        'week_start_iso': week_start.isoformat(),
        'today': today,
        'today_iso': today.isoformat(),
    })


@login_required
def shift_edit(request, pk):
    shift = get_object_or_404(Shift, pk=pk)
    form = ShiftForm(request.POST or None, instance=shift)
    if request.method == 'POST' and form.is_valid():
        form.save()
        week_start = shift.date - timedelta(days=shift.date.weekday())
        messages.success(request, "Shift updated successfully.")
        return redirect(f"{reverse('shift_list')}?week_start={week_start.isoformat()}")
    return render(request, 'scheduling/shift_form.html', {
        'form': form,
        'title': 'Edit Shift',
        'shift': shift,
    })


@login_required
def shift_delete(request, pk):
    from django.http import JsonResponse
    shift = get_object_or_404(Shift, pk=pk)
    if request.method == 'POST':
        if shift.date < date.today():
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'error': 'Cannot delete a past shift.'}, status=400)
            messages.error(request, "Cannot delete a past shift — historical records are preserved.")
            return redirect(f"{reverse('shift_list')}?week_start={(shift.date - timedelta(days=shift.date.weekday())).isoformat()}")
        week_start = shift.date - timedelta(days=shift.date.weekday())
        agent = shift.agent
        log_action(request.user, 'Deleted shift override',
                   f'{agent} on {shift.date.isoformat()}: '
                   f'{shift.start_time.strftime("%H:%M")}–{shift.end_time.strftime("%H:%M")}',
                   agent=agent)
        shift.delete()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'deleted': True, 'pk': pk})
        messages.success(request, "Shift deleted.")
        return redirect(f"{reverse('shift_list')}?week_start={week_start.isoformat()}")
    return render(request, 'scheduling/confirm_delete.html', {
        'object': shift,
        'cancel_url': reverse('shift_list'),
    })


@login_required
def shift_clear_recurring(request):
    """AJAX: clear recurring schedule for an agent from a given week forward."""
    from django.http import JsonResponse
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    agent_pk = request.POST.get('agent_pk')
    week_start_str = request.POST.get('week_start', '')
    agent = get_object_or_404(Agent, pk=agent_pk)

    try:
        ws = date.fromisoformat(week_start_str)
        ws = ws - timedelta(days=ws.weekday())  # ensure Monday
    except (ValueError, TypeError):
        ws = date.today()
        ws = ws - timedelta(days=ws.weekday())

    end_date = ws - timedelta(days=1)  # last day before the clearing week

    count = 0
    for tmpl in ShiftTemplate.objects.filter(agent=agent):
        if tmpl.effective_from is None or tmpl.effective_from < ws:
            # Template was active before this week — cap it so past weeks keep their display
            tmpl.effective_until = end_date
            tmpl.save(update_fields=['effective_until'])
        else:
            # Template started this week or later — remove it entirely
            tmpl.delete()
        count += 1

    log_action(request.user, 'Cleared recurring schedule',
               f'{agent}: {count} template day(s) cleared from {ws}', agent=agent)
    return JsonResponse({'deleted': count})


def _get_week_start(request):
    """Return Monday of the selected week from GET param, session, or current week."""
    today = timezone.localdate()
    default = today - timedelta(days=today.weekday())
    raw = request.GET.get('week_start')
    if raw:
        try:
            ws = date.fromisoformat(raw)
            ws = ws - timedelta(days=ws.weekday())
            request.session['sched_week_start'] = ws.isoformat()
            return ws
        except (ValueError, TypeError):
            pass
    saved = request.session.get('sched_week_start')
    if saved:
        try:
            return date.fromisoformat(saved)
        except ValueError:
            pass
    return default


def _get_supervisor_filter(request):
    """Returns (supervisor_id_str, supervisors_qs). Reads GET param saving to session."""
    supervisors = Agent.objects.filter(
        role_type__in=('supervisor', 'coordinator'), status='active'
    ).select_related('user').order_by('user__last_name', 'user__first_name')

    if 'supervisor' in request.GET:
        val = request.GET.get('supervisor', '')
        request.session['shift_supervisor_filter'] = val
        return val, supervisors

    return request.session.get('shift_supervisor_filter', ''), supervisors


def _apply_supervisor_filter(agents_qs, supervisor_id):
    if supervisor_id:
        try:
            return agents_qs.filter(supervisor_id=int(supervisor_id))
        except (ValueError, TypeError):
            pass
    return agents_qs


@login_required
def overtime_list(request):
    week_start = _get_week_start(request)
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    week_end = week_dates[-1]

    supervisor_id, supervisors = _get_supervisor_filter(request)
    agents = Agent.objects.filter(status='active').select_related('user').order_by(
        'user__last_name', 'user__first_name'
    )
    agents = _apply_supervisor_filter(agents, supervisor_id)

    status_filter = request.GET.get('status_filter', '')
    if 'status_filter' in request.GET:
        request.session['ot_status_filter'] = status_filter
    else:
        status_filter = request.session.get('ot_status_filter', '')

    ot_qs = OvertimeShift.objects.filter(date__in=week_dates, agent__in=agents).order_by('start_time')
    if status_filter:
        ot_qs = ot_qs.filter(status=status_filter)
    ot_map = {}
    for s in ot_qs:
        ot_map.setdefault((s.agent_id, s.date), []).append(s)

    # Fetch verifications for this week's OT shifts
    ot_pks = [s.pk for shifts in ot_map.values() for s in shifts]
    verif_map = {
        v.ot_shift_id: v
        for v in OTShiftVerification.objects.filter(ot_shift_id__in=ot_pks)
    }
    for shifts in ot_map.values():
        for s in shifts:
            s.ot_verif = verif_map.get(s.pk)

    # Last login-logout upload covering any day this week
    last_ll_upload = (
        LoginLogoutUpload.objects.filter(sessions__date__in=week_dates)
        .order_by('-uploaded_at').first()
    )

    rows = []
    for agent in agents:
        cells = []
        week_offered = None
        week_earned = None
        for day_date in week_dates:
            ot_shifts = ot_map.get((agent.pk, day_date), [])
            active_shifts = [s for s in ot_shifts if s.status != 'cancelled']
            offered_vals = [s.incentive_offered() for s in active_shifts if s.incentive_offered() is not None]
            earned_vals = [s.incentive_earned() for s in active_shifts if s.incentive_earned() is not None]
            cell_offered = sum(offered_vals) if offered_vals else None
            cell_earned = sum(earned_vals) if earned_vals else None
            if cell_offered is not None:
                week_offered = (week_offered or 0) + float(cell_offered)
            if cell_earned is not None:
                week_earned = (week_earned or 0) + float(cell_earned)
            cells.append({
                'date': day_date,
                'ot_shifts': ot_shifts,
                'has_active_ot': bool(active_shifts),
                'offered': cell_offered,
                'earned': cell_earned,
            })
        if any(c['ot_shifts'] for c in cells):
            rows.append({'agent': agent, 'cells': cells, 'week_offered': week_offered, 'week_earned': week_earned})

    return render(request, 'scheduling/overtime_list.html', {
        'rows': rows,
        'week_dates': week_dates,
        'week_start': week_start,
        'week_end': week_end,
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
        'supervisors': supervisors,
        'selected_supervisor': str(supervisor_id) if supervisor_id else '',
        'status_filter': status_filter,
        'last_ll_upload': last_ll_upload,
    })


@login_required
def verify_ot_upload(request):
    import csv, io, json as _json
    from datetime import datetime, timedelta as td
    from django.http import JsonResponse
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    csv_file = request.FILES.get('file')
    if not csv_file:
        return JsonResponse({'ok': False, 'error': 'No file uploaded.'}, status=400)

    try:
        content = csv_file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'Could not read file: {e}'}, status=400)

    # Parse all sessions — index by (username, date)
    by_username_date = {}   # {(username, date): [(login_at, logout_at, secs)]}
    all_usernames = set()   # every username that appears anywhere in the file

    for row in rows:
        username = (row.get('AGENT') or '').strip().lower()
        date_str = (row.get('DATE') or '').strip()
        login_ts = (row.get('LOGIN TIMESTAMP') or '').strip()
        logout_ts = (row.get('LOGOUT TIMESTAMP') or '').strip()
        login_time = (row.get('LOGIN TIME') or '').strip()
        if not username or not date_str:
            continue
        try:
            row_date = datetime.strptime(date_str, '%Y/%m/%d').date()
        except ValueError:
            continue
        try:
            login_at = datetime.strptime(login_ts, '%a, %d %b %Y %H:%M:%S')
            logout_at = datetime.strptime(logout_ts, '%a, %d %b %Y %H:%M:%S')
        except ValueError:
            continue
        try:
            p = login_time.split(':')
            secs = int(p[0]) * 3600 + int(p[1]) * 60 + int(p[2])
        except (ValueError, IndexError):
            secs = max(0, int((logout_at - login_at).total_seconds()))
        all_usernames.add(username)
        by_username_date.setdefault((username, row_date), []).append((login_at, logout_at, secs))

    dates_in_file = {d for _, d in by_username_date.keys()}
    if not dates_in_file:
        return JsonResponse({'ok': False, 'error': 'No valid data found in file.'}, status=400)

    # Build agent → username(s) map from Five9Profile
    profiles = Five9Profile.objects.filter(five9_username__gt='').select_related('agent')
    agent_usernames = {}  # agent_id -> set of usernames
    for p in profiles:
        uname = p.five9_username.strip().lower()
        agent_usernames.setdefault(p.agent_id, set()).add(uname)

    # Replace sessions for these dates and create new upload record
    AgentLoginSession.objects.filter(date__in=dates_in_file).delete()
    upload = LoginLogoutUpload.objects.create(filename=csv_file.name, row_count=len(rows))

    # Build agent_map for matching
    agent_map = {
        p.five9_username.strip().lower(): p.agent
        for p in profiles if p.agent and p.agent.status == 'active'
    }

    session_objects = []
    for (username, row_date), sessions in by_username_date.items():
        agent = agent_map.get(username)
        for login_at, logout_at, secs in sessions:
            session_objects.append(AgentLoginSession(
                upload=upload, agent=agent, five9_username=username,
                date=row_date, login_at=login_at, logout_at=logout_at,
                session_seconds=secs,
            ))
    AgentLoginSession.objects.bulk_create(session_objects)

    # Verify OT shifts for dates covered
    ot_shifts = OvertimeShift.objects.filter(date__in=dates_in_file).select_related('agent')
    OTShiftVerification.objects.filter(ot_shift__in=ot_shifts).delete()

    # Fetch codings for all relevant agents on dates in the file
    from adherence.models import Coding
    coding_intervals = {}  # {(agent_id, date): [(start_dt, end_dt)]}
    for c in Coding.objects.filter(date__in=dates_in_file).values('agent_id', 'date', 'start_time', 'end_time'):
        dt_s = datetime.combine(c['date'], c['start_time'])
        dt_e = datetime.combine(c['date'], c['end_time'])
        if dt_e > dt_s:
            coding_intervals.setdefault((c['agent_id'], c['date']), []).append((dt_s, dt_e))

    def _merge_and_sum(intervals):
        if not intervals:
            return 0
        ivs = sorted(intervals)
        cs, ce = ivs[0]
        total = 0
        for s, e in ivs[1:]:
            if s <= ce:
                ce = max(ce, e)
            else:
                total += (ce - cs).total_seconds()
                cs, ce = s, e
        total += (ce - cs).total_seconds()
        return int(total)

    new_verifs = []
    for ot in ot_shifts:
        usernames = agent_usernames.get(ot.agent_id, set())
        username_found = bool(usernames & all_usernames)

        shift_start = datetime.combine(ot.date, ot.start_time)
        shift_end = datetime.combine(ot.date, ot.end_time)
        if shift_end <= shift_start:
            shift_end += td(days=1)
        shift_secs = int((shift_end - shift_start).total_seconds())

        # Clip each interval to the shift window
        def _clip(s, e):
            cs, ce = max(s, shift_start), min(e, shift_end)
            return (cs, ce) if ce > cs else None

        five9_ivs = []
        for uname in usernames:
            for login_at, logout_at, _ in by_username_date.get((uname, ot.date), []):
                iv = _clip(login_at, logout_at)
                if iv:
                    five9_ivs.append(iv)

        coding_ivs = []
        for dt_s, dt_e in coding_intervals.get((ot.agent_id, ot.date), []):
            iv = _clip(dt_s, dt_e)
            if iv:
                coding_ivs.append(iv)

        five9_secs = min(_merge_and_sum(five9_ivs), shift_secs)
        merged_secs = min(_merge_and_sum(five9_ivs + coding_ivs), shift_secs)
        coding_secs = merged_secs - five9_secs  # net additional seconds codings contributed

        new_verifs.append(OTShiftVerification(
            ot_shift=ot, upload=upload,
            verified_seconds=merged_secs,
            five9_seconds=five9_secs,
            coding_seconds=coding_secs,
            shift_seconds=shift_secs,
            username_found=username_found,
        ))
    OTShiftVerification.objects.bulk_create(new_verifs)

    log_action(request.user, 'Uploaded OT verification report',
               f'{csv_file.name} — {len(dates_in_file)} date(s), {len(new_verifs)} shifts verified')

    return JsonResponse({
        'ok': True,
        'dates': sorted(d.isoformat() for d in dates_in_file),
        'shifts_verified': len(new_verifs),
        'filename': csv_file.name,
    })


@login_required
def overtime_week(request):
    DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

    agents = Agent.objects.filter(status='active').select_related('user').order_by(
        'user__last_name', 'user__first_name'
    )

    today = timezone.localdate()
    default_week_start = today - timedelta(days=today.weekday())

    selected_agent_id = request.GET.get('agent') or request.POST.get('agent')
    week_start_str = request.GET.get('week_start') or request.POST.get('week_start')

    try:
        week_start = date.fromisoformat(week_start_str) if week_start_str else default_week_start
        week_start = week_start - timedelta(days=week_start.weekday())
    except (ValueError, TypeError):
        week_start = default_week_start

    week_dates = [week_start + timedelta(days=i) for i in range(7)]

    if request.method == 'POST' and selected_agent_id:
        from decimal import Decimal, InvalidOperation
        agent = get_object_or_404(Agent, pk=selected_agent_id)

        for i, day_date in enumerate(week_dates):
            count = int(request.POST.get(f'day_{i}_ot_count', 0) or 0)
            submitted_pks = set()

            for j in range(count):
                pk_str = request.POST.get(f'day_{i}_ot_{j}_pk', '').strip()
                remove = request.POST.get(f'day_{i}_ot_{j}_remove', '').strip()
                start = request.POST.get(f'day_{i}_ot_{j}_start', '').strip()
                end = request.POST.get(f'day_{i}_ot_{j}_end', '').strip()
                notes = request.POST.get(f'day_{i}_ot_{j}_notes', '').strip()
                incentive_type = request.POST.get(f'day_{i}_ot_{j}_incentive_type', 'none').strip()
                if incentive_type not in ('none', 'time_and_a_half', 'power_hour'):
                    incentive_type = 'none'
                inc_hrs_str = request.POST.get(f'day_{i}_ot_{j}_incentivized_hours', '').strip()
                base_rate_str = request.POST.get(f'day_{i}_ot_{j}_base_hourly_rate', '').strip()
                try:
                    inc_hrs = Decimal(inc_hrs_str) if inc_hrs_str else None
                except InvalidOperation:
                    inc_hrs = None
                try:
                    base_rate = Decimal(base_rate_str) if base_rate_str else None
                except InvalidOperation:
                    base_rate = None

                if remove == 'true':
                    if pk_str:
                        OvertimeShift.objects.filter(pk=pk_str, agent=agent).delete()
                    continue

                if not (start and end):
                    if pk_str:
                        OvertimeShift.objects.filter(pk=pk_str, agent=agent).delete()
                    continue

                defaults = {
                    'start_time': start,
                    'end_time': end,
                    'notes': notes,
                    'incentive_type': incentive_type,
                    'incentivized_hours': inc_hrs,
                    'base_hourly_rate': base_rate,
                }

                if pk_str:
                    try:
                        ot_obj = OvertimeShift.objects.get(pk=pk_str, agent=agent)
                        for k, v in defaults.items():
                            setattr(ot_obj, k, v)
                        ot_obj.save()
                        submitted_pks.add(ot_obj.pk)
                        log_action(request.user, 'Updated OT shift',
                                   f'{agent} on {day_date.isoformat()}: {start}–{end}', agent=agent)
                    except OvertimeShift.DoesNotExist:
                        pass
                else:
                    ot_obj = OvertimeShift.objects.create(agent=agent, date=day_date, **defaults)
                    submitted_pks.add(ot_obj.pk)
                    log_action(request.user, 'Added OT shift',
                               f'{agent} on {day_date.isoformat()}: {start}–{end}', agent=agent)

        messages.success(request, f"OT shifts saved for {agent} — week of {week_start.strftime('%B %d, %Y')}.")
        return redirect(f"{reverse('overtime_list')}?week_start={week_start.isoformat()}")

    # GET: pre-fill from existing OT records for selected agent
    days = []
    agent_hourly_rate = ''
    agent_schedule = {}  # day_index -> list of {start, end} for overlap check

    if selected_agent_id:
        try:
            selected_agent = Agent.objects.get(pk=selected_agent_id)
            if selected_agent.hourly_rate:
                agent_hourly_rate = str(selected_agent.hourly_rate)
        except Agent.DoesNotExist:
            selected_agent = None

        # Build scheduled blocks for overlap check
        for tmpl in ShiftTemplate.objects.filter(agent_id=selected_agent_id):
            if not tmpl.is_off and tmpl.start_time and tmpl.end_time:
                blocks = [{'start': tmpl.start_time.strftime('%H:%M'), 'end': tmpl.end_time.strftime('%H:%M')}]
                for eb in tmpl.extra_blocks.all():
                    blocks.append({'start': eb.start_time.strftime('%H:%M'), 'end': eb.end_time.strftime('%H:%M')})
                agent_schedule[tmpl.day_of_week] = blocks

        # All OT shifts for this agent this week, grouped by date
        ot_by_date = {}
        for s in OvertimeShift.objects.filter(agent_id=selected_agent_id, date__in=week_dates).order_by('start_time'):
            ot_by_date.setdefault(s.date, []).append(s)

        for i, day_date in enumerate(week_dates):
            day_ot_shifts = ot_by_date.get(day_date, [])
            ot_entries = []
            for j, s in enumerate(day_ot_shifts):
                ot_entries.append({
                    'j': j,
                    'pk': s.pk,
                    'start': s.start_time.strftime('%H:%M'),
                    'end': s.end_time.strftime('%H:%M'),
                    'notes': s.notes,
                    'incentive_type': s.incentive_type,
                    'incentivized_hours': str(s.incentivized_hours) if s.incentivized_hours is not None else '',
                    'base_hourly_rate': str(s.base_hourly_rate) if s.base_hourly_rate is not None else agent_hourly_rate,
                })
            days.append({
                'index': i,
                'name': DAYS[i],
                'date': day_date,
                'ot_entries': ot_entries,
                'has_ot': bool(day_ot_shifts),
                'ot_count': len(day_ot_shifts),
            })

    import json
    return render(request, 'scheduling/overtime_week.html', {
        'agents': agents,
        'selected_agent_id': int(selected_agent_id) if selected_agent_id else None,
        'days': days,
        'week_start': week_start,
        'week_end': week_dates[-1],
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
        'week_start_iso': week_start.isoformat(),
        'agent_hourly_rate': agent_hourly_rate,
        'agent_schedule_json': json.dumps(agent_schedule),
    })


@login_required
def overtime_delete(request, pk):
    from django.http import JsonResponse
    ot_shift = get_object_or_404(OvertimeShift, pk=pk)
    if request.method == 'POST':
        week_start = ot_shift.date - timedelta(days=ot_shift.date.weekday())
        agent = ot_shift.agent
        log_action(request.user, 'Deleted OT shift',
                   f'{agent} on {ot_shift.date.isoformat()}: '
                   f'{ot_shift.start_time.strftime("%H:%M")}–{ot_shift.end_time.strftime("%H:%M")}',
                   agent=agent)
        ot_shift.delete()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'deleted': True, 'pk': pk})
        messages.success(request, "OT shift deleted.")
        return redirect(f"{reverse('overtime_list')}?week_start={week_start.isoformat()}")
    return render(request, 'scheduling/confirm_delete.html', {
        'object': ot_shift,
        'cancel_url': reverse('overtime_list'),
    })


@login_required
def shift_quick_edit(request):
    """AJAX: create/update a one-time Shift override (or permanent ShiftTemplate) for a specific agent+date."""
    from django.http import JsonResponse
    import json as _json
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    try:
        data = _json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    agent_pk = data.get('agent_pk')
    date_str = data.get('date', '')
    start = (data.get('start') or '').strip()
    end = (data.get('end') or '').strip()
    is_off = bool(data.get('is_off'))
    permanent = bool(data.get('permanent'))
    extra_blocks = data.get('extra_blocks') or []

    agent = get_object_or_404(Agent, pk=agent_pk)
    try:
        day_date = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid date'}, status=400)

    if not is_off and not (start and end):
        return JsonResponse({'error': 'Start and end time required'}, status=400)

    if permanent:
        day_of_week = day_date.weekday()
        eff_date = timezone.localdate()  # Permanent changes take effect from today
        tmpl, _ = _save_shift_template(agent, day_of_week, eff_date, is_off, start, end, '')
        ShiftTemplateBlock.objects.filter(shift_template=tmpl).delete()
        for n, block in enumerate(extra_blocks[:2], start=2):
            bs = (block.get('start') or '').strip()
            be = (block.get('end') or '').strip()
            if bs and be:
                ShiftTemplateBlock.objects.create(
                    shift_template=tmpl, block_number=n, start_time=bs, end_time=be
                )
        log_action(request.user, 'Quick-edit shift (permanent)',
                   f'{agent} {day_date.strftime("%A")}: {"OFF" if is_off else f"{start}–{end}"}',
                   agent=agent)
    else:
        defaults = {
            'is_off': is_off,
            'start_time': start if start else '00:00',
            'end_time': end if end else '00:00',
        }
        shift_obj, _ = Shift.objects.update_or_create(agent=agent, date=day_date, defaults=defaults)
        ShiftBlock.objects.filter(shift=shift_obj).delete()
        for n, block in enumerate(extra_blocks[:2], start=2):
            bs = (block.get('start') or '').strip()
            be = (block.get('end') or '').strip()
            if bs and be:
                ShiftBlock.objects.create(
                    shift=shift_obj, block_number=n, start_time=bs, end_time=be
                )
        log_action(request.user, 'Quick-edit shift (one-time)',
                   f'{agent} on {day_date.isoformat()}: {"OFF" if is_off else f"{start}–{end}"}',
                   agent=agent)

    return JsonResponse({'ok': True, 'scheduled': '' if is_off else f'{start}–{end}'})


@login_required
def overtime_export(request):
    import csv
    from django.http import HttpResponse

    date_from_str = request.GET.get('date_from', '')
    date_to_str = request.GET.get('date_to', '')
    supervisor_id = request.GET.get('supervisor', '')

    try:
        date_from = date.fromisoformat(date_from_str)
    except (ValueError, TypeError):
        date_from = timezone.localdate() - timedelta(days=30)
    try:
        date_to = date.fromisoformat(date_to_str)
    except (ValueError, TypeError):
        date_to = timezone.localdate()

    ot_qs = OvertimeShift.objects.filter(
        date__range=[date_from, date_to]
    ).select_related('agent__user', 'agent__supervisor__user', 'verification').order_by('date', 'agent__user__last_name')

    if supervisor_id:
        try:
            ot_qs = ot_qs.filter(agent__supervisor_id=int(supervisor_id))
        except (ValueError, TypeError):
            pass

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="ot_payroll_{date_from}_{date_to}.csv"'
    writer = csv.writer(response)
    writer.writerow([
        'Agent Name', 'Employee ID', 'Employer', 'Supervisor',
        'Week Start', 'Date', 'Day of Week',
        'Start Time', 'End Time', 'Total Hours',
        'Incentive Type', 'Incentivized Hours', 'Base Hourly Rate ($)',
        'Base Pay ($)', 'Incentive Bonus ($)', 'Total Pay Offered ($)',
        'Status', 'Cancellation Reason', 'Coverage %',
    ])
    for ot in ot_qs:
        from decimal import Decimal
        is_cancelled = ot.status == 'cancelled'
        total_hrs = Decimal('0') if is_cancelled else ot.total_shift_hours()
        base_rate = ot.base_hourly_rate or Decimal('0')
        inc_hrs = ot.incentivized_hours or Decimal('0')
        if is_cancelled:
            base_pay = incentive_bonus = total_offered = '0.00'
        else:
            if ot.incentive_type == 'time_and_a_half':
                premium = Decimal('0.5')
            elif ot.incentive_type == 'power_hour':
                premium = Decimal('1.0')
            else:
                premium = Decimal('0')
            base_pay = (total_hrs * base_rate).quantize(Decimal('0.01')) if base_rate else ''
            incentive_bonus = (inc_hrs * base_rate * premium).quantize(Decimal('0.01')) if base_rate else ''
            total_offered = ot.incentive_offered() or ''
        week_start = ot.date - timedelta(days=ot.date.weekday())
        writer.writerow([
            str(ot.agent),
            ot.agent.employee_id or '',
            ot.agent.employer,
            str(ot.agent.supervisor) if ot.agent.supervisor else '',
            week_start.strftime('%Y-%m-%d'),
            ot.date.strftime('%Y-%m-%d'),
            ot.date.strftime('%A'),
            ot.start_time.strftime('%H:%M'),
            ot.end_time.strftime('%H:%M'),
            str(total_hrs),
            ot.get_incentive_type_display(),
            str(inc_hrs) if ot.incentivized_hours is not None else '',
            str(ot.base_hourly_rate) if ot.base_hourly_rate is not None else '',
            str(base_pay),
            str(incentive_bonus),
            str(total_offered),
            ot.get_status_display(),
            ot.cancellation_reason,
            str(ot.verification.coverage_pct) + '%' if hasattr(ot, 'verification') and ot.verification and ot.verification.coverage_pct is not None else '',
        ])
    return response


@login_required
def overtime_set_status(request, pk):
    from django.http import JsonResponse
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    import json as _json
    try:
        body = _json.loads(request.body)
        new_status = body.get('status', '')
        cancellation_reason = body.get('cancellation_reason', '').strip()
    except (ValueError, KeyError):
        new_status = ''
        cancellation_reason = ''
    valid = {'pending', 'completed', 'no_show', 'cancelled'}
    if new_status not in valid:
        return JsonResponse({'error': 'Invalid status'}, status=400)
    if new_status == 'cancelled' and not cancellation_reason:
        return JsonResponse({'error': 'Cancellation reason is required.'}, status=400)
    ot_shift = get_object_or_404(OvertimeShift, pk=pk)
    ot_shift.status = new_status
    update_fields = ['status']
    if new_status == 'cancelled':
        ot_shift.cancellation_reason = cancellation_reason
        update_fields.append('cancellation_reason')
    elif ot_shift.cancellation_reason:
        ot_shift.cancellation_reason = ''
        update_fields.append('cancellation_reason')
    ot_shift.save(update_fields=update_fields)
    log_action(request.user, 'Updated OT status',
               f'{ot_shift.agent} on {ot_shift.date}: status={new_status}'
               + (f' (reason: {cancellation_reason})' if cancellation_reason else ''),
               agent=ot_shift.agent)
    return JsonResponse({'status': new_status, 'pk': pk, 'cancellation_reason': ot_shift.cancellation_reason})


@login_required
def live_poll(request):
    """Generic poll endpoint — returns the latest change timestamp for a given page type."""
    from django.db.models import Max
    from .models import AuditLog

    poll_type = request.GET.get('type', '')
    week_start_str = request.GET.get('week_start', '')

    try:
        ws = date.fromisoformat(week_start_str)
        ws -= timedelta(days=ws.weekday())
        week_end = ws + timedelta(days=6)
    except (ValueError, TypeError):
        ws = week_end = None

    latest = None

    if poll_type == 'codings' and ws:
        from adherence.models import Coding
        r = Coding.objects.filter(date__range=[ws, week_end]).aggregate(latest=Max('created_at'))
        latest = r['latest']
    elif poll_type == 'shifts':
        r = AuditLog.objects.filter(action__icontains='schedule').aggregate(latest=Max('timestamp'))
        latest = r['latest']
    elif poll_type == 'users':
        r = AuditLog.objects.filter(action__icontains='agent profile').aggregate(latest=Max('timestamp'))
        latest = r['latest']
    elif poll_type == 'daily' and ws:
        from adherence.models import DailyUpload
        dates = [ws + timedelta(days=i) for i in range(7)]
        r = DailyUpload.objects.filter(date__in=dates).aggregate(latest=Max('uploaded_at'))
        latest = r['latest']
    elif poll_type == 'overtime':
        r = AuditLog.objects.filter(action__icontains='OT shift').aggregate(latest=Max('timestamp'))
        latest = r['latest']

    from django.http import JsonResponse
    return JsonResponse({'latest': latest.isoformat() if latest else None})


@login_required
def activity_log(request):
    from .models import AuditLog
    from django.contrib.auth.models import User

    logs = AuditLog.objects.select_related('user', 'agent__user').order_by('-timestamp')

    # Filter by user
    user_filter = request.GET.get('user', '')
    if user_filter:
        try:
            logs = logs.filter(user_id=int(user_filter))
        except (ValueError, TypeError):
            pass

    # Filter by date
    date_filter = request.GET.get('date', '')
    if date_filter:
        try:
            from datetime import datetime
            filter_date = date.fromisoformat(date_filter)
            logs = logs.filter(timestamp__date=filter_date)
        except (ValueError, TypeError):
            pass

    logs = logs[:500]

    users = User.objects.filter(audit_logs__isnull=False).distinct().order_by('last_name', 'first_name')

    return render(request, 'scheduling/activity_log.html', {
        'logs': logs,
        'users': users,
        'selected_user': user_filter,
        'selected_date': date_filter,
    })


@login_required
def agent_history(request, pk):
    from datetime import date as date_cls, timedelta
    from decimal import Decimal
    from adherence.models import AdherenceRecord, Coding

    agent = get_object_or_404(Agent, pk=pk)
    today = date_cls.today()
    five_years_ago = today.replace(year=today.year - 5)

    date_from_str = request.GET.get('from', '')
    date_to_str = request.GET.get('to', '')
    try:
        date_from = date_cls.fromisoformat(date_from_str)
    except (ValueError, TypeError):
        date_from = today - timedelta(days=30)
    try:
        date_to = date_cls.fromisoformat(date_to_str)
    except (ValueError, TypeError):
        date_to = today
    date_from = max(date_from, five_years_ago)
    date_to = min(date_to, today)

    # Role history
    role_history = list(agent.role_history.select_related('supervisor__user', 'changed_by').all())

    # Seed initial entry if none exists (for legacy agents)
    if not role_history:
        RoleHistory.objects.create(
            agent=agent, role=agent.role, role_type=agent.role_type or '',
            supervisor=agent.supervisor, employer=agent.employer,
            billing_status=agent.billing_status,
            effective_from=agent.start_date or today,
            changed_by=None,
        )
        role_history = list(agent.role_history.select_related('supervisor__user', 'changed_by').all())

    # Schedule history: ShiftTemplates grouped by effective_from
    from collections import defaultdict, OrderedDict
    templates = list(ShiftTemplate.objects.filter(agent=agent).order_by('-effective_from', 'day_of_week'))
    sched_groups = OrderedDict()
    DAYS_ABBR = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    for t in templates:
        key = t.effective_from or date_cls(2000, 1, 1)
        if key not in sched_groups:
            sched_groups[key] = []
        sched_groups[key].append(t)
    schedule_history = list(sched_groups.items())

    # Attendance & hours by week
    records = list(AdherenceRecord.objects.filter(
        agent=agent, date__gte=date_from, date__lte=date_to
    ).order_by('date'))
    codings_qs = list(Coding.objects.filter(
        agent=agent, date__gte=date_from, date__lte=date_to
    ).order_by('-date', 'start_time'))

    record_map = {r.date: r for r in records}
    coding_map = defaultdict(Decimal)
    for c in codings_qs:
        coding_map[c.date] += Decimal(str(c.total_hours()))

    # Build week rows (most recent first)
    ws = date_from - timedelta(days=date_from.weekday())
    attendance_weeks = []
    while ws <= date_to:
        week_dates = [ws + timedelta(days=i) for i in range(7)]
        days = []
        login_total = Decimal('0')
        coded_total = Decimal('0')
        has_data = False
        bonus = True
        bonus_det = False
        BONUS_Q = {'P', 'OT', 'MUT', 'VTO', 'P+VTO'}
        BONUS_DQ = {'Absent', 'NCNS', 'T', 'T+VTO', 'I', 'LOA', 'S'}
        for d in week_dates:
            r = record_map.get(d)
            c_hrs = coding_map.get(d, Decimal('0'))
            status = r.status if r else ''
            hrs = r.actual_hours if r else None
            if r or c_hrs:
                has_data = True
            if hrs:
                login_total += hrs
            coded_total += c_hrs
            if status in BONUS_Q:
                bonus_det = True
            elif status in BONUS_DQ:
                bonus = False
                bonus_det = True
            elif status:
                bonus = False
                bonus_det = True
            days.append({'date': d, 'status': status, 'hours': hrs, 'coded': c_hrs})
        if has_data:
            total = login_total + coded_total
            attendance_weeks.append({
                'week_start': ws,
                'days': days,
                'login_hrs': login_total,
                'coded_hrs': coded_total,
                'total_hrs': total,
                'bonus': 'Yes' if (bonus and bonus_det) else ('No' if not bonus else '—'),
            })
        ws += timedelta(days=7)
    attendance_weeks.reverse()

    # OT history
    ot_shifts = list(OvertimeShift.objects.filter(agent=agent).order_by('-date')[:500])

    # Export CSV
    if request.GET.get('export') == 'attendance':
        import csv
        from django.http import HttpResponse
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="attendance_{agent.pk}_{date_from}_{date_to}.csv"'
        w = csv.writer(resp)
        w.writerow(['Week Start', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun', 'Login Hrs', 'Coded Hrs', 'Total Hrs', 'Bonus'])
        for wk in attendance_weeks:
            w.writerow([
                wk['week_start'],
                *[d['status'] for d in wk['days']],
                str(wk['login_hrs']), str(wk['coded_hrs']), str(wk['total_hrs']),
                wk['bonus'],
            ])
        return resp

    if request.GET.get('export') == 'codings':
        import csv
        from django.http import HttpResponse
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="codings_{agent.pk}_{date_from}_{date_to}.csv"'
        w = csv.writer(resp)
        w.writerow(['Date', 'Start', 'End', 'Duration', 'Notes'])
        for c in codings_qs:
            w.writerow([c.date, c.start_time.strftime('%H:%M'), c.end_time.strftime('%H:%M'), c.total_hhmmss(), c.notes])
        return resp

    if request.GET.get('export') == 'ot':
        import csv
        from django.http import HttpResponse
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="ot_{agent.pk}.csv"'
        w = csv.writer(resp)
        w.writerow(['Date', 'Start', 'End', 'Hours', 'Incentive', 'Completed'])
        for s in ot_shifts:
            w.writerow([s.date, s.start_time.strftime('%H:%M'), s.end_time.strftime('%H:%M'), str(s.total_shift_hours()), s.get_incentive_type_display(), s.get_status_display()])
        return resp

    return render(request, 'scheduling/agent_history.html', {
        'agent': agent,
        'role_history': role_history,
        'schedule_history': schedule_history,
        'attendance_weeks': attendance_weeks,
        'codings': codings_qs,
        'ot_shifts': ot_shifts,
        'date_from': date_from,
        'date_to': date_to,
        'five_years_ago': five_years_ago,
        'today': today,
        'days_abbr': DAYS_ABBR,
    })


# ── Floor-wide Records ────────────────────────────────────────────────────────

@login_required
def records_attendance(request):
    from datetime import date as date_cls, timedelta
    from adherence.models import AdherenceRecord

    today = date_cls.today()
    five_years_ago = today.replace(year=today.year - 5)

    date_from = _parse_date(request.GET.get('from'), today - timedelta(days=30))
    date_to   = _parse_date(request.GET.get('to'), today)
    date_from = max(date_from, five_years_ago)
    date_to   = min(date_to, today)

    supervisor_id = request.GET.get('supervisor', '')
    role_type_f   = request.GET.get('role_type', '')
    employer_f    = request.GET.get('employer', '')
    status_f      = request.GET.get('status', '')

    supervisors = Agent.objects.filter(
        role_type__in=('supervisor', 'coordinator'), status='active'
    ).select_related('user').order_by('user__last_name', 'user__first_name')

    qs = AdherenceRecord.objects.filter(
        date__gte=date_from, date__lte=date_to,
        agent__track_attendance=True,
    ).select_related('agent__user', 'agent__supervisor__user').order_by('-date', 'agent__user__last_name')

    if supervisor_id:
        try:
            qs = qs.filter(agent__supervisor_id=int(supervisor_id))
        except (ValueError, TypeError):
            pass
    if role_type_f:
        qs = qs.filter(agent__role_type=role_type_f)
    if employer_f:
        qs = qs.filter(agent__employer=employer_f)
    if status_f:
        qs = qs.filter(status=status_f)

    records = list(qs[:2000])  # cap for performance

    if request.GET.get('export') == '1':
        import csv as _csv
        from django.http import HttpResponse
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="attendance_records_{date_from}_{date_to}.csv"'
        w = _csv.writer(resp)
        w.writerow(['Date', 'Day', 'Agent', 'Supervisor', 'Role Type', 'Employer', 'Status', 'Hours'])
        for r in records:
            w.writerow([
                r.date, r.date.strftime('%A'),
                str(r.agent), str(r.agent.supervisor or ''),
                r.agent.get_role_type_display(), r.agent.employer,
                r.status, str(r.actual_hours or ''),
            ])
        return resp

    from adherence.models import AdherenceRecord as AR
    status_choices = AR.STATUS_CHOICES
    role_type_choices = Agent.ROLE_TYPE_CHOICES
    employer_choices = Agent.EMPLOYER_CHOICES

    return render(request, 'records/attendance.html', {
        'records': records,
        'date_from': date_from,
        'date_to': date_to,
        'five_years_ago': five_years_ago,
        'today': today,
        'supervisors': supervisors,
        'selected_supervisor': supervisor_id,
        'selected_role_type': role_type_f,
        'selected_employer': employer_f,
        'selected_status': status_f,
        'status_choices': status_choices,
        'role_type_choices': role_type_choices,
        'employer_choices': employer_choices,
        'count': len(records),
    })


@login_required
def records_hours(request):
    from datetime import date as date_cls, timedelta
    from decimal import Decimal
    from collections import defaultdict
    from adherence.models import AdherenceRecord, Coding, DailyAgentHours

    today = date_cls.today()
    five_years_ago = today.replace(year=today.year - 5)

    date_from = _parse_date(request.GET.get('from'), today - timedelta(days=30))
    date_to   = _parse_date(request.GET.get('to'), today)
    date_from = max(date_from, five_years_ago)
    date_to   = min(date_to, today)

    supervisor_id = request.GET.get('supervisor', '')
    role_type_f   = request.GET.get('role_type', '')
    employer_f    = request.GET.get('employer', '')
    billing_f     = request.GET.get('billing', '')

    supervisors = Agent.objects.filter(
        role_type__in=('supervisor', 'coordinator'), status='active'
    ).select_related('user').order_by('user__last_name', 'user__first_name')

    agents_qs = Agent.objects.filter(track_attendance=True).select_related('user', 'supervisor__user')
    if supervisor_id:
        try:
            agents_qs = agents_qs.filter(supervisor_id=int(supervisor_id))
        except (ValueError, TypeError):
            pass
    if role_type_f:
        agents_qs = agents_qs.filter(role_type=role_type_f)
    if employer_f:
        agents_qs = agents_qs.filter(employer=employer_f)
    if billing_f:
        agents_qs = agents_qs.filter(billing_status=billing_f)

    agents = list(agents_qs.order_by('user__last_name', 'user__first_name'))
    agent_ids = [a.pk for a in agents]

    # Get all dates in range
    all_dates = []
    d = date_from
    while d <= date_to:
        all_dates.append(d)
        d += timedelta(days=1)

    adh_qs = AdherenceRecord.objects.filter(agent_id__in=agent_ids, date__gte=date_from, date__lte=date_to)
    cod_qs = Coding.objects.filter(agent_id__in=agent_ids, date__gte=date_from, date__lte=date_to)

    # Map (agent_id, date) -> hours
    adh_map = {}
    for r in adh_qs:
        adh_map[(r.agent_id, r.date)] = r.actual_hours or Decimal('0')

    cod_map = defaultdict(Decimal)
    for c in cod_qs:
        cod_map[(c.agent_id, c.date)] += Decimal(str(c.total_hours()))

    # Group by agent + week
    def week_start(d):
        return d - timedelta(days=d.weekday())

    rows = []
    for agent in agents:
        # Get all weeks
        weeks_seen = set()
        for d in all_dates:
            ws = week_start(d)
            if (agent.pk, ws) not in weeks_seen:
                weeks_seen.add((agent.pk, ws))
                week_dates = [ws + timedelta(days=i) for i in range(7)]
                login_hrs = sum((adh_map.get((agent.pk, wd), Decimal('0')) for wd in week_dates), Decimal('0'))
                coded_hrs = sum((cod_map.get((agent.pk, wd), Decimal('0')) for wd in week_dates), Decimal('0'))
                total_hrs = login_hrs + coded_hrs
                if login_hrs > 0 or coded_hrs > 0:
                    rows.append({
                        'agent': agent,
                        'week_start': ws,
                        'login_hrs': login_hrs,
                        'coded_hrs': coded_hrs,
                        'total_hrs': total_hrs,
                    })

    rows.sort(key=lambda r: (-r['week_start'].toordinal(), r['agent'].user.last_name))

    if request.GET.get('export') == '1':
        import csv as _csv
        from django.http import HttpResponse
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="hours_records_{date_from}_{date_to}.csv"'
        w = _csv.writer(resp)
        w.writerow(['Week Start', 'Agent', 'Supervisor', 'Role Type', 'Employer', 'Login Hrs', 'Coded Hrs', 'Total Hrs'])
        for r in rows:
            w.writerow([
                r['week_start'], str(r['agent']),
                str(r['agent'].supervisor or ''),
                r['agent'].get_role_type_display(), r['agent'].employer,
                str(r['login_hrs']), str(r['coded_hrs']), str(r['total_hrs']),
            ])
        return resp

    return render(request, 'records/hours.html', {
        'rows': rows,
        'date_from': date_from,
        'date_to': date_to,
        'five_years_ago': five_years_ago,
        'today': today,
        'supervisors': supervisors,
        'selected_supervisor': supervisor_id,
        'selected_role_type': role_type_f,
        'selected_employer': employer_f,
        'selected_billing': billing_f,
        'role_type_choices': Agent.ROLE_TYPE_CHOICES,
        'employer_choices': Agent.EMPLOYER_CHOICES,
        'billing_choices': Agent.BILLING_STATUS_CHOICES,
        'count': len(rows),
    })


@login_required
def records_role_log(request):
    from datetime import date as date_cls, timedelta

    today = date_cls.today()
    five_years_ago = today.replace(year=today.year - 5)

    date_from = _parse_date(request.GET.get('from'), today - timedelta(days=30))
    date_to   = _parse_date(request.GET.get('to'), today)
    date_from = max(date_from, five_years_ago)
    date_to   = min(date_to, today)

    supervisor_id = request.GET.get('supervisor', '')

    supervisors = Agent.objects.filter(
        role_type__in=('supervisor', 'coordinator'), status='active'
    ).select_related('user').order_by('user__last_name', 'user__first_name')

    qs = RoleHistory.objects.filter(
        effective_from__gte=date_from, effective_from__lte=date_to,
    ).select_related('agent__user', 'agent__supervisor__user', 'supervisor__user', 'changed_by').order_by('-changed_at')

    if supervisor_id:
        try:
            qs = qs.filter(agent__supervisor_id=int(supervisor_id))
        except (ValueError, TypeError):
            pass

    entries = list(qs[:1000])

    if request.GET.get('export') == '1':
        import csv as _csv
        from django.http import HttpResponse
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="role_log_{date_from}_{date_to}.csv"'
        w = _csv.writer(resp)
        w.writerow(['Date', 'Agent', 'Supervisor', 'Role', 'Role Type', 'Employer', 'Effective From', 'Effective To', 'Changed By'])
        for e in entries:
            w.writerow([
                e.changed_at.strftime('%Y-%m-%d'),
                str(e.agent), str(e.agent.supervisor or ''),
                e.role, e.role_type, e.employer,
                str(e.effective_from), str(e.effective_to or ''),
                e.changed_by.get_full_name() if e.changed_by else '',
            ])
        return resp

    return render(request, 'records/role_log.html', {
        'entries': entries,
        'date_from': date_from,
        'date_to': date_to,
        'five_years_ago': five_years_ago,
        'today': today,
        'supervisors': supervisors,
        'selected_supervisor': supervisor_id,
        'count': len(entries),
    })


def _parse_date(val, default):
    from datetime import date as date_cls
    try:
        return date_cls.fromisoformat(val) if val else default
    except (ValueError, TypeError):
        return default


# ── Scheduled Role Changes ────────────────────────────────────────────────────

@login_required
def schedule_role_change(request, pk):
    from django.views.decorators.http import require_POST as _rp
    if request.method != 'POST':
        return redirect('agent_detail', pk=pk)

    agent = get_object_or_404(Agent, pk=pk)

    # Only one pending change at a time
    if agent.scheduled_role_changes.filter(applied_at__isnull=True, cancelled_at__isnull=True).exists():
        messages.error(request, "This agent already has a pending role change. Cancel it before scheduling a new one.")
        return redirect('agent_detail', pk=pk)

    new_role_type = request.POST.get('new_role_type', '').strip()
    effective_date_str = request.POST.get('effective_date', '').strip()

    valid_role_types = {k for k, _ in Agent.ROLE_TYPE_CHOICES}
    if new_role_type not in valid_role_types:
        messages.error(request, "Invalid role type selected.")
        return redirect('agent_detail', pk=pk)

    try:
        effective_date = date.fromisoformat(effective_date_str)
    except (ValueError, TypeError):
        messages.error(request, "Invalid effective date.")
        return redirect('agent_detail', pk=pk)

    today = timezone.localdate()
    if effective_date < today:
        messages.error(request, "Effective date cannot be in the past.")
        return redirect('agent_detail', pk=pk)

    # Optional new schedule
    days_raw = request.POST.getlist('new_shift_days')
    start_raw = request.POST.get('new_shift_start_time', '').strip()
    end_raw = request.POST.get('new_shift_end_time', '').strip()

    new_shift_days = None
    new_shift_start_time = None
    new_shift_end_time = None

    if days_raw and start_raw and end_raw:
        try:
            from datetime import time as time_cls
            new_shift_days = sorted(int(d) for d in days_raw)
            new_shift_start_time = time_cls.fromisoformat(start_raw)
            new_shift_end_time = time_cls.fromisoformat(end_raw)
        except (ValueError, TypeError):
            new_shift_days = new_shift_start_time = new_shift_end_time = None

    new_supervisor = None
    supervisor_id_raw = request.POST.get('new_supervisor_id', '').strip()
    if supervisor_id_raw:
        try:
            new_supervisor = Agent.objects.get(pk=int(supervisor_id_raw))
        except (Agent.DoesNotExist, ValueError, TypeError):
            pass

    src = ScheduledRoleChange.objects.create(
        agent=agent,
        new_role_type=new_role_type,
        effective_date=effective_date,
        new_shift_days=new_shift_days,
        new_shift_start_time=new_shift_start_time,
        new_shift_end_time=new_shift_end_time,
        new_supervisor=new_supervisor,
        scheduled_by=request.user,
    )
    _sync_pending_schedule(src)

    log_action(
        request.user,
        'Scheduled role change',
        f'Scheduled change to {src.get_new_role_type_display()} effective {effective_date}',
        agent=agent,
    )
    messages.success(request, f"Role change to {src.get_new_role_type_display()} scheduled for {effective_date.strftime('%b %d, %Y')}.")
    return redirect('agent_detail', pk=pk)


@login_required
def cancel_role_change(request, pk):
    if request.method != 'POST':
        return redirect('agent_list')

    src = get_object_or_404(ScheduledRoleChange, pk=pk)
    if not src.is_pending:
        messages.error(request, "This role change has already been applied or cancelled.")
        return redirect('agent_detail', pk=src.agent_id)

    # Undo pre-created templates from _sync_pending_schedule
    if src.new_shift_days:
        ShiftTemplate.objects.filter(agent=src.agent, effective_from=src.effective_date).delete()
        ShiftTemplate.objects.filter(
            agent=src.agent, effective_until=src.effective_date
        ).update(effective_until=None)

    # Restore supervisor if it was pre-applied — open RoleHistory still holds the original
    if src.new_supervisor_id:
        open_rh = src.agent.role_history.filter(effective_to__isnull=True).first()
        if open_rh is not None:
            src.agent.supervisor = open_rh.supervisor
            src.agent.save(update_fields=['supervisor'])

    src.cancelled_at = timezone.now()
    src.cancelled_by = request.user
    src.save(update_fields=['cancelled_at', 'cancelled_by'])

    log_action(
        request.user,
        'Cancelled scheduled role change',
        f'Cancelled planned change to {src.get_new_role_type_display()} (was scheduled for {src.effective_date})',
        agent=src.agent,
    )
    messages.success(request, "Scheduled role change cancelled.")
    return redirect('agent_detail', pk=src.agent_id)
