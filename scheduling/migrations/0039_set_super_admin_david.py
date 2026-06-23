from django.db import migrations


def set_david_super_admin(apps, schema_editor):
    Agent = apps.get_model('scheduling', 'Agent')
    User = apps.get_model('auth', 'User')
    # Set super admin for David's account (davide@legalconversioncenter.com)
    try:
        user = User.objects.get(email='davide@legalconversioncenter.com')
        Agent.objects.filter(user=user).update(is_super_admin=True)
    except User.DoesNotExist:
        pass
    # Also set for any Django superuser who has an Agent profile
    for user in User.objects.filter(is_superuser=True):
        Agent.objects.filter(user=user).update(is_super_admin=True)


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0038_agent_super_admin'),
    ]

    operations = [
        migrations.RunPython(set_david_super_admin, migrations.RunPython.noop),
    ]
