from django.urls import path
from . import views

urlpatterns = [
    path('', views.erlang_calculator, name='erlang_calculator'),
    path('download/', views.erlang_download, name='erlang_download'),
    path('reports/', views.erlang_reports, name='erlang_reports'),
]
