from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect

INACTIVITY_TIMEOUT = 4 * 3600   # 4 hours
ABSOLUTE_TIMEOUT = 16 * 3600    # 16 hours


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
