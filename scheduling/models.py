from decimal import Decimal
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
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, default=Decimal('62.50'), help_text="Agent pay rate in MXN")
    billing_rate_usd = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, help_text="Override billing rate in USD (uses global rate if blank)")
    is_official_admin = models.BooleanField(default=False, help_text="Genuine administrator — receives admin bonus instead of adherence bonus")
    admin_bonus_mxn = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, help_text="Individual admin bonus in MXN (uses global default if blank)")
    notes = models.TextField(blank=True)

    @property
    def separation(self):
        """Returns the current (latest non-cancelled) separation record, or None."""
        seps = [s for s in self.separations.all() if s.status != 'cancelled']
        return sorted(seps, key=lambda s: s.processed_at, reverse=True)[0] if seps else None

    def __str__(self):
        return self.agent_name or self.user.get_full_name() or self.user.username


class Five9Profile(models.Model):
    ROLE_TYPE_CHOICES = Agent.ROLE_TYPE_CHOICES

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name='five9_profiles')
    label = models.CharField(max_length=100, blank=True, help_text="e.g. Primary, Kill Team, Overtime")
    five9_username = models.CharField(max_length=150)
    five9_password = models.CharField(max_length=150, blank=True)
    role_type = models.CharField(max_length=20, choices=ROLE_TYPE_CHOICES, blank=True)
    billable = models.BooleanField(default=True, help_text="Hours from this user count toward billing and payroll")
    is_primary = models.BooleanField(default=False, help_text="Used for attendance tracking and CSV matching display")

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
        indexes = [
            models.Index(fields=['agent', 'day_of_week']),
            models.Index(fields=['agent', 'day_of_week', 'effective_from']),
        ]

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
        indexes = [
            models.Index(fields=['agent', 'date']),
            models.Index(fields=['date']),
        ]

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
    OT_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('no_show', 'No Show'),
        ('cancelled', 'Cancelled'),
    ]
    status = models.CharField(max_length=12, choices=OT_STATUS_CHOICES, default='pending')
    notes = models.TextField(blank=True)
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        ordering = ['date']
        indexes = [
            models.Index(fields=['agent', 'date']),
            models.Index(fields=['date']),
        ]

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
        if self.status != 'completed':
            return None
        return self.incentive_offered()


class LoginLogoutUpload(models.Model):
    uploaded_at = models.DateTimeField(auto_now_add=True)
    filename = models.CharField(max_length=255)
    row_count = models.IntegerField(default=0)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"{self.filename} ({self.uploaded_at:%Y-%m-%d %H:%M})"


class AgentLoginSession(models.Model):
    upload = models.ForeignKey(LoginLogoutUpload, on_delete=models.CASCADE, related_name='sessions')
    agent = models.ForeignKey(Agent, on_delete=models.SET_NULL, null=True, blank=True, related_name='login_sessions')
    five9_username = models.CharField(max_length=100)
    date = models.DateField()
    login_at = models.DateTimeField()
    logout_at = models.DateTimeField()
    session_seconds = models.IntegerField(default=0)

    class Meta:
        ordering = ['date', 'login_at']
        indexes = [
            models.Index(fields=['agent', 'date']),
            models.Index(fields=['five9_username', 'date']),
        ]


