import time
from datetime import date, timedelta
from django.utils import timezone


def retry_on_locked(fn, tries=6, delay=0.12):
    """Run a DB write `fn`, retrying briefly on SQLite 'database is locked'. Two
    writes landing at the same instant can deadlock on SQLite even with WAL + a busy
    timeout (a real risk under the threaded dev server); a short retry turns that
    transient lock into a successful save instead of a 500. On any other error, or
    after `tries` attempts, the exception propagates unchanged. No-op effect on
    Postgres (production), which never raises this. Returns whatever `fn` returns."""
    from django.db import OperationalError
    for attempt in range(tries):
        try:
            return fn()
        except OperationalError as exc:
            if 'locked' in str(exc).lower() and attempt < tries - 1:
                time.sleep(delay)
                continue
            raise


def get_week_start(d=None):
    """Return the Monday of the week containing `d` (defaults to today)."""
    if d is None:
        d = timezone.localdate()
    return d - timedelta(days=d.weekday())


def parse_week_param(raw):
    """Parse an ISO date string and snap to its Monday. Returns None on bad input."""
    try:
        d = date.fromisoformat(raw)
        return d - timedelta(days=d.weekday())
    except (ValueError, TypeError, AttributeError):
        return None


def get_billable_username_map(agent_ids):
    """
    Return (billable_map, primary_billable_map) for a list/queryset of agent PKs.

    billable_map:         agent_id -> set of lowercase Five9 usernames (for set lookups)
    primary_billable_map: agent_id -> display username of the primary billable profile
    """
    from scheduling.models import Five9Profile

    billable_map = {}
    primary_billable_map = {}
    for p in Five9Profile.objects.filter(
        agent__in=agent_ids, billable=True
    ).values('agent_id', 'five9_username', 'is_primary').order_by('agent_id', '-is_primary', 'id'):
        aid = p['agent_id']
        billable_map.setdefault(aid, set()).add(p['five9_username'].strip().lower())
        if aid not in primary_billable_map:
            primary_billable_map[aid] = p['five9_username']

    return billable_map, primary_billable_map
