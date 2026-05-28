from django import forms
from django.contrib.auth.models import User
from .models import Agent, Shift, Break


class AgentUserForm(forms.ModelForm):
    first_name = forms.CharField(max_length=150, label="Legal First Name")
    last_name = forms.CharField(max_length=150, label="Legal Last Name")
    email = forms.EmailField()
    password = forms.CharField(
        widget=forms.PasswordInput, required=False,
        help_text="Leave blank to keep existing password."
    )

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email']


class AgentForm(forms.ModelForm):
    class Meta:
        model = Agent
        fields = [
            'agent_name', 'employee_id', 'role', 'supervisor',
            'start_date', 'phone_number',
            'five9_username', 'five9_password', 'teams_password',
        ]
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'five9_password': forms.PasswordInput(render_value=True),
            'teams_password': forms.PasswordInput(render_value=True),
        }
        labels = {
            'agent_name': 'Agent Name',
            'employee_id': 'Employee ID',
            'five9_username': 'Five9 Username',
            'five9_password': 'Five9 Password',
            'teams_password': 'Teams Password',
            'phone_number': 'Phone Number',
            'start_date': 'Start Date',
        }


class ShiftForm(forms.ModelForm):
    class Meta:
        model = Shift
        fields = ['agent', 'date', 'start_time', 'end_time', 'is_off', 'notes']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'start_time': forms.TimeInput(attrs={'type': 'time'}),
            'end_time': forms.TimeInput(attrs={'type': 'time'}),
            'notes': forms.Textarea(attrs={'rows': 3}),
        }


class BreakForm(forms.ModelForm):
    class Meta:
        model = Break
        fields = ['break_type', 'start_time', 'end_time']
        widgets = {
            'start_time': forms.TimeInput(attrs={'type': 'time'}),
            'end_time': forms.TimeInput(attrs={'type': 'time'}),
        }
