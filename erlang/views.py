import math
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from .calculator import agents_required, service_level, occupancy
from .models import ErlangReport


@login_required
def erlang_calculator(request):
    result = None
    if request.method == 'POST':
        calls = float(request.POST.get('calls_per_hour', 0))
        aht = float(request.POST.get('avg_handle_time', 0))
        target_sl = float(request.POST.get('target_service_level', 80))
        target_time = int(request.POST.get('target_answer_time', 20))
        shrinkage = float(request.POST.get('shrinkage', 0))
        name = request.POST.get('name', 'Unnamed Report')

        agents = agents_required(calls, aht, target_sl, target_time)
        sl = service_level(agents, calls, aht, target_time)
        occ = occupancy(agents, calls, aht)

        if shrinkage > 0 and shrinkage < 100:
            agents_sched = math.ceil(agents / (1 - shrinkage / 100))
        else:
            agents_sched = agents

        report = ErlangReport.objects.create(
            name=name,
            calls_per_hour=calls,
            avg_handle_time=aht,
            target_service_level=target_sl,
            target_answer_time=target_time,
            shrinkage=shrinkage,
            agents_required=agents,
            agents_scheduled=agents_sched,
            service_level_achieved=sl,
            occupancy=occ,
        )
        result = {
            'agents': agents,
            'agents_scheduled': agents_sched,
            'shrinkage': shrinkage,
            'service_level': sl,
            'occupancy': occ,
            'report': report,
        }

    return render(request, 'erlang/calculator.html', {'result': result})


@login_required
def erlang_reports(request):
    reports = ErlangReport.objects.all()
    return render(request, 'erlang/reports.html', {'reports': reports})
