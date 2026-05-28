from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from .models import Agent, Shift


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
def shift_list(request):
    shifts = Shift.objects.select_related('agent__user').order_by('-date', 'start_time')
    return render(request, 'scheduling/shift_list.html', {'shifts': shifts})
