from django.contrib import admin
from .models import Agent, Shift, Break

@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'role', 'team', 'phone_ext']
    list_filter = ['role', 'team']

@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = ['agent', 'date', 'start_time', 'end_time', 'is_off']
    list_filter = ['date', 'is_off']

@admin.register(Break)
class BreakAdmin(admin.ModelAdmin):
    list_display = ['shift', 'break_type', 'start_time', 'end_time']
