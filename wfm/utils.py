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


def get_monday_choices(weeks_back=4, weeks_forward=8):
    """(iso_string, label) pairs for Mondays from `weeks_back` weeks ago through
    `weeks_forward` weeks ahead, relative to today's week. For populating a
    plain server-rendered week-picker <select> — e.g. "Week of Aug 10"."""
    this_monday = get_week_start()
    choices = []
    for i in range(-weeks_back, weeks_forward + 1):
        d = this_monday + timedelta(weeks=i)
        choices.append((d.isoformat(), f"Week of {d.strftime('%b')} {d.day}"))
    return choices


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


def get_adherence_primary_resolver(agent_ids):
    """
    Return counts_for_adherence(agent_id, five9_username, on_date) -> bool.

    True only when that username belongs to a Five9Profile of that agent marked
    is_primary. ADHERENCE DISPLAY ONLY — no fallback: an agent with no primary
    account counts zero Five9 login/not-ready time (their codings still count
    separately). Billing, payroll and nomina keep using get_billable_username_map
    (the billable flag) — the two pipelines are independent by design.

    One query up front, then any number of questions — never a query per agent.
    Both sides are compared .strip().lower()'d: DailyAgentHours.five9_username is
    stored lowercased, Five9Profile.five9_username with only .strip() applied.

    on_date is required of every caller and deliberately ignored today. It is the
    seam for a later phase that resolves which account was primary on a given
    date — adding that becomes an internal change here and touches no caller.
    """
    from scheduling.models import Five9Profile

    primary_map = {}
    for p in Five9Profile.objects.filter(
        agent__in=agent_ids, is_primary=True
    ).values('agent_id', 'five9_username'):
        primary_map.setdefault(p['agent_id'], set()).add(p['five9_username'].strip().lower())

    def counts_for_adherence(agent_id, five9_username, on_date):
        names = primary_map.get(agent_id)
        if not names:
            return False
        return (five9_username or '').strip().lower() in names

    return counts_for_adherence
