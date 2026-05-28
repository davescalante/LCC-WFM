from django import forms
from django.contrib.auth.models import User
from .models import Agent, Shift, Break


class AgentUserForm(forms.ModelForm):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput, required=False,
                               help_text="Leave blank to keep existing password.")

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-input'


class AgentForm(forms.ModelForm):
    class Meta:
        model = Agent
        fields = ['role', 'team', 'phone_ext']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-input'


class ShiftForm(forms.ModelForm):
    class Meta:
        model = Shift
        fields = ['agent', 'date', 'start_time', 'end_time', 'is_off', 'notes']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date', 'class': 'form-input'}),
            'start_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-input'}),
            'end_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-input'}),
            'notes': forms.Textarea(attrs={'rows': 3, 'class': 'form-input'}),
            'agent': forms.Select(attrs={'class': 'form-input'}),
            'is_off': forms.CheckboxInput(),
        }


class BreakForm(forms.ModelForm):
    class Meta:
        model = Break
        fields = ['break_type', 'start_time', 'end_time']
        widgets = {
            'start_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-input'}),
            'end_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-input'}),
            'break_type': forms.Select(attrs={'class': 'form-input'}),
        }
