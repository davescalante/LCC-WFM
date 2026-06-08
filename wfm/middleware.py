from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect

INACTIVITY_TIMEOUT = 4 * 3600   # 4 hours
ABSOLUTE_TIMEOUT = 16 * 3600    # 16 hours

# URL path prefixes agents are allowed to access
_AGENT_ALLOWED = ('/agent/', '/adherence/my/', '/accounts/', '/static/', '/favicon')


class SessionTimeoutMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            from django.utils import timezone
            now = timezone.now().timestamp()

            last_activity = request.session.get('_last_activity')
            if last_activity and (now - last_activity) > INACTIVITY_TIMEOUT:
                return self._expire(request)

            login_time = request.session.get('_login_time')
            if login_time and (now - login_time) > ABSOLUTE_TIMEOUT:
                return self._expire(request)

            request.session['_last_activity'] = now

        return self.get_response(request)

    def _expire(self, request):
        next_url = request.get_full_path()
        logout(request)

        accept = request.headers.get('Accept', '')
        x_req = request.headers.get('X-Requested-With', '')
        is_ajax = (
            x_req == 'XMLHttpRequest'
            or 'application/json' in accept
            or getattr(request, 'content_type', '') == 'application/json'
        )

        if is_ajax:
            return JsonResponse({'expired': True}, status=401)

        return redirect(f'/accounts/login/?expired=1&next={next_url}')


class AgentAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.is_agent = False
        request.agent_request_badge = 0
        request.supervisor_request_badge = 0

        if request.user.is_authenticated:
            try:
                profile = request.user.agent
                if profile.role == 'agent':
                    request.is_agent = True
                    if not any(request.path.startswith(p) for p in _AGENT_ALLOWED):
                        return redirect('agent_my_shifts')
                    from scheduling.models import AgentRequest
                    request.agent_request_badge = AgentRequest.objects.filter(
                        agent=profile, agent_read=False
                    ).count()
                else:
                    from scheduling.models import AgentRequest
                    request.supervisor_request_badge = AgentRequest.objects.filter(
                        status='pending', supervisor_read=False
                    ).count()
            except Exception:
                pass

        return self.get_response(request)
