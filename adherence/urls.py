from django.urls import path
from . import views

urlpatterns = [
    path('', views.adherence_week, name='adherence_dashboard'),
    path('codings/', views.codings_week, name='codings_week'),
    path('payroll/', views.payroll_export, name='payroll_export'),
]
