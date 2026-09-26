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

    True only when that username was the agent's PRIMARY account ON THAT DATE,
    according to their Five9PrimaryPeriod history. ADHERENCE DISPLAY ONLY — no
    fallback: an agent with no primary account on a date counts zero Five9
    login/not-ready time that day (their codings still count separately).
    Billing, payroll and nomina keep using get_billable_username_map (the
    billable flag) — the two pipelines are independent by design.

    For any date, the NEWEST period covering it wins (highest pk). A date no
    period covers resolves to nobody, which is the same "no primary means zero"
    answer the flag-only version gave.

    Agents with no history at all bootstrap from today's is_primary flag, treated
    as primary from the beginning. That is for write paths that have not recorded
    a period yet — never a fallback to a non-primary account.

    At most TWO queries up front, then any number of questions — never a query
    per agent or per day. Answers are memoised per (agent_id, date).

    Both sides are compared .strip().lower()'d: DailyAgentHours.five9_username is
    stored lowercased, Five9Profile.five9_username with only .strip() applied.
    """
    from scheduling.models import Five9Profile, Five9PrimaryPeriod

    agent_ids = list(agent_ids)

    # Periods, oldest first. profile__five9_username is a JOIN on the same query,
    # so a rename follows the entry with no extra lookup; the stored snapshot is
    # used only once the account itself has been deleted.
    periods = {}
    for p in Five9PrimaryPeriod.objects.filter(agent_id__in=agent_ids).values(
        'agent_id', 'start_date', 'end_date', 'five9_username', 'profile__five9_username'
    ).order_by('id'):
        name = p['profile__five9_username'] or p['five9_username'] or ''
        periods.setdefault(p['agent_id'], []).append(
            (p['start_date'], p['end_date'], name.strip().lower())
        )

    # Bootstrap only for agents with no history at all.
    bootstrap = {}
    unseeded = [aid for aid in agent_ids if aid not in periods]
    if unseeded:
        for p in Five9Profile.objects.filter(
            agent_id__in=unseeded, is_primary=True
        ).values('agent_id', 'five9_username'):
            bootstrap.setdefault(p['agent_id'], set()).add(p['five9_username'].strip().lower())

    winner_cache = {}

    def _winner(agent_id, on_date):
        """The username primary for that agent on that date, or None."""
        key = (agent_id, on_date)
        if key in winner_cache:
            return winner_cache[key]
        result = None
        for start, end, name in reversed(periods.get(agent_id, ())):   # newest first
            if (start is None or on_date >= start) and (end is None or on_date <= end):
                result = name
                break
        winner_cache[key] = result
        return result

    def counts_for_adherence(agent_id, five9_username, on_date):
        uname = (five9_username or '').strip().lower()
        if not uname:
            return False
        if agent_id not in periods:
            return uname in bootstrap.get(agent_id, ())
        return uname == _winner(agent_id, on_date)

    return counts_for_adherence
