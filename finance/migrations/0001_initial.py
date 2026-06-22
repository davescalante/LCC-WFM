from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='BillingSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('billing_rate_usd', models.DecimalField(decimal_places=2, default=Decimal('14.00'), help_text='Infinity billing rate to LCC (USD per hour, applies to all billable employees)', max_digits=8)),
                ('usd_to_mxn', models.DecimalField(decimal_places=4, default=Decimal('17.0000'), help_text='USD to MXN conversion rate', max_digits=10)),
                ('usd_to_mxn_updated', models.DateField(blank=True, help_text='Date the exchange rate was last updated', null=True)),
                ('nr_cap_regular_hours', models.DecimalField(decimal_places=2, default=Decimal('6.00'), help_text='Weekly not-ready cap for Regular Agents (hours)', max_digits=5)),
                ('nr_cap_kill_team_hours', models.DecimalField(decimal_places=2, default=Decimal('7.00'), help_text='Weekly not-ready cap for Kill Team agents (hours)', max_digits=5)),
                ('adherence_bonus_mxn', models.DecimalField(decimal_places=2, default=Decimal('0.00'), help_text='Adherence bonus amount in MXN paid to qualifying agents', max_digits=8)),
            ],
            options={
                'verbose_name': 'Billing Settings',
                'verbose_name_plural': 'Billing Settings',
            },
        ),
    ]
