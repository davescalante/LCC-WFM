from django.contrib import admin
from .models import Agent, Shift, Break

@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'employee_id', 'role', 'supervisor', 'start_date', 'phone_number']
    list_filter = ['role']
    search_fields = ['user__first_name', 'user__last_name', 'agent_name', 'employee_id']

@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = ['agent', 'date', 'start_time', 'end_time', 'is_off']
    list_filter = ['date', 'is_off']

@admin.register(Break)
class BreakAdmin(admin.ModelAdmin):
    list_display = ['shift', 'break_type', 'start_time', 'end_time']
