from django.urls import path
from django.contrib.auth.views import LogoutView
from .views import (
    CustomLoginView, HomeView, ProfileView, CustomPasswordChangeView,
    UserListView, UserCreateView, UserUpdateView, UserDeleteView,
    SchoolListView, SchoolCreateView, SchoolUpdateView, SchoolDeleteView
)

urlpatterns = [
    path('login/', CustomLoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('home/', HomeView.as_view(), name='home'),
    path('profile/', ProfileView.as_view(), name='profile'),
    path('password-change/', CustomPasswordChangeView.as_view(), name='password_change'),
    
    # User CRUD
    path('users/', UserListView.as_view(), name='user_list'),
    path('users/add/', UserCreateView.as_view(), name='user_add'),
    path('users/<int:pk>/edit/', UserUpdateView.as_view(), name='user_edit'),
    path('users/<int:pk>/delete/', UserDeleteView.as_view(), name='user_delete'),

    # School CRUD
    path('schools/', SchoolListView.as_view(), name='school_list'),
    path('schools/add/', SchoolCreateView.as_view(), name='school_add'),
    path('schools/<int:pk>/edit/', SchoolUpdateView.as_view(), name='school_edit'),
    path('schools/<int:pk>/delete/', SchoolDeleteView.as_view(), name='school_delete'),
]
