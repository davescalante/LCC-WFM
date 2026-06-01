from django.db import migrations, models


def set_defaults(apps, schema_editor):
    Agent = apps.get_model('scheduling', 'Agent')
    Agent.objects.filter(status='active').update(
        employer='Infinity',
        billing_status='Billed',
        track_attendance=True,
    )
    Agent.objects.filter(status='inactive').update(
        employer='Infinity',
        billing_status='Not Billed',
        track_attendance=False,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0015_auditlog'),
    ]

    operations = [
        migrations.AddField(
            model_name='agent',
            name='employer',
            field=models.CharField(
                choices=[('LCC', 'LCC'), ('Infinity', 'Infinity')],
                default='Infinity',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='agent',
            name='billing_status',
            field=models.CharField(
                choices=[('Billed', 'Billed'), ('Not Billed', 'Not Billed')],
                default='Not Billed',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='agent',
            name='track_attendance',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(set_defaults, migrations.RunPython.noop),
    ]
