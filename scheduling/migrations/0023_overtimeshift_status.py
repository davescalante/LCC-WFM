from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0022_rolehistory'),
    ]

    operations = [
        migrations.AddField(
            model_name='overtimeshift',
            name='status',
            field=models.CharField(
                choices=[('pending', 'Pending'), ('completed', 'Completed'), ('no_show', 'No Show')],
                default='pending',
                max_length=10,
            ),
        ),
        migrations.RunSQL(
            "UPDATE scheduling_overtimeshift SET status = 'completed' WHERE is_completed = 1",
            reverse_sql="UPDATE scheduling_overtimeshift SET is_completed = 1 WHERE status = 'completed'",
        ),
        migrations.RemoveField(
            model_name='overtimeshift',
            name='is_completed',
        ),
    ]
