"""
Views for the learner dashboard.
"""
from django.utils import timezone
from django.urls import reverse

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET
from edx_django_utils import monitoring as monitoring_utils

from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.edxmako.shortcuts import marketing_link
from common.djangoapps.student.helpers import get_resume_urls_for_enrollments
from common.djangoapps.student.views.dashboard import (
    complete_course_mode_info,
    get_course_enrollments,
    get_org_black_and_whitelist_for_site,
    get_filtered_course_entitlements,
)
from common.djangoapps.util.json_request import JsonResponse
from lms.djangoapps.bulk_email.models import Optout
from lms.djangoapps.bulk_email.models_api import is_bulk_email_feature_enabled
from lms.djangoapps.commerce.utils import EcommerceService
from lms.djangoapps.learner_home.serializers import LearnerDashboardSerializer, EntitlementSerializer
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers


def get_platform_settings():
    """Get settings used for platform level connections: emails, url routes, etc."""

    return {
        "supportEmail": settings.DEFAULT_FEEDBACK_EMAIL,
        "billingEmail": settings.PAYMENT_SUPPORT_EMAIL,
        "courseSearchUrl": marketing_link("COURSES"),
    }


def get_user_account_confirmation_info(user):
    """Determine if a user needs to verify their account and related URL info"""

    activation_email_support_link = (
        configuration_helpers.get_value(
            "ACTIVATION_EMAIL_SUPPORT_LINK", settings.ACTIVATION_EMAIL_SUPPORT_LINK
        )
        or settings.SUPPORT_SITE_LINK
    )

    email_confirmation = {
        "isNeeded": not user.is_active,
        "sendEmailUrl": activation_email_support_link,
    }

    return email_confirmation


def get_enrollments(user, org_allow_list, org_block_list, course_limit=None):
    """Get enrollments and enrollment course modes for user"""

    course_enrollments = list(
        get_course_enrollments(user, org_allow_list, org_block_list, course_limit)
    )

    # Sort the enrollments by enrollment date
    course_enrollments.sort(key=lambda x: x.created, reverse=True)

    # Record how many courses there are so that we can get a better
    # understanding of usage patterns on prod.
    monitoring_utils.accumulate("num_courses", len(course_enrollments))

    # Retrieve the course modes for each course
    enrolled_course_ids = [enrollment.course_id for enrollment in course_enrollments]
    __, unexpired_course_modes = CourseMode.all_and_unexpired_modes_for_courses(
        enrolled_course_ids
    )
    course_modes_by_course = {
        course_id: {mode.slug: mode for mode in modes}
        for course_id, modes in unexpired_course_modes.items()
    }

    # Construct a dictionary of course mode information
    # used to render the course list.  We re-use the course modes dict
    # we loaded earlier to avoid hitting the database.
    course_mode_info = {
        enrollment.course_id: complete_course_mode_info(
            enrollment.course_id,
            enrollment,
            modes=course_modes_by_course[enrollment.course_id],
        )
        for enrollment in course_enrollments
    }

    return course_enrollments, course_mode_info


def get_entitlements(user, org_allow_list, org_block_list):
    """Get entitlments for the user"""
    filtered_entitlements, course_entitlement_available_sessions, _ = get_filtered_course_entitlements(
        user, org_allow_list, org_block_list
    )

    course_entitlements = []

    for course_entitlement in filtered_entitlements:
        entitlement = {
            "availableSessions": course_entitlement_available_sessions[str(course_entitlement.uuid)],
            "isRefundable": course_entitlement.is_entitlement_refundable(),
            "isFulfilled": bool(course_entitlement.enrollment_course_run),
            "canViewCourse": None,  # unclear
            "changeDeadline": None,  # unclear
            "isExpired": course_entitlement.expired_at > timezone.now(),
            "expirationDate": course_entitlement.expired_at,
            "enrollUrl": reverse('entitlements_api:v1:enrollments', args=[str(course_entitlement.uuid)]),
            "leaveSessionURL": reverse('entitlements_api:v1:enrollments', args=[str(course_entitlement.uuid)]),
        }
        course_entitlements.append(EntitlementSerializer(entitlement).data)

    return course_entitlements


def get_email_settings_info(user, course_enrollments):
    """
    Given a user and enrollments, determine which courses allow bulk email (show_email_settings_for)
    and which the learner has opted out from (optouts)
    """
    course_optouts = Optout.objects.filter(user=user).values_list(
        "course_id", flat=True
    )

    # only show email settings for course where bulk email is turned on
    show_email_settings_for = frozenset(
        enrollment.course_id
        for enrollment in course_enrollments
        if (is_bulk_email_feature_enabled(enrollment.course_id))
    )

    return show_email_settings_for, course_optouts


def get_ecommerce_payment_page(user):
    """Determine the ecommerce payment page URL if enabled for this user"""
    ecommerce_service = EcommerceService()
    return (
        ecommerce_service.payment_page_url()
        if ecommerce_service.is_enabled(user)
        else None
    )


@login_required
@require_GET
def dashboard_view(request):  # pylint: disable=unused-argument
    """List of courses a user is enrolled in or entitled to"""

    # Get user, determine if user needs to confirm email account
    user = request.user
    email_confirmation = get_user_account_confirmation_info(user)

    # Get the org whitelist or the org blacklist for the current site
    site_org_whitelist, site_org_blacklist = get_org_black_and_whitelist_for_site()

    # TODO - Get entitlements (moving before enrollments because we use this to filter the enrollments)
    course_entitlements = get_entitlements(
        user, site_org_whitelist, site_org_blacklist
    )

    # Get enrollments
    course_enrollments, course_mode_info = get_enrollments(
        user, site_org_whitelist, site_org_blacklist
    )

    # Get email opt-outs for student
    show_email_settings_for, course_optouts = get_email_settings_info(
        user, course_enrollments
    )

    # TODO - Get verification status by course (do we still need this?)

    # TODO - Determine view access for courses (for showing courseware link or not)

    # TODO - Get related programs

    # TODO - Get user verification status

    # e-commerce info
    ecommerce_payment_page = get_ecommerce_payment_page(user)

    # Gather urls for course card resume buttons.
    resume_button_urls = get_resume_urls_for_enrollments(user, course_enrollments)

    learner_dash_data = {
        "emailConfirmation": email_confirmation,
        "enterpriseDashboards": None,
        "platformSettings": get_platform_settings(),
        "enrollments": course_enrollments,
        "entitlements": course_entitlements,
        "unfulfilledEntitlements": [],
        "suggestedCourses": [],
    }

    context = {
        "ecommerce_payment_page": ecommerce_payment_page,
        "course_mode_info": course_mode_info,
        "course_optouts": course_optouts,
        "resume_course_urls": resume_button_urls,
        "show_email_settings_for": show_email_settings_for,
    }

    response_data = LearnerDashboardSerializer(learner_dash_data, context=context).data
    return JsonResponse(response_data)
