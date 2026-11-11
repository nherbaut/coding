from django.contrib.auth.decorators import user_passes_test


def admin_required(view):
    return user_passes_test(
        lambda user: user.is_active and user.is_staff, login_url="/admin/login/"
    )(view)
