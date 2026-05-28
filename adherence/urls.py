from django.urls import path
from . import views

urlpatterns = [
    path('', views.adherence_dashboard, name='adherence_dashboard'),
    path('log/', views.log_adherence, name='log_adherence'),
]
