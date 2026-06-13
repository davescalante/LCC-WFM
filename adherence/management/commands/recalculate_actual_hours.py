from decimal import Decimal
from django.core.management.base import BaseCommand
from adherence.models import DailyAgentHours, Coding, AdherenceRecord


class Command(BaseCommand):
    help = 'Recalculate actual_hours for all agents on all uploaded dates using current codings'

    def handle(self, *args, **options):
        dahs = (
            DailyAgentHours.objects
            .filter(agent__isnull=False)
            .select_related('upload', 'agent__user')
            .order_by('upload__date', 'agent__user__last_name')
        )

        total = dahs.count()
        corrected = 0
        unchanged = 0

        self.stdout.write(f'Processing {total} agent-day upload records...')

        for dah in dahs:
            agent_id = dah.agent_id
            upload_date = dah.upload.date

            coded_secs = sum(
                c.total_seconds_count()
                for c in Coding.objects.filter(agent_id=agent_id, date=upload_date)
            )
            total_secs = dah.login_seconds + coded_secs
            allowance_secs = int(total_secs * 0.125)
            excess_secs = max(0, dah.not_ready_seconds - allowance_secs)
            login_final_secs = max(0, dah.login_seconds - excess_secs)
            new_hours = Decimal(str(round(login_final_secs / 3600, 6)))

            record = AdherenceRecord.objects.filter(agent_id=agent_id, date=upload_date).first()
            old_hours = record.actual_hours if record else None

            if old_hours != new_hours:
                AdherenceRecord.objects.update_or_create(
                    agent_id=agent_id,
                    date=upload_date,
                    defaults={'actual_hours': new_hours},
                )
                agent_name = dah.agent.agent_name or dah.agent.user.get_full_name()
                self.stdout.write(
                    f'  UPDATED {agent_name} on {upload_date}: '
                    f'{old_hours} → {new_hours}'
                )
                corrected += 1
            else:
                unchanged += 1

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. {corrected} record(s) corrected, {unchanged} already correct.'
        ))
