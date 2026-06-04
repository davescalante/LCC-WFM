from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0026_otshiftverification_coding_seconds'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ScheduledRoleChange',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('new_role_type', models.CharField(max_length=20, choices=[
                    ('training', 'Training'), ('incubation', 'Incubation'),
                    ('regular_agent', 'Regular Agent'), ('kill_team', 'Kill Team'),
                    ('night_shift', 'Night Shift'), ('supervisor', 'Supervisor'),
                    ('qa', 'QA'), ('cs', 'CS'), ('testing', 'Testing'),
                    ('sms_email', 'SMS/Email'), ('admin_training', 'Training'),
                    ('coordinator', 'Coordinator'),
                ])),
                ('effective_date', models.DateField()),
                ('new_shift_days', models.JSONField(blank=True, null=True)),
                ('new_shift_start_time', models.TimeField(blank=True, null=True)),
                ('new_shift_end_time', models.TimeField(blank=True, null=True)),
                ('scheduled_at', models.DateTimeField(auto_now_add=True)),
                ('applied_at', models.DateTimeField(blank=True, null=True)),
                ('cancelled_at', models.DateTimeField(blank=True, null=True)),
                ('agent', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='scheduled_role_changes', to='scheduling.agent')),
                ('scheduled_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('cancelled_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['effective_date']},
        ),
    ]
