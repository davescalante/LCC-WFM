from django.core.cache import cache
from django.utils import timezone


class ApplyScheduledRoleChangesMiddleware:
    """Run apply_due_role_changes() once per calendar day on the first request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        today = timezone.localdate().isoformat()
        key = f'role_changes_applied_{today}'
        if not cache.get(key):
            try:
                from .views import apply_due_role_changes
                apply_due_role_changes()
            except Exception:
                pass  # never block a request over a background job
            cache.set(key, True, 86400)
        return self.get_response(request)
