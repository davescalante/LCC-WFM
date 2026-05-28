from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from scheduling.models import AuditLog
from adherence.models import DailyUpload
from erlang.models import ErlangActualStaff, ErlangReport


class Command(BaseCommand):
    help = 'Delete all app data except superuser accounts. Irreversible.'

    def handle(self, *args, **options):
        superusers = User.objects.filter(is_superuser=True)
        superuser_names = ', '.join(u.username for u in superusers)
        self.stdout.write(f'Keeping superuser(s): {superuser_names}')

        # Clear tables with no agent/user FK cascade
        audit_count, _ = AuditLog.objects.all().delete()
        erlang_staff, _ = ErlangActualStaff.objects.all().delete()
        erlang_reports, _ = ErlangReport.objects.all().delete()
        daily_uploads, _ = DailyUpload.objects.all().delete()  # cascades to DailyAgentHours

        # Delete all non-superuser users — cascades through Agent to all scheduling,
        # adherence, and related records.
        non_super = User.objects.filter(is_superuser=False)
        user_count = non_super.count()
        non_super.delete()

        self.stdout.write(self.style.SUCCESS(
            f'Done. Deleted: {user_count} users/agents, '
            f'{audit_count} activity log entries, '
            f'{erlang_staff} Erlang staff entries, '
            f'{erlang_reports} Erlang reports, '
            f'{daily_uploads} daily upload batches (+ their hour rows).'
        ))
