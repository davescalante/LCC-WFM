from datetime import date, timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.urls import reverse
from django.utils import timezone
from .models import Agent, Shift, Break, EmploymentPeriod, Five9Profile, ShiftTemplate, OvertimeShift, log_action
from .forms import AgentUserForm, AgentForm, ShiftForm, BreakForm


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
        role_type='supervisor', status='active'
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
    shifts = Shift.objects.filter(agent=agent).order_by('-date')[:30]
    return render(request, 'scheduling/agent_detail.html', {'agent': agent, 'shifts': shifts})


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
            agent = agent_form.save()
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


@login_required
def shift_list(request):
    today = timezone.localdate()
    default_week_start = today - timedelta(days=today.weekday())

    week_start_str = request.GET.get('week_start')
    try:
        week_start = date.fromisoformat(week_start_str) if week_start_str else default_week_start
        week_start = week_start - timedelta(days=week_start.weekday())
    except ValueError:
        week_start = default_week_start

    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    week_end = week_dates[-1]

    supervisors = Agent.objects.filter(
        role_type='supervisor', status='active'
    ).select_related('user').order_by('user__last_name', 'user__first_name')

    if 'supervisor' in request.GET:
        supervisor_id = request.GET.get('supervisor', '')
        request.session['shift_supervisor_filter'] = supervisor_id
    else:
        supervisor_id = request.session.get('shift_supervisor_filter', '')

    agents = Agent.objects.filter(status='active').select_related('user').order_by('user__last_name', 'user__first_name')
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

    # Recurring templates (day-of-week defaults)
    templates_qs = ShiftTemplate.objects.filter(agent__in=agents)
    template_map = {(t.agent_id, t.day_of_week): t for t in templates_qs}

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
            template = template_map.get((agent.pk, day_date.weekday()))
            cells.append({
                'date': day_date,
                'shift': override,
                'template': template if not override else None,
            })
        rows.append({
            'agent': agent,
            'cells': cells,
            'has_prev_week_shifts': agent.pk in prev_week_agent_ids,
            'has_this_week_shifts': agent.pk in this_week_override_ids,
            'has_template': template_map.get((agent.pk, 0)) is not None
                or any((agent.pk, d) in template_map for d in range(7)),
        })

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
            partial_days = []
            for i, day_date, is_off, start, end, notes in _day_configs():
                if is_off or (start and end):
                    ShiftTemplate.objects.update_or_create(
                        agent=agent, day_of_week=i,
                        defaults={
                            'start_time': start or '09:00',
                            'end_time': end or '17:00',
                            'is_off': is_off,
                            'notes': notes,
                        }
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
                    Shift.objects.update_or_create(
                        agent=agent, date=day_date,
                        defaults={
                            'start_time': start or '09:00',
                            'end_time': end or '17:00',
                            'is_off': is_off,
                            'notes': notes,
                        }
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
                        Shift.objects.update_or_create(
                            agent=agent, date=target,
                            defaults={
                                'start_time': start or '09:00',
                                'end_time': end or '17:00',
                                'is_off': is_off,
                                'notes': notes,
                            }
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
            templates[t.day_of_week] = t

    days = []
    for i, day_date in enumerate(week_dates):
        override = overrides.get(day_date)
        tmpl = templates.get(i)
        src = override or tmpl
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
    shift = get_object_or_404(Shift, pk=pk)
    if request.method == 'POST':
        week_start = shift.date - timedelta(days=shift.date.weekday())
        shift.delete()
        messages.success(request, "Shift deleted.")
        return redirect(f"{reverse('shift_list')}?week_start={week_start.isoformat()}")
    return render(request, 'scheduling/confirm_delete.html', {
        'object': shift,
        'cancel_url': reverse('shift_list'),
    })


def _get_week_start(request):
    """Return Monday of the selected week from GET param or today."""
    today = timezone.localdate()
    default = today - timedelta(days=today.weekday())
    raw = request.GET.get('week_start')
    try:
        ws = date.fromisoformat(raw) if raw else default
        return ws - timedelta(days=ws.weekday())
    except (ValueError, TypeError):
        return default


def _get_supervisor_filter(request):
    """Returns (supervisor_id_str, supervisors_qs). Reads GET param saving to session."""
    supervisors = Agent.objects.filter(
        role_type='supervisor', status='active'
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

    ot_qs = OvertimeShift.objects.filter(date__in=week_dates, agent__in=agents)
    ot_map = {(s.agent_id, s.date): s for s in ot_qs}

    rows = []
    for agent in agents:
        cells = []
        for day_date in week_dates:
            ot_shift = ot_map.get((agent.pk, day_date))
            cells.append({'date': day_date, 'ot_shift': ot_shift})
        rows.append({'agent': agent, 'cells': cells})

    return render(request, 'scheduling/overtime_list.html', {
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
        agent = get_object_or_404(Agent, pk=selected_agent_id)
        for i, day_date in enumerate(week_dates):
            ot_start = request.POST.get(f'day_{i}_start', '').strip()
            ot_end = request.POST.get(f'day_{i}_end', '').strip()
            ot_notes = request.POST.get(f'day_{i}_notes', '').strip()
            date_iso = day_date.isoformat()

            if ot_start and ot_end:
                _, created = OvertimeShift.objects.update_or_create(
                    agent=agent,
                    date=day_date,
                    defaults={
                        'start_time': ot_start,
                        'end_time': ot_end,
                        'notes': ot_notes,
                    }
                )
                verb = 'Added' if created else 'Updated'
                log_action(request.user, f'{verb} OT shift',
                           f'{agent} on {date_iso}: {ot_start}–{ot_end}', agent=agent)
            else:
                deleted, _ = OvertimeShift.objects.filter(agent=agent, date=day_date).delete()
                if deleted:
                    log_action(request.user, 'Removed OT shift',
                               f'{agent} on {date_iso}', agent=agent)

        messages.success(request, f"OT shifts saved for {agent} — week of {week_start.strftime('%B %d, %Y')}.")
        return redirect(f"{reverse('overtime_list')}?week_start={week_start.isoformat()}")

    # GET: pre-fill from existing OT records for selected agent
    days = []
    if selected_agent_id:
        ot_map = {
            s.date: s
            for s in OvertimeShift.objects.filter(agent_id=selected_agent_id, date__in=week_dates)
        }
        for i, day_date in enumerate(week_dates):
            ot_shift = ot_map.get(day_date)
            days.append({
                'index': i,
                'name': DAYS[i],
                'date': day_date,
                'start': ot_shift.start_time.strftime('%H:%M') if ot_shift else '',
                'end': ot_shift.end_time.strftime('%H:%M') if ot_shift else '',
                'notes': ot_shift.notes if ot_shift else '',
                'has_ot': bool(ot_shift),
            })

    return render(request, 'scheduling/overtime_week.html', {
        'agents': agents,
        'selected_agent_id': int(selected_agent_id) if selected_agent_id else None,
        'days': days,
        'week_start': week_start,
        'week_end': week_dates[-1],
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
        'week_start_iso': week_start.isoformat(),
    })


@login_required
def overtime_delete(request, pk):
    ot_shift = get_object_or_404(OvertimeShift, pk=pk)
    if request.method == 'POST':
        week_start = ot_shift.date - timedelta(days=ot_shift.date.weekday())
        ot_shift.delete()
        messages.success(request, "OT shift deleted.")
        return redirect(f"{reverse('overtime_list')}?week_start={week_start.isoformat()}")
    return render(request, 'scheduling/confirm_delete.html', {
        'object': ot_shift,
        'cancel_url': reverse('overtime_list'),
    })


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
