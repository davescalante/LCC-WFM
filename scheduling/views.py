from datetime import date, timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.urls import reverse
from django.utils import timezone
from .models import Agent, Shift, Break, EmploymentPeriod, Five9Profile
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
        messages.success(request, f"User {user.get_full_name()} created successfully.")
        return redirect('agent_list')
    return render(request, 'scheduling/agent_form.html', {
        'user_form': user_form,
        'agent_form': agent_form,
        'title': 'Add User',
        'five9_profiles': [],
        'role_type_choices': Agent.ROLE_TYPE_CHOICES,
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
            agent_form.save()

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
    })


@login_required
def agent_delete(request, pk):
    agent = get_object_or_404(Agent, pk=pk)
    if request.method == 'POST':
        name = agent.user.get_full_name()
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

    agents = Agent.objects.filter(status='active').select_related('user').order_by('user__last_name', 'user__first_name')

    prev_week_start = week_start - timedelta(days=7)
    prev_week_dates = [prev_week_start + timedelta(days=i) for i in range(7)]
    has_prev_week = Shift.objects.filter(date__in=prev_week_dates).exists()
    has_this_week = Shift.objects.filter(date__in=week_dates).exists()

    shifts_qs = Shift.objects.filter(
        date__in=week_dates, agent__in=agents
    ).select_related('agent__user')
    shift_map = {(s.agent_id, s.date): s for s in shifts_qs}

    # Per-agent: which agents have data in the previous week / this week
    prev_week_agent_ids = set(
        Shift.objects.filter(date__in=prev_week_dates)
        .values_list('agent_id', flat=True)
    )
    this_week_agent_ids = set(s.agent_id for s in shifts_qs)

    rows = []
    for agent in agents:
        cells = []
        for day_date in week_dates:
            shift = shift_map.get((agent.pk, day_date))
            cells.append({'date': day_date, 'shift': shift})
        rows.append({
            'agent': agent,
            'cells': cells,
            'has_prev_week_shifts': agent.pk in prev_week_agent_ids,
            'has_this_week_shifts': agent.pk in this_week_agent_ids,
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

    # Default week start to current Monday
    today = timezone.localdate()
    default_week_start = today - timedelta(days=today.weekday())

    selected_agent_id = request.GET.get('agent') or request.POST.get('agent')
    week_start_str = request.GET.get('week_start') or request.POST.get('week_start')

    try:
        week_start = date.fromisoformat(week_start_str) if week_start_str else default_week_start
        # Snap to Monday of the selected week
        week_start = week_start - timedelta(days=week_start.weekday())
    except ValueError:
        week_start = default_week_start

    week_dates = [week_start + timedelta(days=i) for i in range(7)]

    # Load existing shifts for pre-filling
    existing = {}
    if selected_agent_id:
        for shift in Shift.objects.filter(agent_id=selected_agent_id, date__in=week_dates):
            existing[shift.date] = shift

    if request.method == 'POST' and selected_agent_id:
        agent = get_object_or_404(Agent, pk=selected_agent_id)
        for i, day_date in enumerate(week_dates):
            is_off = request.POST.get(f'day_{i}_off') == 'on'
            start = request.POST.get(f'day_{i}_start')
            end = request.POST.get(f'day_{i}_end')
            notes = request.POST.get(f'day_{i}_notes', '')

            if is_off or (start and end):
                Shift.objects.update_or_create(
                    agent=agent,
                    date=day_date,
                    defaults={
                        'start_time': start or '09:00',
                        'end_time': end or '17:00',
                        'is_off': is_off,
                        'notes': notes,
                    }
                )
        messages.success(request, f"Schedule saved for week of {week_start.strftime('%B %d, %Y')}.")
        return redirect(f"{reverse('shift_list')}?week_start={week_start.isoformat()}")

    # Build day context with existing data pre-filled
    days = []
    for i, day_date in enumerate(week_dates):
        shift = existing.get(day_date)
        days.append({
            'index': i,
            'name': DAYS[i],
            'date': day_date,
            'start': shift.start_time.strftime('%H:%M') if shift and not shift.is_off else '',
            'end': shift.end_time.strftime('%H:%M') if shift and not shift.is_off else '',
            'is_off': shift.is_off if shift else False,
            'notes': shift.notes if shift else '',
        })

    return render(request, 'scheduling/shift_week.html', {
        'agents': agents,
        'selected_agent_id': int(selected_agent_id) if selected_agent_id else None,
        'week_start': week_start,
        'week_end': week_dates[-1],
        'days': days,
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
