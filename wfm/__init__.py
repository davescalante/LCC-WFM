from django.contrib.auth.signals import user_logged_in


def _set_session_timestamps(sender, request, user, **kwargs):
    from django.utils import timezone
    ts = timezone.now().timestamp()
    request.session['_login_time'] = ts
    request.session['_last_activity'] = ts


user_logged_in.connect(_set_session_timestamps)
