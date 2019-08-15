# -*- coding: utf-8 -*-
import logging
import sys
from functools import wraps

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.mail import get_connection
from django.core.mail.message import EmailMultiAlternatives
from django.db.models import Q
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.utils.translation import ugettext as _
from django.utils.translation import override

from wagtail.admin.navigation import get_explorable_root_page
from wagtail.core.models import GroupPagePermission, PageRevision
from wagtail.users.models import UserProfile
from wagtail.utils.deprecation import MovedDefinitionHandler, RemovedInWagtail29Warning

logger = logging.getLogger('wagtail.admin')


def users_with_page_permission(page, permission_type, include_superusers=True):
    # Get user model
    User = get_user_model()

    # Find GroupPagePermission records of the given type that apply to this page or an ancestor
    ancestors_and_self = list(page.get_ancestors()) + [page]
    perm = GroupPagePermission.objects.filter(permission_type=permission_type, page__in=ancestors_and_self)
    q = Q(groups__page_permissions__in=perm)

    # Include superusers
    if include_superusers:
        q |= Q(is_superuser=True)

    return User.objects.filter(is_active=True).filter(q).distinct()


def permission_denied(request):
    """Return a standard 'permission denied' response"""
    if request.is_ajax():
        raise PermissionDenied

    from wagtail.admin import messages

    messages.error(request, _('Sorry, you do not have permission to access this area.'))
    return redirect('wagtailadmin_home')


def user_passes_test(test):
    """
    Given a test function that takes a user object and returns a boolean,
    return a view decorator that denies access to the user if the test returns false.
    """
    def decorator(view_func):
        # decorator takes the view function, and returns the view wrapped in
        # a permission check

        @wraps(view_func)
        def wrapped_view_func(request, *args, **kwargs):
            if test(request.user):
                # permission check succeeds; run the view function as normal
                return view_func(request, *args, **kwargs)
            else:
                # permission check failed
                return permission_denied(request)

        return wrapped_view_func

    return decorator


def permission_required(permission_name):
    """
    Replacement for django.contrib.auth.decorators.permission_required which returns a
    more meaningful 'permission denied' response than just redirecting to the login page.
    (The latter doesn't work anyway because Wagtail doesn't define LOGIN_URL...)
    """
    def test(user):
        return user.has_perm(permission_name)

    # user_passes_test constructs a decorator function specific to the above test function
    return user_passes_test(test)


def any_permission_required(*perms):
    """
    Decorator that accepts a list of permission names, and allows the user
    to pass if they have *any* of the permissions in the list
    """
    def test(user):
        for perm in perms:
            if user.has_perm(perm):
                return True

        return False

    return user_passes_test(test)


class PermissionPolicyChecker:
    """
    Provides a view decorator that enforces the given permission policy,
    returning the wagtailadmin 'permission denied' response if permission not granted
    """
    def __init__(self, policy):
        self.policy = policy

    def require(self, action):
        def test(user):
            return self.policy.user_has_permission(user, action)

        return user_passes_test(test)

    def require_any(self, *actions):
        def test(user):
            return self.policy.user_has_any_permission(user, actions)

        return user_passes_test(test)


def send_mail(subject, message, recipient_list, from_email=None, **kwargs):
    """
    Wrapper around Django's EmailMultiAlternatives as done in send_mail().
    Custom from_email handling and special Auto-Submitted header.
    """
    if not from_email:
        if hasattr(settings, 'WAGTAILADMIN_NOTIFICATION_FROM_EMAIL'):
            from_email = settings.WAGTAILADMIN_NOTIFICATION_FROM_EMAIL
        elif hasattr(settings, 'DEFAULT_FROM_EMAIL'):
            from_email = settings.DEFAULT_FROM_EMAIL
        else:
            from_email = 'webmaster@localhost'

    connection = kwargs.get('connection', False) or get_connection(
        username=kwargs.get('auth_user', None),
        password=kwargs.get('auth_password', None),
        fail_silently=kwargs.get('fail_silently', None),
    )
    multi_alt_kwargs = {
        'connection': connection,
        'headers': {
            'Auto-Submitted': 'auto-generated',
        }
    }
    mail = EmailMultiAlternatives(subject, message, from_email, recipient_list, **multi_alt_kwargs)
    html_message = kwargs.get('html_message', None)
    if html_message:
        mail.attach_alternative(html_message, 'text/html')

    return mail.send()


