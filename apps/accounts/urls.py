from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    LoginView,
    LogoutView,
    MeView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
)

urlpatterns = [
    path("login", LoginView.as_view(), name="auth_login"),
    path("refresh", TokenRefreshView.as_view(), name="auth_refresh"),
    path("logout", LogoutView.as_view(), name="auth_logout"),
    path("me", MeView.as_view(), name="auth_me"),
    path("password-reset", PasswordResetRequestView.as_view(), name="auth_password_reset"),
    path(
        "password-reset/confirm",
        PasswordResetConfirmView.as_view(),
        name="auth_password_reset_confirm",
    ),
]