class OTShiftVerification(models.Model):
    ot_shift = models.OneToOneField(OvertimeShift, on_delete=models.CASCADE, related_name='verification')
    upload = models.ForeignKey(LoginLogoutUpload, on_delete=models.SET_NULL, null=True, related_name='verifications')
    verified_seconds = models.IntegerField(default=0)
    five9_seconds = models.IntegerField(default=0)
    coding_seconds = models.IntegerField(default=0)
    shift_seconds = models.IntegerField(default=0)
    username_found = models.BooleanField(default=True)
    verified_at = models.DateTimeField(auto_now=True)

    @property
    def coverage_pct(self):
        if self.shift_seconds == 0:
            return None
        return min(100.0, round(self.verified_seconds / self.shift_seconds * 100, 1))

    @staticmethod
    def _fmt_secs(secs):
        h, m = divmod(secs // 60, 60)
        return (f"{h}h {m}m" if m else f"{h}h") if h else f"{m}m"

    @property
    def verified_display(self):
        return self._fmt_secs(self.verified_seconds)

    @property
    def five9_display(self):
        return self._fmt_secs(self.five9_seconds)

    @property
    def coding_display(self):
        return self._fmt_secs(self.coding_seconds)

    @property
    def shift_display(self):
        return self._fmt_secs(self.shift_seconds)


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


class ScheduledRoleChange(models.Model):
    """A role change queued to take effect automatically on a future date."""
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name='scheduled_role_changes')
    new_role_type = models.CharField(max_length=20, choices=Agent.ROLE_TYPE_CHOICES)
    effective_date = models.DateField()
    # Optional new schedule applied on effective_date
    new_shift_days = models.JSONField(null=True, blank=True)       # list of 0-6 integers
    new_shift_start_time = models.TimeField(null=True, blank=True)
    new_shift_end_time = models.TimeField(null=True, blank=True)
    # Optional new supervisor applied on effective_date
    new_supervisor = models.ForeignKey(
        'Agent', null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    # Audit
    scheduled_by = models.ForeignKey(
        'auth.User', null=True, on_delete=models.SET_NULL, related_name='+'
    )
    scheduled_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )

    class Meta:
        ordering = ['effective_date']

    def __str__(self):
        return f"{self.agent} → {self.new_role_type} on {self.effective_date}"

    @property
    def is_pending(self):
        return self.applied_at is None and self.cancelled_at is None


