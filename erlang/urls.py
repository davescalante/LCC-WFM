from django.urls import path
from . import views

urlpatterns = [
    path('', views.erlang_calculator, name='erlang_calculator'),
    path('download/', views.erlang_download, name='erlang_download'),
    path('save-actual/', views.erlang_save_actual, name='erlang_save_actual'),
    path('reports/', views.erlang_reports, name='erlang_reports'),
]
