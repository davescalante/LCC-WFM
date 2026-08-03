from django.urls import path

from . import views

app_name = 'nomina'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('inputs/', views.inputs, name='inputs'),
    path('agents/', views.agent_nomina, name='agent_nomina'),
    path('admins/', views.admin_nomina, name='admin_nomina'),
    path('break-abuse/', views.break_abuse, name='break_abuse'),
    path('holidays/', views.holidays, name='holidays'),
]
