from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0025_overtimeshift_cancelled'),
    ]

    operations = [
        migrations.AddField(
            model_name='otshiftverification',
            name='five9_seconds',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='otshiftverification',
            name='coding_seconds',
            field=models.IntegerField(default=0),
        ),
    ]
