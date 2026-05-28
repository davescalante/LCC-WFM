from django.db import models
from django.contrib.auth.models import User


class Agent(models.Model):
    ROLE_CHOICES = [
        ('agent', 'Agent'),
        ('admin', 'Admin'),
    ]
    ROLE_TYPE_CHOICES = [
        # Agent types
        ('training', 'Training'),
        ('incubation', 'Incubation'),
        ('regular_agent', 'Regular Agent'),
        ('kill_team', 'Kill Team'),
        ('night_shift', 'Night Shift'),
        # Admin types
        ('supervisor', 'Supervisor'),
        ('qa', 'QA'),
        ('cs', 'CS'),
        ('testing', 'Testing'),
        ('sms_email', 'SMS/Email'),
        ('admin_training', 'Training'),
        ('coordinator', 'Coordinator'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='agent')
    role_type = models.CharField(max_length=20, choices=ROLE_TYPE_CHOICES, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')
    agent_name = models.CharField(max_length=100, blank=True, help_text="Display/call center name")
    employee_id = models.CharField(max_length=50, blank=True, unique=True, null=True)
    start_date = models.DateField(null=True, blank=True)
    termination_date = models.DateField(null=True, blank=True)
    COUNTRY_CODE_CHOICES = [
        ('+1', '+1 (US/Canada)'),
        ('+52', '+52 (Mexico)'),
    ]
    phone_country_code = models.CharField(max_length=5, choices=COUNTRY_CODE_CHOICES, default='+1', blank=True)
    phone_number = models.CharField(max_length=20, blank=True)
    five9_username = models.CharField(max_length=150, blank=True)
    five9_password = models.CharField(max_length=150, blank=True)
    teams_password = models.CharField(max_length=150, blank=True)
    supervisor = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='direct_reports'
    )
    notes = models.TextField(blank=True)

    def __str__(self):
        return self.agent_name or self.user.get_full_name() or self.user.username


class EmploymentPeriod(models.Model):
    REASON_CHOICES = [
        ('', '— Active / No reason —'),
        ('resigned', 'Resigned'),
        ('terminated', 'Terminated'),
        ('laid_off', 'Laid Off'),
        ('contract_end', 'Contract Ended'),
        ('other', 'Other'),
    ]
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name='employment_periods')
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    reason_ended = models.CharField(max_length=20, choices=REASON_CHOICES, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['start_date']

    def __str__(self):
        end = self.end_date.strftime('%b %d, %Y') if self.end_date else 'Present'
        return f"{self.start_date.strftime('%b %d, %Y')} – {end}"

    @property
    def is_current(self):
        return self.end_date is None


class Shift(models.Model):
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name='shifts')
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_off = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['date', 'start_time']

    def __str__(self):
        return f"{self.agent} - {self.date} {self.start_time}-{self.end_time}"


class Break(models.Model):
    BREAK_TYPES = [
        ('break', 'Break'),
        ('lunch', 'Lunch'),
        ('training', 'Training'),
        ('meeting', 'Meeting'),
        ('coaching', 'Coaching'),
    ]
    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name='breaks')
    break_type = models.CharField(max_length=20, choices=BREAK_TYPES, default='break')
    start_time = models.TimeField()
    end_time = models.TimeField()

    def __str__(self):
        return f"{self.shift.agent} - {self.break_type} {self.start_time}-{self.end_time}"
