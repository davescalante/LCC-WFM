from django import forms
from django.contrib.auth.models import User
from .models import Agent, Shift


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
        # Only super admins may grant the Admin Codings/Adherence permission.
        # When the editing user isn't a super admin we remove the field
        # entirely so it is neither rendered nor reset to False on save.
        can_grant_admin_tabs = kwargs.pop('can_grant_admin_tabs', False)
        super().__init__(*args, **kwargs)
        self.fields['supervisor'].queryset = Agent.objects.filter(
            role_type__in=('supervisor', 'coordinator')
        ).select_related('user').order_by('user__last_name', 'user__first_name')
        if not can_grant_admin_tabs:
            self.fields.pop('can_access_admin_tabs', None)
            self.fields.pop('can_manage_loans', None)
            # The Finance section (pay/billing rates, admin bonus) and the Super Admin
            # flag are super-admin-only. Strip them server-side so a non-super-admin can
            # neither see nor set them — not merely hidden (matches USER_EXPORT_FINANCIAL).
            self.fields.pop('hourly_rate', None)
            self.fields.pop('billing_rate_usd', None)
            self.fields.pop('admin_bonus_mxn', None)
            self.fields.pop('adherence_bonus_max_mxn', None)
            self.fields.pop('is_super_admin', None)

    class Meta:
        model = Agent
        fields = [
            'agent_name', 'employee_id', 'role', 'role_type', 'status', 'supervisor',
            'employer', 'billing_status', 'track_attendance',
            'phone_country_code', 'phone_number',
            'teams_password', 'hourly_rate', 'billing_rate_usd',
            'is_official_admin', 'admin_bonus_mxn', 'adherence_bonus_max_mxn', 'is_super_admin',
            'can_access_admin_tabs', 'can_manage_loans', 'adherence_start_date', 'notes',
        ]
        widgets = {
            'teams_password': forms.PasswordInput(render_value=True),
            'notes': forms.Textarea(attrs={'rows': 4}),
            'adherence_start_date': forms.DateInput(attrs={'type': 'date'}),
        }
        labels = {
            'agent_name': 'Agent Name',
            'employee_id': 'Employee ID',
            'teams_password': 'Teams Password',
            'phone_number': 'Phone Number',
            'employer': 'Employer',
            'billing_status': 'Billing Status',
            'track_attendance': 'Track Attendance',
            'hourly_rate': 'Hourly Rate ($)',
        }

    def clean_billing_rate_usd(self):
        val = self.cleaned_data.get('billing_rate_usd')
        if val is not None and val <= 0:
            raise forms.ValidationError("Billing rate must be greater than zero.")
        return val

    def clean_hourly_rate(self):
        val = self.cleaned_data.get('hourly_rate')
        if val is not None and val < 0:
            raise forms.ValidationError("Hourly rate cannot be negative.")
        return val

    def clean_adherence_start_date(self):
        val = self.cleaned_data.get('adherence_start_date')
        if val is not None and val.weekday() != 0:
            raise forms.ValidationError("Adherence start date must be a Monday.")
        return val


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
