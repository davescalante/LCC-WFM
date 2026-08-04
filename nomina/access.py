"""Access control for the Nómina section — super admins only (mirrors Finance)."""
from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect


def has_nomina_access(user):
    if user.is_superuser:
        return True
    try:
        return user.agent.is_super_admin
    except Exception:
        return False


def nomina_access_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        if not has_nomina_access(request.user):
            messages.error(request, "Access denied.")
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return _wrapped


def has_loan_access(user):
    """Loans module: super admins OR users granted `can_manage_loans`."""
    if has_nomina_access(user):
        return True
    try:
        return user.agent.can_manage_loans
    except Exception:
        return False


def loan_access_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        if not has_loan_access(request.user):
            messages.error(request, "Access denied.")
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return _wrapped
