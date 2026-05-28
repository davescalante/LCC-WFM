from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from scheduling.models import Shift, Agent
from .models import AdherenceRecord


@login_required
def adherence_dashboard(request):
    today = timezone.localdate()
    records = AdherenceRecord.objects.filter(
        shift__date=today
    ).select_related('shift__agent__user').order_by('-timestamp')
    shifts_today = Shift.objects.filter(date=today).select_related('agent__user')
    return render(request, 'adherence/dashboard.html', {
        'records': records,
        'shifts_today': shifts_today,
        'today': today,
    })


@login_required
def log_adherence(request):
    if request.method == 'POST':
        shift_id = request.POST.get('shift_id')
        shift = get_object_or_404(Shift, pk=shift_id)
        AdherenceRecord.objects.create(
            shift=shift,
            status=request.POST.get('status'),
            actual_start=request.POST.get('actual_start') or None,
            actual_end=request.POST.get('actual_end') or None,
            notes=request.POST.get('notes', ''),
        )
    today = timezone.localdate()
    shifts = Shift.objects.filter(date=today).select_related('agent__user')
    return render(request, 'adherence/log.html', {'shifts': shifts})
