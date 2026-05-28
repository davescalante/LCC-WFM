from django import forms
from django.contrib.auth.models import User
from .models import Agent, Shift, Break


class AgentUserForm(forms.ModelForm):
    legal_name = forms.CharField(max_length=300, label="Legal Name", help_text="Enter full legal name")
    email = forms.EmailField()
    password = forms.CharField(
        widget=forms.PasswordInput, required=False,
        help_text="Leave blank to keep existing password."
    )

    class Meta:
        model = User
        fields = ['username', 'email']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            full_name = f"{self.instance.first_name} {self.instance.last_name}".strip()
            self.fields['legal_name'].initial = full_name

    def save(self, commit=True):
        user = super().save(commit=False)
        legal_name = self.cleaned_data.get('legal_name', '').strip()
        parts = legal_name.split(' ', 1)
        user.first_name = parts[0]
        user.last_name = parts[1] if len(parts) > 1 else ''
        if commit:
            user.save()
        return user


class AgentForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['supervisor'].queryset = Agent.objects.filter(
            role_type='supervisor'
        ).select_related('user').order_by('user__last_name', 'user__first_name')

    class Meta:
        model = Agent
        fields = [
            'agent_name', 'employee_id', 'role', 'role_type', 'status', 'supervisor',
            'phone_country_code', 'phone_number',
            'teams_password', 'notes',
        ]
        widgets = {
            'teams_password': forms.PasswordInput(render_value=True),
            'notes': forms.Textarea(attrs={'rows': 4}),
        }
        labels = {
            'agent_name': 'Agent Name',
            'employee_id': 'Employee ID',
            'teams_password': 'Teams Password',
            'phone_number': 'Phone Number',
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
