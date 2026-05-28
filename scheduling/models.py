from django.db import models
from django.contrib.auth.models import User


class Agent(models.Model):
    ROLE_CHOICES = [
        ('agent', 'Agent'),
        ('supervisor', 'Supervisor'),
        ('coordinator', 'Coordinator'),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='agent')
    team = models.CharField(max_length=100, blank=True)
    phone_ext = models.CharField(max_length=10, blank=True)

    def __str__(self):
        return f"{self.user.get_full_name()} ({self.role})"


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
