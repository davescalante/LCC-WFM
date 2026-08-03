from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from wfm.utils import get_week_start, parse_week_param
from .access import nomina_access_required


@login_required
@nomina_access_required
def dashboard(request):
    """Nómina landing page — super admin only.

    Foundation scaffold: a week picker and section placeholders. The weekly
    Agent/Admin payroll assembly, paste-imports, and modules (Loans, Break
    Abuse, Welcome Bonus, Vacation Tracker) are added in later phases. No
    payroll data is computed here yet.
    """
    raw = request.GET.get('week_start') or request.GET.get('week')
    week_start = parse_week_param(raw) or get_week_start()
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    return render(request, 'nomina/dashboard.html', {
        'week_start': week_start,
        'week_end': week_dates[-1],
        'prev_week': (week_start - timedelta(days=7)).isoformat(),
        'next_week': (week_start + timedelta(days=7)).isoformat(),
        'today': timezone.localdate(),
    })