def send_notification(page_revision_id, notification, excluded_user_id):
    # Get revision
    revision = PageRevision.objects.get(id=page_revision_id)

    # Get list of recipients
    if notification == 'submitted':
        # Get list of publishers
        include_superusers = getattr(settings, 'WAGTAILADMIN_NOTIFICATION_INCLUDE_SUPERUSERS', True)
        recipients = users_with_page_permission(revision.page, 'publish', include_superusers)
    elif notification in ['rejected', 'approved']:
        # Get submitter
        recipients = [revision.user]
    else:
        return False

    # Get list of email addresses
    email_recipients = [
        recipient for recipient in recipients
        if recipient.email and recipient.pk != excluded_user_id and getattr(
            UserProfile.get_for_user(recipient),
            notification + '_notifications'
        )
    ]

    # Return if there are no email addresses
    if not email_recipients:
        return True

    # Get template
    template_subject = 'wagtailadmin/notifications/' + notification + '_subject.txt'
    template_text = 'wagtailadmin/notifications/' + notification + '.txt'
    template_html = 'wagtailadmin/notifications/' + notification + '.html'

    # Common context to template
    context = {
        "revision": revision,
        "settings": settings,
    }

    # Send emails
    sent_count = 0
    for recipient in email_recipients:
        try:
            # update context with this recipient
            context["user"] = recipient

            # Translate text to the recipient language settings
            with override(recipient.wagtail_userprofile.get_preferred_language()):
                # Get email subject and content
                email_subject = render_to_string(template_subject, context).strip()
                email_content = render_to_string(template_text, context).strip()

            kwargs = {}
            if getattr(settings, 'WAGTAILADMIN_NOTIFICATION_USE_HTML', False):
                kwargs['html_message'] = render_to_string(template_html, context)

            # Send email
            send_mail(email_subject, email_content, [recipient.email], **kwargs)
            sent_count += 1
        except Exception:
            logger.exception(
                "Failed to send notification email '%s' to %s",
                email_subject, recipient.email
            )

    return sent_count == len(email_recipients)


def user_has_any_page_permission(user):
    """
    Check if a user has any permission to add, edit, or otherwise manage any
    page.
    """
    # Can't do nothin if you're not active.
    if not user.is_active:
        return False

    # Superusers can do anything.
    if user.is_superuser:
        return True

    # At least one of the users groups has a GroupPagePermission.
    # The user can probably do something.
    if GroupPagePermission.objects.filter(group__in=user.groups.all()).exists():
        return True

    # Specific permissions for a page type do not mean anything.

    # No luck! This user can not do anything with pages.
    return False


def get_site_for_user(user):
    root_page = get_explorable_root_page(user)
    if root_page:
        root_site = root_page.get_site()
    else:
        root_site = None
    real_site_name = None
    if root_site:
        real_site_name = root_site.site_name if root_site.site_name else root_site.hostname
    return {
        'root_page': root_page,
        'root_site': root_site,
        'site_name': real_site_name if real_site_name else settings.WAGTAIL_SITE_NAME,
    }


MOVED_DEFINITIONS = {
    'WAGTAILADMIN_PROVIDED_LANGUAGES': 'wagtail.admin.locale',
    'get_js_translation_strings': 'wagtail.admin.locale',
    'get_available_admin_languages': 'wagtail.admin.locale',
    'get_available_admin_time_zones': 'wagtail.admin.locale',

    'get_object_usage': 'wagtail.admin.models',
    'popular_tags_for_model': 'wagtail.admin.models',
}

sys.modules[__name__] = MovedDefinitionHandler(sys.modules[__name__], MOVED_DEFINITIONS, RemovedInWagtail29Warning)