from django.contrib.auth import get_user_model
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from audit.events import log_event
from audit.models import AuditAction

from .permissions import IsSuperAdmin
from .serializers import (
    ChangePasswordSerializer,
    RoleTokenObtainPairSerializer,
    UserCreateSerializer,
    UserSerializer,
    UserUpdateSerializer,
)

User = get_user_model()


class LoginView(TokenObtainPairView):
    """Obtain an access/refresh JWT pair. Throttled against brute force."""

    serializer_class = RoleTokenObtainPairSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"
    permission_classes = []  # public by design
    authentication_classes = []

    def post(self, request, *args, **kwargs):
        raw_username = request.data.get("username", "") if hasattr(request.data, "get") else ""
        username = str(raw_username)[:150]
        try:
            response = super().post(request, *args, **kwargs)
        except AuthenticationFailed:
            log_event(request, AuditAction.LOGIN_FAILED, actor_username=username)
            raise
        user = User.objects.filter(username=username).first()
        if user is not None:
            # .update() so a login does not create a change-history row for the user.
            User.objects.filter(pk=user.pk).update(last_login=timezone.now())
        log_event(request, AuditAction.LOGIN_SUCCESS, actor=user, actor_username=username)
        return response


class UserViewSet(viewsets.ModelViewSet):
    """User administration - Super Administrator only."""

    permission_classes = [IsSuperAdmin]
    queryset = User.objects.all().order_by("username")
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        if self.action in ("update", "partial_update"):
            return UserUpdateSerializer
        return UserSerializer

    def perform_destroy(self, instance):
        # Never hard-delete: payroll history references users. Deactivate instead.
        if instance.pk == self.request.user.pk:
            from rest_framework.exceptions import ValidationError

            raise ValidationError("You cannot deactivate your own account.")
        instance.is_active = False
        instance.save(update_fields=["is_active"])


class MeView(APIView):
    """The authenticated user's own account."""

    @extend_schema(responses=UserSerializer)
    def get(self, request):
        return Response(UserSerializer(request.user).data)


class ChangePasswordView(APIView):
    @extend_schema(request=ChangePasswordSerializer, responses={204: None})
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_event(request, AuditAction.PASSWORD_CHANGED, target=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)
