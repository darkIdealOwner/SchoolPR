from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model

from .phone import normalize_phone

User = get_user_model()

class PhoneBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        # We use 'username' as the field for phone_number in the login form
        phone_number = normalize_phone(username or kwargs.get('phone_number'))
        try:
            user = User.objects.get(phone_number=phone_number)
            if user.check_password(password):
                return user
        except User.DoesNotExist:
            return None
        return None
