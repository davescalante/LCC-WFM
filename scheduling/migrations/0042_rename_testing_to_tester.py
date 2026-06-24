from django.db import migrations


def rename_testing_to_tester(apps, schema_editor):
    Agent = apps.get_model('scheduling', 'Agent')
    RoleHistory = apps.get_model('scheduling', 'RoleHistory')
    ScheduledRoleChange = apps.get_model('scheduling', 'ScheduledRoleChange')
    Agent.objects.filter(role_type='testing').update(role_type='tester')
    RoleHistory.objects.filter(role_type='testing').update(role_type='tester')
    ScheduledRoleChange.objects.filter(new_role_type='testing').update(new_role_type='tester')


def reverse_rename(apps, schema_editor):
    Agent = apps.get_model('scheduling', 'Agent')
    RoleHistory = apps.get_model('scheduling', 'RoleHistory')
    ScheduledRoleChange = apps.get_model('scheduling', 'ScheduledRoleChange')
    Agent.objects.filter(role_type='tester').update(role_type='testing')
    RoleHistory.objects.filter(role_type='tester').update(role_type='testing')
    ScheduledRoleChange.objects.filter(new_role_type='tester').update(new_role_type='testing')


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0041_add_adherence_performance_indexes'),
    ]

    operations = [
        migrations.RunPython(rename_testing_to_tester, reverse_rename),
    ]
