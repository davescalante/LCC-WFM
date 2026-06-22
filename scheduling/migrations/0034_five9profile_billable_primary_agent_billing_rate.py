from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0033_agentseparation_update'),
    ]

    operations = [
        migrations.AddField(
            model_name='five9profile',
            name='billable',
            field=models.BooleanField(default=True, help_text='Hours from this user count toward billing and payroll'),
        ),
        migrations.AddField(
            model_name='five9profile',
            name='is_primary',
            field=models.BooleanField(default=False, help_text='Used for attendance tracking and CSV matching display'),
        ),
        migrations.AddField(
            model_name='agent',
            name='billing_rate_usd',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Override billing rate in USD (uses global rate if blank)', max_digits=8, null=True),
        ),
        # Mark the first Five9Profile for each agent as primary (data migration)
        migrations.RunSQL(
            """
            UPDATE scheduling_five9profile
            SET is_primary = TRUE
            WHERE id IN (
                SELECT MIN(id) FROM scheduling_five9profile GROUP BY agent_id
            );
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
