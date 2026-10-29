from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, AuditLog

class CustomUserAdmin(UserAdmin):
    model = User
    list_display = ['username', 'first_name', 'phone_number', 'role', 'is_staff']
    fieldsets = UserAdmin.fieldsets + (
        (None, {'fields': ('phone_number', 'role')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        (None, {'fields': ('phone_number', 'role')}),
    )

admin.site.register(User, CustomUserAdmin)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'user', 'action', 'model_name', 'object_id', 'object_repr']
    list_filter = ['action', 'model_name']
    search_fields = ['object_repr', 'object_id']
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
