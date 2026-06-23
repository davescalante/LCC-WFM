from decimal import Decimal
from django.db import migrations


def backfill_hourly_rate(apps, schema_editor):
    Agent = apps.get_model('scheduling', 'Agent')
    Agent.objects.filter(hourly_rate__isnull=True).update(hourly_rate=Decimal('62.50'))
    Agent.objects.filter(hourly_rate=0).update(hourly_rate=Decimal('62.50'))


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0035_agent_hourly_rate_default'),
    ]

    operations = [
        migrations.RunPython(backfill_hourly_rate, migrations.RunPython.noop),
    ]
