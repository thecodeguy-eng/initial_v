from django.utils.functional import SimpleLazyObject

from .models import PlatformSettings, SupportChat


def platform_settings(request):
    """Expose Admin > Settings (min deposit/withdrawal, fees) to every template,
    so public pages can't drift from what the admin actually configured.
    Lazy, so pages that never mention it don't pay for the query."""
    return {'platform_settings': SimpleLazyObject(lambda: PlatformSettings.objects.first() or PlatformSettings())}


def admin_support_badge(request):
    """Unread-conversation count for the admin menu. Only runs for admins on admin pages."""
    user = getattr(request, 'user', None)
    if user and user.is_authenticated and (user.is_staff or user.is_superuser) and request.path.startswith('/custom-admin'):
        return {'admin_unread_chats': SupportChat.objects.filter(
            messages__is_read=False, messages__sender_type='USER').distinct().count()}
    return {}
