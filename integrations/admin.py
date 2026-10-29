from django.contrib import admin

from .models import SyncRun


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ['started_at', 'entity', 'channel', 'status', 'dry_run', 'total', 'created', 'updated', 'reversed', 'failed']
    list_filter = ['entity', 'channel', 'status', 'dry_run']
    readonly_fields = [f.name for f in SyncRun._meta.fields]

    def has_add_permission(self, request):
        return False
