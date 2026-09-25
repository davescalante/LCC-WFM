"""Read-only Five9 account setup report for active agents.

Lists every active agent's Five9 accounts and flags unusual configurations of the
primary/billable flags on scheduling.models.Five9Profile. Opens no write transaction
and never modifies a row. Not wired into any URL or view; run it by hand:

    python manage.py five9_account_setup_report

Why this exists. Some agents run a second Five9 account purely for talk-time
reporting; time on a non-primary account is never paid unless someone codes it.
Before touching how the Adherence tab displays that (a later phase), this reports
the real production Five9Profile setup so odd configurations -- no primary account,
a non-billable primary sitting next to a billable secondary, more than one billable
or primary account -- can be seen and understood first.
"""
from django.core.management.base import BaseCommand

from scheduling.models import Agent


def _display_name(agent):
    return agent.agent_name or agent.user.get_full_name() or agent.user.get_username()


class Command(BaseCommand):
    help = 'Read-only: report each active agent\'s Five9 account setup (no writes).'

    def handle(self, *args, **options):
        self.stdout.write('Read-only Five9 account setup report — active agents')
        self.stdout.write('(no rows modified)\n')

        agents = (
            Agent.objects.filter(status='active')
            .select_related('user', 'supervisor__user')
            .prefetch_related('five9_profiles')
            .order_by('agent_name')
        )

        section_a, section_b, section_c, section_d, section_e = [], [], [], [], []

        for agent in agents:
            profiles = list(agent.five9_profiles.all())
            if not profiles:
                continue

            primaries = [p for p in profiles if p.is_primary]
            billables = [p for p in profiles if p.billable]

            if len(profiles) >= 2 and not primaries:
                section_a.append((agent, profiles))
            if len(profiles) == 1 and not primaries:
                section_b.append((agent, profiles))

            non_billable_primaries = [p for p in primaries if not p.billable]
            if non_billable_primaries:
                other_billable = [p for p in billables if p not in non_billable_primaries]
                if other_billable:
                    section_c.append((agent, profiles))

            if len(billables) > 1:
                section_d.append((agent, profiles))

            if len(primaries) > 1:
                section_e.append((agent, profiles))

        sections = [
            ('a', '2 or more Five9 accounts, none marked primary', section_a),
            ('b', 'Exactly 1 Five9 account, not marked primary', section_b),
            ('c', 'Primary account is not billable, while another account is billable', section_c),
            ('d', 'More than one billable account', section_d),
            ('e', 'More than one account marked primary', section_e),
        ]

        for letter, title, entries in sections:
            self.stdout.write(f'({letter}) {title}')
            if not entries:
                self.stdout.write('  none\n')
                continue
            for agent, profiles in entries:
                supervisor = _display_name(agent.supervisor) if agent.supervisor else '—'
                self.stdout.write(
                    f'  {_display_name(agent)} — supervisor: {supervisor}, '
                    f'employer: {agent.get_employer_display()}'
                )
                for p in profiles:
                    self.stdout.write(
                        f'      {p.five9_username}  '
                        f'(primary={"Y" if p.is_primary else "N"}, '
                        f'billable={"Y" if p.billable else "N"})'
                    )
            self.stdout.write(f'  — {len(entries)} agent(s)\n')

        self.stdout.write(self.style.SUCCESS('Summary: ' + ', '.join(
            f'({letter}) {len(entries)}' for letter, _, entries in sections
        )))
        self.stdout.write(self.style.SUCCESS('Nothing was written.'))
