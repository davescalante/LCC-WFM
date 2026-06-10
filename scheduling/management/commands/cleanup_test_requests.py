from datetime import timedelta
from django.core.management.base import BaseCommand, CommandError
from scheduling.models import Agent, AgentRequest, Shift, ShiftTemplate


class Command(BaseCommand):
    help = 'Delete all AgentRequest test data for a named agent and revert auto-applied changes'

    def add_arguments(self, parser):
        parser.add_argument('agent_name', type=str, help='Agent name to clean up (partial match)')

    def handle(self, *args, **options):
        name = options['agent_name']
        matches = Agent.objects.filter(agent_name__icontains=name)
        if not matches.exists():
            raise CommandError(f'No agent found matching "{name}"')
        if matches.count() > 1:
            raise CommandError(f'Multiple agents match "{name}": {list(matches.values_list("agent_name", flat=True))}')

        agent = matches.first()
        self.stdout.write(f'Cleaning up test data for: {agent.agent_name}')

        requests = AgentRequest.objects.filter(agent=agent)
        self.stdout.write(f'Found {requests.count()} request(s)')

        from adherence.models import AdherenceRecord, Coding

        for req in requests:
            if req.status != 'approved':
                self.stdout.write(f'  Skipping {req.get_request_type_display()} ({req.status}) — no auto-actions to revert')
                continue

            if req.request_type == 'coding' and req.coding_date:
                deleted, _ = Coding.objects.filter(
                    agent=agent,
                    date=req.coding_date,
                    start_time=req.coding_start_time,
                    end_time=req.coding_end_time,
                ).delete()
                self.stdout.write(f'  Deleted {deleted} coding entry for {req.coding_date}')

            elif req.request_type == 'vacation' and req.vacation_start and req.vacation_end:
                d = req.vacation_start
                count = 0
                while d <= req.vacation_end:
                    deleted, _ = AdherenceRecord.objects.filter(agent=agent, date=d, status='V').delete()
                    count += deleted
                    d += timedelta(days=1)
                self.stdout.write(f'  Removed {count} Vacation adherence record(s): {req.vacation_start} – {req.vacation_end}')

            elif req.request_type == 'vto' and req.vto_date:
                deleted, _ = AdherenceRecord.objects.filter(agent=agent, date=req.vto_date, status='VTO').delete()
                self.stdout.write(f'  Removed {deleted} VTO adherence record(s) for {req.vto_date}')

            elif req.request_type == 'loa' and req.loa_start and req.loa_end:
                d = req.loa_start
                count = 0
                while d <= req.loa_end:
                    deleted, _ = AdherenceRecord.objects.filter(agent=agent, date=d, status='LOA').delete()
                    count += deleted
                    d += timedelta(days=1)
                self.stdout.write(f'  Removed {count} LOA adherence record(s): {req.loa_start} – {req.loa_end}')

            elif req.request_type == 'day_off_change' and req.effective_date:
                deleted, _ = Shift.objects.filter(agent=agent, date=req.effective_date).delete()
                self.stdout.write(f'  Removed shift override for {req.effective_date}')
                if req.day_off_type == 'permanent' and req.requested_day_off is not None:
                    eff_mon = req.effective_date - timedelta(days=req.effective_date.weekday())
                    deleted_t, _ = ShiftTemplate.objects.filter(
                        agent=agent,
                        day_of_week=req.requested_day_off,
                        effective_from=eff_mon,
                    ).delete()
                    self.stdout.write(f'  Removed {deleted_t} ShiftTemplate(s) created by permanent day-off change')

        deleted_reqs, _ = requests.delete()
        self.stdout.write(self.style.SUCCESS(f'Deleted {deleted_reqs} request(s). Cleanup complete.'))
