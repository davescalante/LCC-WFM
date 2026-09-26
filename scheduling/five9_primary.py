"""Primary Five9 account history — the one place that writes Five9PrimaryPeriod
and keeps Five9Profile.is_primary in step with it.

The rule: each entry names one account and covers a range of days; for any date
the NEWEST entry covering it (highest pk) decides which account was primary.
Entries are append-only — a correction is a newer entry, never an edit.

is_primary is NOT a second source of truth. It stays because many screens, sorts
and exports read it for TODAY's setup, and this module guarantees it always equals
whatever the history says is primary today. Everything date-aware reads the history
through wfm.utils.get_adherence_primary_resolver instead.

ADHERENCE DISPLAY ONLY. Billing, payroll, Nomina and OT verification select
usernames through wfm.utils.get_billable_username_map on the `billable` flag and
never read anything here — the two pipelines are independent by design.
"""
from datetime import date, timedelta

from django.template.defaultfilters import date as date_filter
from django.utils import timezone

from .models import Five9PrimaryPeriod, log_action


def _fmt(d):
    """A date the way the rest of the app writes one: 'Oct 5, 2026'."""
    return date_filter(d, 'M j, Y')


def _range_words(start, end):
    """The covered range in plain words, for the Activity Log."""
    if start is None and end is None:
        return 'from the beginning, open-ended'
    if start is None:
        return f'from the beginning until {_fmt(end)}'
    if end is None:
        return f'from {_fmt(start)}, open-ended'
    return f'{_fmt(start)} to {_fmt(end)}'


def _covers(period, on_date):
    return ((period.start_date is None or on_date >= period.start_date)
            and (period.end_date is None or on_date <= period.end_date))


def _winning_period(periods, on_date):
    """Newest entry covering that date, or None. `periods` is oldest-first."""
    for p in reversed(periods):
        if _covers(p, on_date):
            return p
    return None


def _periods_for(agent):
    return list(agent.five9_primary_periods.select_related('profile').order_by('id'))


def todays_primary_period(agent):
    """The entry deciding who is primary today, or None."""
    return _winning_period(_periods_for(agent), timezone.localdate())


def current_primary_profile(agent):
    """The Five9Profile that is primary today according to the history, or None.

    None also when today's winning entry names an account that has since been
    deleted — there is no surviving row to point at, which is exactly what
    is_primary shows in that case too.
    """
    period = todays_primary_period(agent)
    if period is None:
        return None
    if period.profile_id:
        return period.profile
    name = (period.five9_username or '').strip().lower()
    if not name:
        return None
    return next((p for p in agent.five9_profiles.all()
                 if p.five9_username.strip().lower() == name), None)


def ensure_seeded(agent):
    """Backfill the 'from the beginning' entry for an agent whose primary account
    predates this history — the same bootstrap get_adherence_primary_resolver
    applies when reading, made real so write paths have something to build on.

    Records nothing observable: the entry says exactly what is_primary already
    said, so there is no recalculation and no Activity Log entry, for the same
    reason the seed migration writes rows only.

    Returns the entry it created, or None when there was nothing to backfill.
    """
    if agent.five9_primary_periods.exists():
        return None
    profile = agent.five9_profiles.filter(is_primary=True).order_by('id').first()
    if profile is None:
        return None
    return Five9PrimaryPeriod.objects.create(
        agent=agent,
        profile=profile,
        five9_username=profile.five9_username,
        start_date=None,
        end_date=None,
        kind='initial',
        created_by=None,
    )


def sync_is_primary_from_history(agent):
    """Make Five9Profile.is_primary equal today's winner, exactly.

    Uses .update() rather than .save() so it can never re-enter a save path or
    fire a side effect. Returns the profile now marked primary, or None.
    """
    winner = current_primary_profile(agent)
    if winner is None:
        agent.five9_profiles.update(is_primary=False)
        return None
    agent.five9_profiles.exclude(pk=winner.pk).update(is_primary=False)
    agent.five9_profiles.filter(pk=winner.pk).update(is_primary=True)
    return winner


