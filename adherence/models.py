from django.db import models
from scheduling.models import Shift


class AdherenceRecord(models.Model):
    STATUS_CHOICES = [
        ('on_time', 'On Time'),
        ('late', 'Late'),
        ('absent', 'Absent'),
        ('early_leave', 'Left Early'),
        ('extended_break', 'Extended Break'),
        ('unscheduled_break', 'Unscheduled Break'),
    ]
    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name='adherence_records')
    timestamp = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    actual_start = models.TimeField(null=True, blank=True)
    actual_end = models.TimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.shift.agent} - {self.status} on {self.shift.date}"

    @property
    def adherence_percentage(self):
        if not self.actual_start or not self.actual_end:
            return None
        from datetime import datetime, date
        scheduled_start = datetime.combine(date.today(), self.shift.start_time)
        scheduled_end = datetime.combine(date.today(), self.shift.end_time)
        actual_start = datetime.combine(date.today(), self.actual_start)
        actual_end = datetime.combine(date.today(), self.actual_end)
        scheduled_mins = (scheduled_end - scheduled_start).seconds / 60
        on_schedule_mins = max(0, (min(actual_end, scheduled_end) - max(actual_start, scheduled_start)).seconds / 60)
        return round((on_schedule_mins / scheduled_mins) * 100, 1) if scheduled_mins > 0 else 0
