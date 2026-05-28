from django.urls import path
from . import views

urlpatterns = [
    path('', views.erlang_calculator, name='erlang_calculator'),
    path('reports/', views.erlang_reports, name='erlang_reports'),
]
