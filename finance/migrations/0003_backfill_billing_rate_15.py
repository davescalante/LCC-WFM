from decimal import Decimal
from django.db import migrations


def update_billing_rate(apps, schema_editor):
    BillingSettings = apps.get_model('finance', 'BillingSettings')
    BillingSettings.objects.filter(billing_rate_usd=Decimal('14.00')).update(
        billing_rate_usd=Decimal('15.00')
    )


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0002_billing_rate_default_15'),
    ]

    operations = [
        migrations.RunPython(update_billing_rate, migrations.RunPython.noop),
    ]
