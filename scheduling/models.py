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
    EMPLOYER_CHOICES = [
        ('LCC', 'LCC'),
        ('Infinity', 'Infinity'),
    ]
    BILLING_STATUS_CHOICES = [
        ('Billed', 'Billed'),
        ('Not Billed', 'Not Billed'),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='agent')
    role_type = models.CharField(max_length=20, choices=ROLE_TYPE_CHOICES, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')
    employer = models.CharField(max_length=20, choices=EMPLOYER_CHOICES, default='Infinity')
    billing_status = models.CharField(max_length=20, choices=BILLING_STATUS_CHOICES, default='Not Billed')
    track_attendance = models.BooleanField(default=False)
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
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True)

    def __str__(self):
        return self.agent_name or self.user.get_full_name() or self.user.username


class Five9Profile(models.Model):
    ROLE_TYPE_CHOICES = Agent.ROLE_TYPE_CHOICES

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name='five9_profiles')
    label = models.CharField(max_length=100, blank=True, help_text="e.g. Primary, Kill Team, Overtime")
    five9_username = models.CharField(max_length=150)
    five9_password = models.CharField(max_length=150, blank=True)
    role_type = models.CharField(max_length=20, choices=ROLE_TYPE_CHOICES, blank=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        tag = self.label or self.get_role_type_display() or 'Account'
        return f"{self.five9_username} ({tag})"


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


class ShiftTemplate(models.Model):
    """Recurring weekly schedule for an agent — the default for every week."""
    DAY_CHOICES = [
        (0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'),
        (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday'),
    ]
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name='shift_templates')
    day_of_week = models.IntegerField(choices=DAY_CHOICES)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    is_off = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    # Week (Monday) from which this template takes effect. NULL = legacy/always.
    effective_from = models.DateField(null=True, blank=True)
    # Week (Monday) after which this template no longer applies. NULL = no end.
    effective_until = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ['day_of_week', 'effective_from']

    def __str__(self):
        day = self.get_day_of_week_display()
        if self.is_off:
            return f"{self.agent} {day} OFF"
        return f"{self.agent} {day} {self.start_time}–{self.end_time}"


class ShiftTemplateBlock(models.Model):
    shift_template = models.ForeignKey(ShiftTemplate, on_delete=models.CASCADE, related_name='extra_blocks')
    block_number = models.IntegerField()  # 2 or 3
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        unique_together = ('shift_template', 'block_number')
        ordering = ['block_number']

    def __str__(self):
        return f"Block {self.block_number}: {self.start_time}–{self.end_time}"


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


class ShiftBlock(models.Model):
    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name='extra_blocks')
    block_number = models.IntegerField()  # 2 or 3
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        unique_together = ('shift', 'block_number')
        ordering = ['block_number']

    def __str__(self):
        return f"Block {self.block_number}: {self.start_time}–{self.end_time}"


class OvertimeShift(models.Model):
    INCENTIVE_CHOICES = [
        ('none', 'No Incentive'),
        ('time_and_a_half', 'Time & a Half (1.5x)'),
        ('power_hour', 'Power Hour (2x)'),
    ]
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name='overtime_shifts')
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    incentive_type = models.CharField(max_length=20, choices=INCENTIVE_CHOICES, default='none')
    incentivized_hours = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    base_hourly_rate = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    is_completed = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['date']

    def __str__(self):
        return f"{self.agent} — OT {self.date} {self.start_time}–{self.end_time}"

    def total_shift_hours(self):
        from decimal import Decimal
        s, e = self.start_time, self.end_time
        secs = (e.hour * 3600 + e.minute * 60 + e.second) - (s.hour * 3600 + s.minute * 60 + s.second)
        if secs < 0:
            secs += 86400
        return Decimal(str(round(secs / 3600, 2)))

    def incentive_offered(self):
        from decimal import Decimal
        rate = self.base_hourly_rate
        if not rate:
            return None
        total_hrs = self.total_shift_hours()
        inc_hrs = self.incentivized_hours or Decimal('0')
        if self.incentive_type == 'time_and_a_half':
            premium = Decimal('0.5')
        elif self.incentive_type == 'power_hour':
            premium = Decimal('1.0')
        else:
            premium = Decimal('0')
        return (total_hrs * rate + inc_hrs * rate * premium).quantize(Decimal('0.01'))

    def incentive_earned(self):
        if not self.is_completed:
            return None
        return self.incentive_offered()


class AuditLog(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    action = models.CharField(max_length=200)
    detail = models.TextField(blank=True)
    agent = models.ForeignKey(Agent, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} — {self.user} — {self.action}"


def log_action(user, action, detail='', agent=None):
    AuditLog.objects.create(user=user, action=action, detail=detail, agent=agent)


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
