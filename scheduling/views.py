from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.urls import reverse
from django.utils import timezone
from .models import Agent, Shift, Break
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
    agents = Agent.objects.select_related('user').order_by('user__last_name')
    return render(request, 'scheduling/agent_list.html', {'agents': agents})


@login_required
def agent_detail(request, pk):
    agent = get_object_or_404(Agent, pk=pk)
    shifts = Shift.objects.filter(agent=agent).order_by('-date')[:30]
    return render(request, 'scheduling/agent_detail.html', {'agent': agent, 'shifts': shifts})


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
        messages.success(request, f"User {user.get_full_name()} created successfully.")
        return redirect('agent_list')
    return render(request, 'scheduling/agent_form.html', {
        'user_form': user_form,
        'agent_form': agent_form,
        'title': 'Add User',
    })


@login_required
def agent_edit(request, pk):
    agent = get_object_or_404(Agent, pk=pk)
    user_form = AgentUserForm(request.POST or None, instance=agent.user)
    agent_form = AgentForm(request.POST or None, instance=agent)
    if request.method == 'POST' and user_form.is_valid() and agent_form.is_valid():
        user = user_form.save(commit=False)
        password = user_form.cleaned_data.get('password')
        if password:
            user.set_password(password)
        user.save()
        agent_form.save()
        messages.success(request, f"User {user.get_full_name()} updated successfully.")
        return redirect('agent_detail', pk=agent.pk)
    return render(request, 'scheduling/agent_form.html', {
        'user_form': user_form,
        'agent_form': agent_form,
        'title': 'Edit User',
        'agent': agent,
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
    shifts = Shift.objects.select_related('agent__user').order_by('-date', 'start_time')
    return render(request, 'scheduling/shift_list.html', {'shifts': shifts})


@login_required
def shift_create(request):
    form = ShiftForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, "Shift created successfully.")
        return redirect('shift_list')
    return render(request, 'scheduling/shift_form.html', {'form': form, 'title': 'Add Shift'})


@login_required
def shift_edit(request, pk):
    shift = get_object_or_404(Shift, pk=pk)
    form = ShiftForm(request.POST or None, instance=shift)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, "Shift updated successfully.")
        return redirect('shift_list')
    return render(request, 'scheduling/shift_form.html', {
        'form': form,
        'title': 'Edit Shift',
        'shift': shift,
    })


@login_required
def shift_delete(request, pk):
    shift = get_object_or_404(Shift, pk=pk)
    if request.method == 'POST':
        shift.delete()
        messages.success(request, "Shift deleted.")
        return redirect('shift_list')
    return render(request, 'scheduling/confirm_delete.html', {
        'object': shift,
        'cancel_url': reverse('shift_list'),
    })
