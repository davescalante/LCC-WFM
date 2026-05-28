from datetime import datetime, date as date_type, timedelta
from decimal import Decimal
from django.db import models
from scheduling.models import Shift, Agent


class AdherenceRecord(models.Model):
    STATUS_CHOICES = [
        ('P',       'P — Present'),
        ('T',       'T — Tardy'),
        ('Absent',  'Absent'),
        ('NCNS',    'NCNS — No Call No Show'),
        ('VTO',     'VTO — Voluntary Time Off'),
        ('IMSS',    'IMSS — Medical/IMSS Leave'),
        ('MUT',     'MUT — Make Up Time'),
        ('OT',      'OT — Over Time'),
        ('P+VTO',   'P+VTO — Present then VTO'),
        ('T+VTO',   'T+VTO — Tardy then VTO'),
        ('I',       'I — Incomplete Shift'),
        ('Quit',    'Quit'),
        ('Baja',    'Baja — Terminated'),
        ('V',       'V — Vacation'),
    ]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name='adherence_records')
    date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, blank=True)
    actual_hours = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('agent', 'date')
        ordering = ['date', 'agent']

    def __str__(self):
        return f"{self.agent} — {self.date} — {self.status}"


class Coding(models.Model):
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name='codings')
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'agent', 'start_time']

    def total_seconds_count(self):
        start = datetime.combine(date_type.today(), self.start_time)
        end = datetime.combine(date_type.today(), self.end_time)
        return max(0, int((end - start).total_seconds()))

    def total_minutes(self):
        return self.total_seconds_count() // 60

    def total_hours(self):
        return round(self.total_seconds_count() / 3600, 6)

    def total_hhmmss(self):
        secs = self.total_seconds_count()
        h = secs // 3600
        m = (secs % 3600) // 60
        s = secs % 60
        return f'{h:02d}:{m:02d}:{s:02d}'

    def __str__(self):
        return f"{self.agent} — {self.date} — {self.start_time}–{self.end_time}"


class DailyUpload(models.Model):
    """One uploaded Five9 CSV file per day."""
    date = models.DateField(unique=True)
    uploaded_at = models.DateTimeField(auto_now=True)
    filename = models.CharField(max_length=255, blank=True)
    row_count = models.IntegerField(default=0)
    unmatched_count = models.IntegerField(default=0)

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f"Five9 upload — {self.date} ({self.row_count} agents)"


class DailyAgentHours(models.Model):
    """One row from a Five9 daily CSV: one agent's login/not-ready time for one day."""
    upload = models.ForeignKey(DailyUpload, on_delete=models.CASCADE, related_name='rows')
    agent = models.ForeignKey(
        Agent, on_delete=models.SET_NULL, null=True, blank=True, related_name='daily_hours'
    )
    five9_username = models.CharField(max_length=100)
    agent_group = models.CharField(max_length=100, blank=True)
    login_seconds = models.IntegerField(default=0)
    not_ready_seconds = models.IntegerField(default=0)

    class Meta:
        ordering = ['five9_username']
        unique_together = ('upload', 'five9_username')

    def __str__(self):
        return f"{self.five9_username} — {self.upload.date}"


class PayrollAdjustment(models.Model):
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name='payroll_adjustments')
    week_start = models.DateField()
    commission_deduction = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    notes = models.TextField(blank=True)

    class Meta:
        unique_together = ('agent', 'week_start')

    def __str__(self):
        return f"{self.agent} — {self.week_start} — ${self.commission_deduction}"
