from django.core.management.base import BaseCommand
from scheduling.views import apply_due_role_changes


class Command(BaseCommand):
    help = 'Apply all pending scheduled role changes whose effective date has arrived.'

    def handle(self, *args, **options):
        count = apply_due_role_changes()
        if count:
            self.stdout.write(self.style.SUCCESS(f'Applied {count} scheduled role change(s).'))
        else:
            self.stdout.write('No pending role changes due.')
