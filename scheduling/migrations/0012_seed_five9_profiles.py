from django.db import migrations


def seed_profiles(apps, schema_editor):
    Agent = apps.get_model('scheduling', 'Agent')
    Five9Profile = apps.get_model('scheduling', 'Five9Profile')
    for agent in Agent.objects.filter(five9_username__gt=''):
        if not Five9Profile.objects.filter(agent=agent).exists():
            Five9Profile.objects.create(
                agent=agent,
                label='Primary',
                five9_username=agent.five9_username,
                five9_password=agent.five9_password or '',
                role_type=agent.role_type or '',
            )


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0011_five9_profile'),
    ]

    operations = [
        migrations.RunPython(seed_profiles, migrations.RunPython.noop),
    ]
