"""RETIRED. Do not run.

This command used to recompute AdherenceRecord.actual_hours by selecting
Five9 hours through the `billable` flag, with a fallback that counted EVERY
account when no billable profile was set, and a hardcoded nr_ratio of 0.125.
That fallback is what caused the Sep 18, 2026 unpaid-day incident: an agent's
9-hour OT day worked entirely on a non-primary account was counted as worked
on the Adherence tab, so nobody coded it and he went unpaid.

Kept as a stub (rather than deleted) so an old invocation -- cron, a stale
runbook, muscle memory -- fails loudly instead of silently doing nothing or
erroring on an import that no longer exists. It writes nothing and accepts
no arguments (old or new); the signature swallows anything passed and never
touches the database.

Use adherence.management.commands.recalculate_display_hours instead --
preview by default, --apply to write.
"""
from django.core.management.base import BaseCommand

RETIREMENT_MESSAGE = (
    "recalculate_actual_hours is retired. It selected hours by the billable "
    "flag with a fallback that counted every account when none was billable, "
    "and hardcoded a 0.125 NR ratio -- running it would put extra-account "
    "time back into adherence display hours. Use recalculate_display_hours "
    "instead (preview by default; --apply to write). Nothing was written."
)


class Command(BaseCommand):
    help = 'RETIRED -- writes nothing. Use recalculate_display_hours instead.'

    def handle(self, *args, **options):
        self.stdout.write(RETIREMENT_MESSAGE)
        raise SystemExit(1)
