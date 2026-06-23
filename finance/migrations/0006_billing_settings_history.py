from decimal import Decimal
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def seed_history_from_singleton(apps, schema_editor):
    BillingSettings = apps.get_model('finance', 'BillingSettings')
    BillingSettingsHistory = apps.get_model('finance', 'BillingSettingsHistory')
    singleton = BillingSettings.objects.filter(pk=1).first()
    if singleton:
        BillingSettingsHistory.objects.create(
            week_start='2020-01-06',
            billing_rate_usd=singleton.billing_rate_usd,
            usd_to_mxn=singleton.usd_to_mxn,
            nr_cap_regular_hours=singleton.nr_cap_regular_hours,
            nr_cap_kill_team_hours=singleton.nr_cap_kill_team_hours,
            default_admin_bonus_mxn=singleton.default_admin_bonus_mxn,
            adherence_bonus_max_mxn=singleton.adherence_bonus_max_mxn,
            adherence_bonus_full_hours=singleton.adherence_bonus_full_hours,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0005_default_admin_bonus'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='BillingSettingsHistory',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('week_start', models.DateField(db_index=True, help_text='Monday of the first week this rate applies')),
                ('changed_at', models.DateTimeField(auto_now_add=True)),
                ('billing_rate_usd', models.DecimalField(decimal_places=2, max_digits=8)),
                ('usd_to_mxn', models.DecimalField(decimal_places=4, max_digits=10)),
                ('nr_cap_regular_hours', models.DecimalField(decimal_places=2, max_digits=5)),
                ('nr_cap_kill_team_hours', models.DecimalField(decimal_places=2, max_digits=5)),
                ('default_admin_bonus_mxn', models.DecimalField(decimal_places=2, max_digits=8)),
                ('adherence_bonus_max_mxn', models.DecimalField(decimal_places=2, max_digits=8)),
                ('adherence_bonus_full_hours', models.DecimalField(decimal_places=2, max_digits=5)),
                ('changed_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='billing_settings_changes', to=settings.AUTH_USER_MODEL
                )),
            ],
            options={
                'verbose_name': 'Billing Settings History',
                'verbose_name_plural': 'Billing Settings History',
                'ordering': ['-week_start', '-changed_at'],
            },
        ),
        migrations.RunPython(seed_history_from_singleton, migrations.RunPython.noop),
    ]