class RoleHistory(models.Model):
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name='role_history')
    role = models.CharField(max_length=20)
    role_type = models.CharField(max_length=20, blank=True)
    supervisor = models.ForeignKey(
        Agent, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    employer = models.CharField(max_length=20, blank=True)
    billing_status = models.CharField(max_length=20, blank=True)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    changed_by = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-effective_from', '-changed_at']

    def __str__(self):
        to = self.effective_to.strftime('%b %d, %Y') if self.effective_to else 'Present'
        return f"{self.agent} — {self.role} from {self.effective_from.strftime('%b %d, %Y')} to {to}"


class AgentRequest(models.Model):
    REQUEST_TYPE_CHOICES = [
        ('coding', 'Coding Request'),
        ('vacation', 'Vacation Request'),
        ('day_off_change', 'Day Off Change Request'),
        ('vto', 'VTO Request'),
        ('loa', 'LOA Request'),
        ('schedule_change', 'Schedule Change Request'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('done', 'Done'),
    ]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name='agent_requests')
    request_type = models.CharField(max_length=20, choices=REQUEST_TYPE_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    submitted_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    # Coding
    coding_date = models.DateField(null=True, blank=True)
    coding_start_time = models.TimeField(null=True, blank=True)
    coding_end_time = models.TimeField(null=True, blank=True)

    # Vacation
    vacation_start = models.DateField(null=True, blank=True)
    vacation_end = models.DateField(null=True, blank=True)

    # Day Off Change
    day_off_type = models.CharField(max_length=10, blank=True)  # 'one_time' or 'permanent'
    current_day_off = models.IntegerField(null=True, blank=True)   # 0=Mon..6=Sun
    requested_day_off = models.IntegerField(null=True, blank=True)
    effective_date = models.DateField(null=True, blank=True)

    # VTO
    vto_date = models.DateField(null=True, blank=True)

    # LOA
    loa_start = models.DateField(null=True, blank=True)
    loa_end = models.DateField(null=True, blank=True)

    # Schedule Change
    current_schedule_desc = models.TextField(blank=True)
    requested_schedule_desc = models.TextField(blank=True)
    schedule_new_start_time = models.TimeField(null=True, blank=True)
    schedule_new_end_time = models.TimeField(null=True, blank=True)
    schedule_change_days = models.JSONField(null=True, blank=True)  # list of int 0–6 (Mon–Sun)
    schedule_effective_date = models.DateField(null=True, blank=True)

    # Review
    reviewed_by = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='reviewed_agent_requests'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)

    # Done
    done_by = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='done_agent_requests'
    )
    done_at = models.DateTimeField(null=True, blank=True)

    # Audit
    auto_action_log = models.TextField(blank=True)

    # Notification flags: False = unread / needs attention
    supervisor_read = models.BooleanField(default=False)  # False when newly submitted
    agent_read = models.BooleanField(default=True)        # False when supervisor responds

    class Meta:
        ordering = ['-submitted_at']

    def __str__(self):
        return f"{self.agent} — {self.get_request_type_display()} ({self.submitted_at:%Y-%m-%d})"

    def summary(self):
        _days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        if self.request_type == 'coding' and self.coding_date:
            t1 = self.coding_start_time.strftime('%H:%M') if self.coding_start_time else '?'
            t2 = self.coding_end_time.strftime('%H:%M') if self.coding_end_time else '?'
            return f"{self.coding_date} · {t1}–{t2}"
        if self.request_type == 'vacation' and self.vacation_start:
            if not self.vacation_end or self.vacation_start == self.vacation_end:
                return str(self.vacation_start)
            return f"{self.vacation_start} – {self.vacation_end}"
        if self.request_type == 'day_off_change':
            cur = _days[self.current_day_off] if self.current_day_off is not None else '?'
            req = _days[self.requested_day_off] if self.requested_day_off is not None else '?'
            tag = ' (One-Time)' if self.day_off_type == 'one_time' else ' (Permanent)' if self.day_off_type == 'permanent' else ''
            return f"{cur} → {req}{tag}"
        if self.request_type == 'vto':
            return str(self.vto_date) if self.vto_date else ''
        if self.request_type == 'loa' and self.loa_start:
            return f"{self.loa_start} – {self.loa_end}" if self.loa_end else str(self.loa_start)
        if self.request_type == 'schedule_change':
            return (self.requested_schedule_desc or '')[:80]
        return (self.notes or '')[:60]


class AgentSeparation(models.Model):
    SEPARATION_TYPE_CHOICES = [
        ('quit', 'Quit (Voluntary)'),
        ('terminated', 'Terminated (Involuntary)'),
        ('abandonment', 'Job Abandonment'),
        ('contract_end', 'End of Contract'),
        ('resigned_notice', 'Resigned with Notice'),
    ]
    STATUS_CHOICES = [
        ('in_progress', 'In Progress'),
        ('finalized', 'Finalized'),
        ('cancelled', 'Cancelled'),
    ]
    # Maps separation type to EmploymentPeriod.reason_ended
    _EP_REASON_MAP = {
        'quit': 'resigned',
        'terminated': 'terminated',
        'abandonment': 'terminated',
        'contract_end': 'contract_end',
        'resigned_notice': 'resigned',
    }

    agent = models.ForeignKey(
        Agent, on_delete=models.CASCADE, related_name='separations'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='finalized')
    separation_type = models.CharField(max_length=20, choices=SEPARATION_TYPE_CHOICES)
    last_day_worked = models.DateField()
    remove_from_adherence_date = models.DateField(
        null=True, blank=True,
        help_text="Monday of first week agent no longer appears on Adherence"
    )
    notes = models.TextField(blank=True)
    processed_by = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='separations_processed'
    )
    processed_at = models.DateTimeField(auto_now_add=True)
    finalized_by = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='separations_finalized'
    )
    finalized_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-processed_at']

    def __str__(self):
        return f"{self.agent} — {self.get_separation_type_display()} (last day {self.last_day_worked})"
