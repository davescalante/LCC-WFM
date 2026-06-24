from django.db import models


class ErlangActualStaff(models.Model):
    week_start = models.DateField()
    day = models.CharField(max_length=10)
    hour = models.IntegerField()
    actual_agents = models.IntegerField()

    class Meta:
        unique_together = ('week_start', 'day', 'hour')
        ordering = ['week_start', 'day', 'hour']

    def __str__(self):
        return f"{self.week_start} {self.day} {self.hour}:00 — {self.actual_agents} agents"


class ErlangCallRow(models.Model):
    """Parsed Five9 CSV data for one week — shared across all users."""
    week_start = models.DateField()
    day = models.CharField(max_length=10)
    hour = models.IntegerField()
    total_calls = models.FloatField()
    avg_calls = models.FloatField()

    class Meta:
        unique_together = ('week_start', 'day', 'hour')
        ordering = ['week_start', 'day', 'hour']

    def __str__(self):
        return f"{self.week_start} {self.day} {self.hour}:00 — {self.avg_calls} avg calls"


class ErlangWeekParams(models.Model):
    """Calculation parameters for one week — shared across all users."""
    week_start = models.DateField(unique=True)
    target_sl = models.FloatField(default=80)
    target_seconds = models.IntegerField(default=20)
    shrinkage = models.FloatField(default=0)
    aht_seconds = models.IntegerField(default=420)
    weeks = models.IntegerField(default=3)
    weeks_by_day = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)
    calculated_by = models.ForeignKey(
        'auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='erlang_calculations',
    )
    csv_uploaded_at = models.DateTimeField(null=True, blank=True)
    csv_uploaded_by = models.ForeignKey(
        'auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='erlang_uploads',
    )

    class Meta:
        ordering = ['-week_start']

    def __str__(self):
        return f"{self.week_start} — {self.weeks}w AHT={self.aht_seconds}s SL={self.target_sl}%"


class ErlangReport(models.Model):
    name = models.CharField(max_length=200)
    week_start = models.DateField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    calls_per_hour = models.FloatField()
    avg_handle_time = models.FloatField(help_text="Average handle time in seconds")
    target_service_level = models.FloatField(help_text="Target service level as percentage (e.g. 80)")
    target_answer_time = models.IntegerField(help_text="Target answer time in seconds (e.g. 20)")
    shrinkage = models.FloatField(default=0, help_text="Shrinkage percentage (e.g. 30 for 30%)")
    agents_required = models.IntegerField(null=True, blank=True)
    agents_scheduled = models.IntegerField(null=True, blank=True, help_text="Agents needed after applying shrinkage")
    service_level_achieved = models.FloatField(null=True, blank=True)
    occupancy = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} - {self.created_at.strftime('%Y-%m-%d')}"