def recalculate_affected_days(agent, start, end):
    """Recompute this agent's stored adherence display hours for exactly the days
    an entry covers, reusing recalculate_display_hours' rules unchanged: update
    only, never creates a row, never touches an Official Admin, only days that
    actually have a Daily Hours row behind them. No new math, and no Activity Log
    entry of its own — the entry that caused it owns that record.

    `start` None means from the beginning (no lower bound). Nothing past today can
    have adherence data, so the range is always capped at today.
    """
    from adherence.management.commands.recalculate_display_hours import (
        plan_display_hours, apply_display_hours,
    )
    today = timezone.localdate()
    upper = min(end, today) if end else today
    if start is not None and start > upper:
        return 0
    return apply_display_hours(plan_display_hours(start, upper, agent.pk))


def record_primary_period(agent, profile, start, end, kind, user=None):
    """Record one primary-account entry and everything that must move with it:
    the is_primary sync, the recalculation of exactly the days it covers, and one
    Activity Log entry. Callers run this inside their own transaction, so all four
    land together or not at all.

    The backfill runs FIRST, and it is load-bearing rather than tidy-up. Without
    it, an entry that does not cover today (a past period, say) could be the
    agent's only history — today's winner would then resolve to nobody and the
    sync below would clear is_primary, silently dropping that agent to zero Five9
    adherence login. It lives here, at the single choke point, so no write path
    can forget it; running first is also what gives the backfilled entry a lower
    pk than this one, so it never wins over it.
    """
    ensure_seeded(agent)
    period = Five9PrimaryPeriod.objects.create(
        agent=agent,
        profile=profile,
        five9_username=(profile.five9_username if profile else ''),
        start_date=start,
        end_date=end,
        kind=kind,
        created_by=user if getattr(user, 'pk', None) else None,
    )
    sync_is_primary_from_history(agent)
    recalculate_affected_days(agent, start, end)
    log_action(
        period.created_by,
        'Changed Five9 primary account',
        f'{agent} — {period.resolved_username()} · '
        f'{period.get_kind_display()} · {_range_words(start, end)}',
        agent=agent,
    )
    return period


def refresh_username_snapshots(profile):
    """Keep every entry's fallback username current when an account is renamed, so
    it is still right if that account is later deleted. While the account exists
    the live name is read through the link, so a rename already follows on its own.
    """
    profile.primary_periods.update(five9_username=profile.five9_username)


def resolve_primary_timeline(agent):
    """Flatten the append-only entries into contiguous segments, newest-wins.

    Returns [{'start': date|None, 'end': date|None, 'username': str|None}, ...]
    oldest first, where a None start means "from the beginning", a None end means
    "still current", and a None username means nobody was primary then.
    """
    periods = _periods_for(agent)
    if not periods:
        return []

    # A winner can only change at a period's start, or the day after its end.
    bounds = set()
    for p in periods:
        if p.start_date:
            bounds.add(p.start_date)
        if p.end_date:
            bounds.add(p.end_date + timedelta(days=1))
    ordered = [None] + sorted(bounds)

    merged = []
    for b in ordered:
        winner = _winning_period(periods, b if b is not None else date.min)
        name = winner.resolved_username() if winner else None
        if merged and merged[-1][1] == name:
            continue
        merged.append((b, name))

    segments = []
    for i, (start, name) in enumerate(merged):
        nxt = merged[i + 1][0] if i + 1 < len(merged) else None
        segments.append({
            'start': start,
            'end': (nxt - timedelta(days=1)) if nxt else None,
            'username': name,
        })
    return segments


def format_primary_timeline(agent):
    """One plain-words line of who was primary when, or '' when there is nothing
    worth saying — which is the normal case of a single account that has always
    been primary. Only agents where more than one account has actually been
    primary get a line.

        Primary: marreyes until Oct 4, 2026 · marreyes2 Oct 5, 2026 – Oct 7, 2026
        · marreyes from Oct 8, 2026 (current)
    """
    segments = resolve_primary_timeline(agent)
    if len({s['username'] for s in segments if s['username']}) < 2:
        return ''

    parts = []
    for s in segments:
        name = s['username'] or 'no primary'
        if s['start'] is None and s['end'] is not None:
            parts.append(f"{name} until {_fmt(s['end'])}")
        elif s['end'] is None:
            parts.append(f"{name} from {_fmt(s['start'])} (current)"
                         if s['start'] else f'{name} (current)')
        else:
            parts.append(f"{name} {_fmt(s['start'])} – {_fmt(s['end'])}")
    return 'Primary: ' + ' · '.join(parts)
