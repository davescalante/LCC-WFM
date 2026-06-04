from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0024_ot_login_verification'),
    ]

    operations = [
        migrations.AddField(
            model_name='overtimeshift',
            name='cancellation_reason',
            field=models.TextField(blank=True),
        ),
        migrations.AlterField(
            model_name='overtimeshift',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('completed', 'Completed'),
                    ('no_show', 'No Show'),
                    ('cancelled', 'Cancelled'),
                ],
                default='pending',
                max_length=12,
            ),
        ),
    ]
