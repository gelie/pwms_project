import asyncio
import json

# import logging
import os
from datetime import timedelta

import httpx
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.core.mail import send_mail

from .models import (
    Attachment,
    AuditLog,
    Comment,
    Drive,
    Event,
    EventType,
    Group,
    GroupMembership,
    Notification,
    Role,
    SharePointFolder,
    SharePointToken,
    Site,
    SiteMember,
    State,
    Transition,
    User,
    UserDelegation,
    Venue,
    Workflow,
    WorkflowReferral,
    WorkflowTransitionLog,
    WorkflowType,
    WorkflowTypeChildConfig,
    WorkflowTypeReferralConfig,
)
from .epetition import post_status_update
from .notifications import notify_workflow_created
from .sharepoint import (
    get_application_token,
    get_drive_items,
    get_folder_items,
    get_site_drives,
    upload_file,
)


# HTMX Partial Rendering Decorator
def htmx_partial(template_name):
    """
    Decorator that enables HTMX partial rendering for views.
    Returns template partial for HTMX requests, full template otherwise.
    """
    from functools import wraps

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            # Call the original view function
            response = view_func(request, *args, **kwargs)

            # If the view returned a response (not a dict), return it as-is
            if not isinstance(response, dict):
                return response

            # Handle HTMX partial rendering
            if request.headers.get("HX-Request"):
                # HTMX request - return only the content partial
                partial_template = f"{template_name}#content"
                return render(request, partial_template, response)
            else:
                # Regular request - return full page
                return render(request, template_name, response)

        return wrapper

    return decorator


# ============================================================================
# AUTHENTICATION VIEWS
# ============================================================================


@htmx_partial("workflows/login.html")
def login_view(request):
    """User login view"""
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            messages.success(
                request, f"Welcome back, {user.get_full_name() or user.username}!"
            )
            return redirect("dashboard")
        else:
            messages.error(request, "Invalid username or password.")
    return {}
    # return render(request, "workflows/login.html")


def logout_view(request):
    """User logout view"""
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect("login")


# ============================================================================
# DASHBOARD VIEW
# ============================================================================


@login_required
@htmx_partial("workflows/dashboard.html")
def dashboard(request):
    """Main dashboard view with workflow overview"""
    user = request.user

    # Get user's groups
    user_groups = user.memberships.filter(is_active=True).values_list(
        "group", flat=True
    )

    # Get workflows user can view
    if user.is_superuser:
        # Superusers can see all workflows on dashboard
        workflows = Workflow.objects.all().select_related(
            "workflow_type", "current_state", "owner"
        )
    else:
        # Use new RBAC system: workflows where user's groups have access
        # OR legacy system: workflows from user's groups or referred to user's groups
        workflows = (
            Workflow.objects.filter(
                Q(group_access__group__in=user_groups)
                | Q(workflow_type__group__in=user_groups)
                | Q(referred_to_groups__in=user_groups)
            )
            .distinct()
            .select_related("workflow_type", "current_state", "owner")
        )

    # Statistics
    total_workflows = workflows.count()
    my_workflows = workflows.filter(owner=user).count()
    assigned_to_me = workflows.filter(assigned_to=user).count()

    # New workflows (is_initial state)
    new_workflows = workflows.filter(current_state__is_initial=True).count()

    # Active workflows (not terminal)
    active = workflows.filter(
        current_state__is_terminal=False, current_state__is_initial=False
    ).count()

    # Completed workflows (terminal states)
    completed = workflows.filter(current_state__is_terminal=True).count()

    # Workflows created this month
    now = timezone.now()
    this_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    created_this_month = workflows.filter(created_at__gte=this_month_start).count()

    # Overdue workflows - count all workflows past deadline
    overdue_workflows = workflows.filter(
        deadline__lt=timezone.now(), current_state__is_terminal=False
    ).count()

    # Workflows by priority
    urgent_count = workflows.filter(priority="urgent").count()
    high_count = workflows.filter(priority="high").count()

    # Recent workflows
    recent_workflows = workflows.order_by("-created_at")[:10]

    # Upcoming events
    if user.is_superuser:
        upcoming_events = (
            Event.objects.filter(start_datetime__gte=timezone.now())
            .exclude(status="cancelled")
            .order_by("start_datetime")[:5]
        )
    else:
        upcoming_events = (
            Event.objects.filter(
                group__in=user_groups, start_datetime__gte=timezone.now()
            )
            .exclude(status="cancelled")
            .order_by("start_datetime")[:5]
        )

    # Workflows by state (for chart) - with grouped state info
    workflows_by_state_raw = workflows.values(
        "current_state__name",
        "current_state__color",
        "current_state__is_initial",
        "current_state__is_terminal",
    ).annotate(count=Count("id"))

    # Group states for pie chart
    state_new_count = 0
    state_completed_count = 0
    state_in_progress_count = 0

    for item in workflows_by_state_raw:
        if item.get("current_state__is_initial"):
            state_new_count += item["count"]
        elif item.get("current_state__is_terminal"):
            state_completed_count += item["count"]
        else:
            state_in_progress_count += item["count"]

    workflows_by_state_grouped = [
        {"name": "New", "count": state_new_count, "color": "#3b82f6"},
        {"name": "In Progress", "count": state_in_progress_count, "color": "#f59e0b"},
        {"name": "Completed", "count": state_completed_count, "color": "#10b981"},
    ]

    # Workflows by type - with cumulative angles for pie chart
    workflows_by_type_raw = (
        workflows.values("workflow_type__name")
        .annotate(count=Count("id"))
        .order_by("workflow_type__name")
    )

    # Calculate angles and cumulative positions for pie chart
    # Deduplicate by workflow type name
    workflows_by_type_dict = {}
    for item in workflows_by_type_raw:
        type_name = item["workflow_type__name"]
        if type_name in workflows_by_type_dict:
            workflows_by_type_dict[type_name]["count"] += item["count"]
        else:
            workflows_by_type_dict[type_name] = {
                "workflow_type__name": type_name,
                "count": item["count"],
            }

    workflows_by_type_list = list(workflows_by_type_dict.values())
    cumulative = 0
    colors = [
        "#3b82f6",
        "#10b981",
        "#f59e0b",
        "#ef4444",
        "#8b5cf6",
        "#ec4899",
        "#14b8a6",
        "#f97316",
    ]

    for i, item in enumerate(workflows_by_type_list):
        item["color"] = colors[i % len(colors)]
        item["start_angle"] = (
            int((cumulative / total_workflows) * 360) if total_workflows > 0 else 0
        )
        cumulative += item["count"]
        item["end_angle"] = (
            int((cumulative / total_workflows) * 360) if total_workflows > 0 else 0
        )

    context = {
        "total_workflows": total_workflows,
        "my_workflows": my_workflows,
        "assigned_to_me": assigned_to_me,
        "new_workflows": new_workflows,
        "active": active,
        "completed": completed,
        "created_this_month": created_this_month,
        "overdue_workflows": overdue_workflows,
        "urgent_count": urgent_count,
        "high_count": high_count,
        "recent_workflows": recent_workflows,
        "upcoming_events": upcoming_events,
        "workflows_by_state": workflows_by_state_grouped,
        "workflows_by_type": workflows_by_type_list,
    }

    return context


# ============================================================================
# WORKFLOW VIEWS
# ============================================================================


@login_required
@htmx_partial("workflows/workflow_list.html")
def workflow_list(request):
    """List all workflows user can access"""
    user = request.user
    user_groups = user.memberships.filter(is_active=True).values_list(
        "group", flat=True
    )

    # Base queryset - show only top-level parent workflows
    if user.is_superuser:
        # Superusers can see all workflows
        workflows = Workflow.objects.filter(
            parent_workflow__isnull=True,
        ).select_related(
            "workflow_type",
            "current_state",
            "owner",
            "assigned_to",
            "parent_workflow",
        )
    else:
        # Use new RBAC system with fallback to legacy
        workflows = (
            Workflow.objects.filter(
                Q(group_access__group__in=user_groups)
                | Q(workflow_type__group__in=user_groups)
                | Q(referred_to_groups__in=user_groups),
                parent_workflow__isnull=True,
            )
            .distinct()
            .select_related(
                "workflow_type",
                "current_state",
                "owner",
                "assigned_to",
                "parent_workflow",
            )
        )

    # Filters
    workflow_type = request.GET.get("type")
    state = request.GET.get("state")
    priority = request.GET.getlist("priority")
    group = request.GET.get("group")
    search = request.GET.get("search")

    if workflow_type:
        workflows = workflows.filter(workflow_type_id=workflow_type)
    if state:
        workflows = workflows.filter(current_state_id=state)
    if priority:
        workflows = workflows.filter(priority__in=priority)
    if group:
        workflows = workflows.filter(workflow_type__group_id=group)
    if search:
        workflows = workflows.filter(
            Q(title__icontains=search) | Q(description__icontains=search)
        )

    # For filter dropdowns — scoped to what the user can actually see
    accessible_type_ids = workflows.values_list(
        "workflow_type_id", flat=True
    ).distinct()
    accessible_state_ids = workflows.values_list(
        "current_state_id", flat=True
    ).distinct()
    workflow_types = WorkflowType.objects.filter(id__in=accessible_type_ids).order_by(
        "name"
    )
    states = State.objects.filter(id__in=accessible_state_ids).order_by("name")
    groups = (
        Group.objects.all()
        if user.is_superuser
        else Group.objects.filter(id__in=user_groups)
    )

    # Workflow types the user can create
    if user.is_superuser:
        creatable_workflow_types = WorkflowType.objects.filter(enabled=True)
    else:
        user_roles = user.memberships.filter(is_active=True).values_list(
            "role", flat=True
        )
        creatable_workflow_types = WorkflowType.objects.filter(
            enabled=True, create_roles__in=user_roles
        ).distinct()

    # Attach available transitions per workflow for the status dropdown
    workflows_list = list(workflows)
    for wf in workflows_list:
        wf.user_transitions = wf.get_available_transitions(user)

    priority_choices = [
        ("urgent", "Urgent"),
        ("high", "High"),
        ("medium", "Medium"),
        ("low", "Low"),
    ]

    context = {
        "workflows": workflows_list,
        "workflow_types": workflow_types,
        "states": states,
        "groups": groups,
        "creatable_workflow_types": creatable_workflow_types,
        "priority_choices": priority_choices,
        "selected_priorities": priority,
    }

    return context


@login_required
@htmx_partial("workflows/event_workflow_create.html")
def workflow_create(request):
    """
    Create a Workflow instance from an available WorkflowType.

    UI/POST contract (kept intentionally simple so you can wire it up from a modal or page):
    - workflow_type (required): WorkflowType id
    - group (optional): Group id (defaults to type's group)
    - title (required)
    - description (optional)
    - priority (optional): low|medium|high|urgent
    - deadline (optional): ISO-ish datetime string accepted by Django DateTimeField form parsing if you later add a Form
    """
    user = request.user

    # Only allow users with roles that can create at least one enabled workflow type
    user_roles = user.memberships.filter(is_active=True).values_list("role", flat=True)
    can_create = (
        WorkflowType.objects.filter(enabled=True, create_roles__in=user_roles)
        .distinct()
        .exists()
    )
    if not can_create:
        messages.error(request, "You do not have permission to create workflows.")
        return redirect("workflow_list")

    parent_workflow = None
    if request.method == "GET":
        parent_id = request.GET.get("parent")
        if parent_id:
            parent_workflow = get_object_or_404(Workflow, pk=parent_id)
            # Check if user can create subworkflows for this parent
            if not parent_workflow.can_user_edit(request.user):
                messages.error(
                    request,
                    "You do not have permission to create subworkflows for this workflow.",
                )
                return redirect("workflow_detail", pk=parent_id)

        # If creating a child workflow, filter to allowed child types
        if parent_id:
            parent_workflow = get_object_or_404(Workflow, pk=parent_id)
            allowed_child_type_ids = list(
                WorkflowTypeChildConfig.objects.filter(
                    parent_type=parent_workflow.workflow_type
                ).values_list("child_type_id", flat=True)
            )
            if user.is_superuser:
                workflow_types = WorkflowType.objects.filter(
                    enabled=True,
                    pk__in=allowed_child_type_ids,
                ).distinct()
            else:
                workflow_types = WorkflowType.objects.filter(
                    enabled=True,
                    create_roles__in=user_roles,
                    pk__in=allowed_child_type_ids,
                ).distinct()
        else:
            if user.is_superuser:
                workflow_types = WorkflowType.objects.filter(enabled=True).distinct()
            else:
                workflow_types = WorkflowType.objects.filter(
                    enabled=True, create_roles__in=user_roles
                ).distinct()

        from .forms import PROFILE_FORMS, get_workflow_type_profile_key

        # Annotate each type with its typed-profile key so the template can
        # show the correct explicit fields.
        for wt in workflow_types:
            wt.profile_key = get_workflow_type_profile_key(wt) or ""

        available_profile_keys = {wt.profile_key for wt in workflow_types}
        profile_forms = {
            key: form_cls(prefix=key)
            for key, form_cls in PROFILE_FORMS.items()
            if key in available_profile_keys
        }

        # Pre-select workflow type if passed via ?type= query param
        preselected_type = request.GET.get("type", "")
        preselected_type_name = ""
        if preselected_type:
            preselected_type_name = next(
                (wt.name for wt in workflow_types if str(wt.pk) == preselected_type), ""
            )

        # Get available events for the user's groups
        user_group_ids = user.memberships.filter(is_active=True).values_list(
            "group", flat=True
        )
        available_events = Event.objects.filter(
            group__in=user_group_ids, status__in=["scheduled", "in_progress"]
        ).order_by("start_datetime")

        # Get user's groups for group selection
        user_groups = list(
            Group.objects.filter(id__in=user_group_ids)
            .order_by("name")
            .values("pk", "name")
        )

        context = {
            "workflow_types": workflow_types,
            "parent_workflow": parent_workflow,
            "profile_forms": profile_forms,
            "preselected_type": preselected_type,
            "preselected_type_name": preselected_type_name,
            "available_events": available_events,
            "user_groups": user_groups,
        }
        if request.headers.get("HX-Request"):
            return render(request, "workflows/workflow_create.html#content", context)
        return render(request, "workflows/workflow_create.html", context)

    # POST
    workflow_type_id = request.POST.get("workflow_type")
    parent_workflow_id = request.POST.get("parent_workflow")
    relationship_type = None
    title = (request.POST.get("title") or "").strip()
    description = (request.POST.get("description") or "").strip()
    priority = request.POST.get("priority") or "medium"
    event_id = request.POST.get("event") or None

    if not workflow_type_id or not title:
        messages.error(request, "Workflow type and title are required.")
        return redirect("workflow_list")

    workflow_type = get_object_or_404(WorkflowType, pk=workflow_type_id)

    from .forms import get_workflow_profile_form_class, get_workflow_type_profile_key

    # Validate explicit, type-specific fields (typed workflow profile).
    profile_key = get_workflow_type_profile_key(workflow_type)
    profile_form_class = get_workflow_profile_form_class(workflow_type)
    profile_form = (
        profile_form_class(request.POST, prefix=profile_key)
        if profile_form_class
        else None
    )
    if profile_form and not profile_form.is_valid():
        for field_errors in profile_form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect("workflow_create")

    # Check if user has a role that can create this specific workflow type
    if (
        not user.is_superuser
        and not workflow_type.create_roles.filter(id__in=user_roles).exists()
    ):
        messages.error(
            request, "You do not have permission to create this type of workflow."
        )
        return redirect("workflow_list")

    parent_workflow = None
    if parent_workflow_id:
        parent_workflow = get_object_or_404(Workflow, pk=parent_workflow_id)
        # Check permissions
        if not parent_workflow.can_user_edit(request.user):
            messages.error(
                request,
                "You do not have permission to create subworkflows for this workflow.",
            )
            return redirect("workflow_detail", pk=parent_workflow_id)
        # Check if parent can have this type
        if not parent_workflow.can_be_parent_of(workflow_type):
            messages.error(
                request,
                f"{parent_workflow.workflow_type.name} cannot have {workflow_type.name} as subworkflow.",
            )
            return redirect("workflow_detail", pk=parent_workflow_id)
        # Resolve the relationship config FK
        relationship_type = parent_workflow.get_child_config(workflow_type)
        if not relationship_type:
            messages.error(request, "Relationship type is required for subworkflows.")
            return redirect("workflow_detail", pk=parent_workflow_id)

    initial_state = (
        State.objects.filter(workflow_type=workflow_type, is_initial=True)
        .order_by("order", "id")
        .first()
    )
    if not initial_state:
        # Fallback: first state by order if none marked initial
        initial_state = (
            State.objects.filter(workflow_type=workflow_type)
            .order_by("order", "id")
            .first()
        )

    if not initial_state:
        messages.error(
            request, f"No states configured for workflow type '{workflow_type.name}'."
        )
        return redirect("workflow_list")

    # Parse deadline
    deadline_str = request.POST.get("deadline")
    deadline = None
    if deadline_str:
        try:
            deadline = timezone.datetime.fromisoformat(
                deadline_str.replace("Z", "+00:00")
            )
            if timezone.is_naive(deadline):
                deadline = timezone.make_aware(deadline)
        except (ValueError, AttributeError):
            pass

    # Get event if provided
    event = None
    if event_id:
        try:
            event = Event.objects.get(pk=event_id)
        except Event.DoesNotExist:
            pass

    # Get selected group
    group_id = request.POST.get("group")
    if not group_id:
        messages.error(request, "Please select an owning group for the workflow.")
        return redirect("workflow_create")

    try:
        selected_group = Group.objects.get(pk=group_id)
        # Verify user is a member of the selected group
        if (
            not user.is_superuser
            and not user.memberships.filter(
                group=selected_group, is_active=True
            ).exists()
        ):
            messages.error(request, "You must be a member of the selected group.")
            return redirect("workflow_create")
    except Group.DoesNotExist:
        messages.error(request, "Invalid group selected.")
        return redirect("workflow_create")

    workflow = Workflow.objects.create(
        workflow_type=workflow_type,
        title=title,
        description=description,
        current_state=initial_state,
        owner=user,
        priority=priority,
        parent_workflow=parent_workflow,
        relationship_type=relationship_type if parent_workflow else None,
        deadline=deadline,
        event=event,
    )
    workflow.notify_roles.set(workflow_type.notify_roles.all())

    if profile_form:
        profile = profile_form.save(commit=False)
        profile.workflow = workflow
        profile.save()

    # Create RBAC group access for the selected group
    # Permissions are now determined by the user's Role within the group
    workflow.add_group_access(
        group=selected_group,
        is_primary=True,
        granted_by=user,
        notes=f"Primary group selected at creation by {user.get_full_name() or user.username}",
    )

    notify_workflow_created(workflow, actor=user)

    messages.success(request, f"Workflow created: {workflow.title}")
    return redirect("workflow_detail", pk=workflow.pk)


@login_required
@require_http_methods(["POST"])
def workflow_bulk_create(request, parent_pk):
    """Bulk-create multiple child workflows under a parent in one submission."""
    parent = get_object_or_404(Workflow, pk=parent_pk)

    if not parent.can_user_edit(request.user):
        return JsonResponse(
            {"success": False, "error": "Permission denied."}, status=403
        )

    workflow_type_id = request.POST.get("workflow_type")
    if not workflow_type_id:
        return JsonResponse(
            {"success": False, "error": "Workflow type is required."}, status=400
        )

    workflow_type = get_object_or_404(WorkflowType, pk=workflow_type_id)

    if not parent.can_be_parent_of(workflow_type):
        return JsonResponse(
            {
                "success": False,
                "error": f"{parent.workflow_type.name} cannot have {workflow_type.name} as a sub-workflow.",
            },
            status=400,
        )

    if not request.user.is_superuser:
        user_roles = request.user.memberships.filter(is_active=True).values_list(
            "role", flat=True
        )
        if not workflow_type.create_roles.filter(id__in=user_roles).exists():
            return JsonResponse(
                {
                    "success": False,
                    "error": "You do not have permission to create this workflow type.",
                },
                status=403,
            )

    relationship_type = parent.get_child_config(workflow_type)
    if not relationship_type:
        return JsonResponse(
            {"success": False, "error": "No relationship config found."}, status=400
        )

    initial_state = (
        State.objects.filter(workflow_type=workflow_type, is_initial=True)
        .order_by("order", "id")
        .first()
    ) or State.objects.filter(workflow_type=workflow_type).order_by(
        "order", "id"
    ).first()

    if not initial_state:
        return JsonResponse(
            {
                "success": False,
                "error": f"No states configured for '{workflow_type.name}'.",
            },
            status=400,
        )

    # Collect rows: titles[] / priorities[] / deadlines[] / descriptions[]
    titles = request.POST.getlist("titles[]")
    priorities = request.POST.getlist("priorities[]")
    deadlines = request.POST.getlist("deadlines[]")
    descriptions = request.POST.getlist("descriptions[]")

    created = []
    for i, title in enumerate(titles):
        title = title.strip()
        if not title:
            continue
        priority = priorities[i] if i < len(priorities) else "medium"
        deadline_str = deadlines[i] if i < len(deadlines) else ""
        description = descriptions[i].strip() if i < len(descriptions) else ""

        deadline = None
        if deadline_str:
            from django.utils.dateparse import parse_date, parse_datetime

            deadline = parse_datetime(deadline_str) or parse_date(deadline_str)

        wf = Workflow.objects.create(
            workflow_type=workflow_type,
            title=title,
            description=description,
            current_state=initial_state,
            owner=request.user,
            priority=priority or "medium",
            deadline=deadline,
            parent_workflow=parent,
            relationship_type=relationship_type,
        )
        wf.notify_roles.set(workflow_type.notify_roles.all())

        # Create group access - inherit from parent's primary group
        parent_primary_access = parent.group_access.filter(is_primary=True).first()
        if parent_primary_access:
            wf.add_group_access(
                group=parent_primary_access.group,
                is_primary=True,
                granted_by=request.user,
                notes=f"Inherited from parent workflow: {parent.title}",
            )
        else:
            # Fallback: use WorkflowType's group (legacy)
            wf.add_group_access(
                group=workflow_type.group,
                is_primary=True,
                granted_by=request.user,
                notes="Auto-assigned from WorkflowType (parent had no group access)",
            )

        notify_workflow_created(wf, actor=request.user)
        created.append({"id": wf.pk, "title": wf.title})

    if not created:
        return JsonResponse(
            {"success": False, "error": "No valid titles provided."}, status=400
        )

    return JsonResponse({"success": True, "created": created, "count": len(created)})


def _diagram_paths_for_workflow_type(workflow_type_name: str) -> list[str]:
    """Return candidate absolute paths for a workflow type diagram SVG."""
    diagram_filename = f"{workflow_type_name.lower().replace(' ', '_')}_workflow.svg"

    configured_dirs = getattr(settings, "WORKFLOW_DIAGRAM_DIRS", None)
    if configured_dirs:
        return [
            str(os.path.join(str(path), diagram_filename)) for path in configured_dirs
        ]

    # Fallback for older environments without settings configured
    app_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(app_dir)
    return [
        os.path.join(project_root, "workflow_diagrams", diagram_filename),
        os.path.join(project_root, "diagrams", diagram_filename),
        os.path.join(app_dir, "diagrams", diagram_filename),
    ]


@login_required
@htmx_partial("workflows/workflow_detail.html")
def workflow_detail(request, pk):
    """Detailed view of a workflow"""
    workflow = get_object_or_404(Workflow, pk=pk)

    # Check if user can view this workflow
    if not workflow.can_user_view(request.user):
        messages.error(request, "You do not have permission to view this workflow.")
        return redirect("workflow_list")

    if request.method == "POST":
        text = (request.POST.get("comment") or "").strip()

        # Accept both repeated attachment_ids and CSV from the hidden input.
        raw_attachment_values = request.POST.getlist("attachment_ids")
        raw_attachment_values += request.POST.getlist("attachment_ids[]")

        attachment_ids = []
        for raw_value in raw_attachment_values:
            if not raw_value:
                continue
            for candidate in str(raw_value).split(","):
                candidate = candidate.strip()
                if not candidate:
                    continue
                try:
                    # Validate UUID format up-front to avoid queryset ValidationError.
                    Attachment._meta.get_field("id").to_python(candidate)
                    attachment_ids.append(candidate)
                except (ValidationError, ValueError, TypeError):
                    continue

        if text:
            comment = Comment.objects.create(
                workflow=workflow, user=request.user, text=text
            )

            # Add attachments if provided
            if attachment_ids:
                try:
                    attachments = list(Attachment.objects.filter(id__in=attachment_ids))
                except (ValidationError, ValueError, TypeError):
                    attachments = []
                if attachments:
                    comment.attachments.add(*attachments)

            messages.success(request, "Comment added.")
        else:
            messages.error(request, "Comment cannot be empty.")
        return redirect("workflow_detail", pk=pk)

    # Get available transitions for this user
    available_transitions = workflow.get_available_transitions(request.user)

    # Get transition history
    transition_logs = workflow.transition_logs.all().select_related(
        "user", "from_state", "to_state"
    )

    # Get comments with attachments
    comments = (
        workflow.comments.all().select_related("user").prefetch_related("attachments")
    )

    # Get hierarchy information
    hierarchy_path = workflow.get_workflow_hierarchy_path()
    sub_workflows = workflow.sub_workflows.all().select_related(
        "workflow_type", "current_state", "owner", "relationship_type"
    )
    root_workflow = workflow.get_root_workflow()

    # Get sibling workflows (other sub-workflows of the same parent)
    sibling_workflows = []
    if workflow.parent_workflow:
        sibling_workflows = workflow.parent_workflow.sub_workflows.exclude(
            id=workflow.id
        ).select_related("workflow_type", "current_state", "relationship_type")

    # Allowed child types for this workflow type (drives "Add" buttons)
    allowed_child_configs = workflow.workflow_type.allowed_child_configs.select_related(
        "child_type"
    )

    # Calculate related count for badge
    related_count = sub_workflows.count() + len(sibling_workflows)
    if workflow.parent_workflow:
        related_count += 1

    # Diagram tab: states with permissions, transitions with roles
    wt = workflow.workflow_type
    diagram_states = wt.states.prefetch_related("permissions__roles").order_by(
        "order", "name"
    )
    diagram_transitions = (
        wt.transitions.select_related("from_state", "to_state")
        .prefetch_related("allowed_roles")
        .order_by("order", "name")
    )

    # Check if diagram file exists (support legacy and current output directories)
    diagram_paths = _diagram_paths_for_workflow_type(wt.name)
    diagram_url = (
        f"/workflow-types/{wt.pk}/diagram/"
        if any(os.path.exists(path) for path in diagram_paths)
        else None
    )

    # Referral context
    active_referrals = workflow.active_referrals
    referral_history = workflow.referrals.select_related(
        "referred_to", "referred_by", "recalled_by"
    ).order_by("-referred_at")
    can_edit = workflow.can_user_edit(request.user)
    can_refer = can_edit and workflow.current_state.allows_referrals

    # Add referral status information
    is_referred = workflow.is_referred
    referral_deadline = workflow.referral_deadline

    # Explicit, type-specific fields from the typed workflow profile.
    typed_fields = workflow.get_typed_fields()

    can_delete = workflow.can_user_delete(request.user)

    context = {
        "workflow": workflow,
        "available_transitions": available_transitions,
        "transition_logs": transition_logs,
        "comments": comments,
        "hierarchy_path": hierarchy_path,
        "sub_workflows": sub_workflows,
        "sibling_workflows": sibling_workflows,
        "root_workflow": root_workflow,
        "is_root": workflow.is_root_workflow,
        "has_children": workflow.has_sub_workflows,
        "hierarchy_level": workflow.hierarchy_level,
        "related_count": related_count,
        "active_referrals": active_referrals,
        "referral_history": referral_history,
        "can_edit": can_edit,
        "can_refer": can_refer,
        "is_referred": is_referred,
        "referral_deadline": referral_deadline,
        "diagram_url": diagram_url,
        "diagram_states": diagram_states,
        "diagram_transitions": diagram_transitions,
        "typed_fields": typed_fields,
        "allowed_child_configs": allowed_child_configs,
        "can_delete": can_delete,
        "referral_configs": [],  # Deprecated - keeping for template compatibility
    }

    return context


@login_required
@htmx_partial("workflows/event_create.html")
def event_create(request):
    """Create a new event."""
    from .forms import EventForm

    if request.method == "POST":
        form = EventForm(request.user, request.POST)
        if form.is_valid():
            event = form.save()
            messages.success(request, f"Event '{event.title}' created successfully.")
            return redirect("event_detail", pk=event.pk)
    else:
        form = EventForm(request.user)

    selected_group = None
    group_search = ""
    selected_group_id = form["group"].value()
    if selected_group_id:
        try:
            selected_group = (
                form.fields["group"].queryset.filter(pk=selected_group_id).first()
            )
        except (ValueError, ValidationError):
            selected_group = None
        if selected_group:
            group_search = selected_group.name

    if request.method == "POST" and not group_search:
        group_search = (request.POST.get("group_search") or request.POST.get("search") or "").strip()

    context = {
        "form": form,
        "selected_group": selected_group,
        "group_search": group_search,
    }
    return context
    # return render(request, "workflows/event_create.html", context )


@login_required
def event_group_lookup(request):
    """HTMX endpoint for searching groups in the event create/edit forms."""
    search_query = (request.GET.get("search") or request.GET.get("group_search") or "").strip()

    # If the user is superuser, allow searching all groups. Otherwise restrict to their groups.
    if request.user.is_superuser:
        groups = Group.objects.all()
    else:
        user_group_ids = request.user.memberships.filter(is_active=True).values_list(
            "group", flat=True
        )
        groups = Group.objects.filter(id__in=user_group_ids)

    # Broaden search to multiple fields so terms like 'ICT' may match short_name/slug/description
    if search_query:
        groups = groups.filter(
            Q(name__icontains=search_query)
            | Q(short_name__icontains=search_query)
            | Q(slug__icontains=search_query)
            | Q(description__icontains=search_query)
            | Q(parent__name__icontains=search_query)
        ).distinct()

    groups = groups.order_by("name")[:20]
    groups_count = groups.count()
    debug_mode = bool(request.GET.get("debug"))

    suggestions = False
    # If no matches and a search was provided, offer suggestions for superusers
    if groups_count == 0 and search_query and request.user.is_superuser:
        suggestions = True
        groups = Group.objects.order_by("name")[:20]
        groups_count = groups.count()

    return render(
        request,
        "workflows/partials/event_group_lookup_results.html",
        {
            "groups": groups,
            "search_query": search_query,
            "groups_count": groups_count,
            "debug": debug_mode,
            "request_user": request.user.get_username(),
            "suggestions": suggestions,
        },
    )


@login_required
# @htmx_partial("workflows/event_detail.html")
def event_update_status(request, pk, status):
    """Update event status."""
    event = get_object_or_404(Event, pk=pk)

    # Check if user can update this event (organizer or group member)
    user_groups = request.user.memberships.filter(is_active=True).values_list(
        "group", flat=True
    )
    if event.organizer != request.user and event.group.id not in user_groups:
        messages.error(request, "You don't have permission to update this event.")
        return redirect("event_detail", pk=pk)

    # Validate status
    valid_statuses = [choice[0] for choice in Event.STATUS_CHOICES]
    if status not in valid_statuses:
        messages.error(request, "Invalid status.")
        return redirect("event_detail", pk=pk)

    old_status = event.status
    event.status = status
    event.save()

    message = f"Event status changed from {old_status.title()} to {status.title()}."

    # If status changed to completed, mention attendance records were created
    if old_status != status and status == "completed":
        attendance_count = event.attendances.count()
        message += f" Attendance records created for {attendance_count} group members."

    messages.success(request, message)

    return redirect("event_detail", pk=pk)
    # return {
    #     "event": event,
    #     "attendances": event.attendances.select_related("user").order_by(
    #         "user__first_name", "user__last_name"
    #     ),
    # }


@login_required
@htmx_partial("workflows/event_edit.html")
def event_edit(request, pk):
    """Edit an existing event."""
    event = get_object_or_404(Event, pk=pk)

    # Check permissions (organizer or group member)
    user_groups = request.user.memberships.filter(is_active=True).values_list(
        "group", flat=True
    )
    if event.organizer != request.user and event.group.id not in user_groups:
        messages.error(request, "You don't have permission to edit this event.")
        return redirect("event_detail", pk=pk)

    from .forms import EventForm

    if request.method == "POST":
        form = EventForm(request.user, request.POST, instance=event)
        if form.is_valid():
            form.save()
            messages.success(request, f"Event '{event.title}' updated successfully.")
            return redirect("event_detail", pk=pk)
    else:
        form = EventForm(request.user, instance=event)

    selected_group = None
    group_search = ""
    selected_group_id = form["group"].value()
    if selected_group_id:
        try:
            selected_group = (
                form.fields["group"].queryset.filter(pk=selected_group_id).first()
            )
        except (ValueError, ValidationError):
            selected_group = None
        if selected_group:
            group_search = selected_group.name

    if request.method == "POST" and not group_search:
        group_search = (request.POST.get("group_search") or request.POST.get("search") or "").strip()

    context = {
        "form": form,
        "event": event,
        "selected_group": selected_group,
        "group_search": group_search,
        "attendances": event.attendances.select_related("user").order_by(
            "user__first_name", "user__last_name"
        ),
    }
    return context


@login_required
def event_export(request, pk):
    """Export event as iCal file."""
    event = get_object_or_404(Event, pk=pk)

    # Create iCal content
    ical_content = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//PWS//Event Export//EN
BEGIN:VEVENT
UID:{event.pk}@pws.local
DTSTART:{event.start_datetime.strftime("%Y%m%dT%H%M%S")}
DTEND:{event.end_datetime.strftime("%Y%m%dT%H%M%S")}
SUMMARY:{event.title}
DESCRIPTION:{event.description.replace("\n", "\\n")}
LOCATION:{event.venue.name if event.venue else "TBD"}
STATUS:{event.status.upper()}
END:VEVENT
END:VCALENDAR"""

    response = HttpResponse(ical_content, content_type="text/calendar")
    response["Content-Disposition"] = f'attachment; filename="{event.title}.ics"'
    return response


@login_required
def event_export_pdf(request, pk):
    """Export event as PDF with details and attendance records."""
    from django.template.loader import render_to_string
    from weasyprint import CSS, HTML

    event = get_object_or_404(Event, pk=pk)

    # Get attendance records
    attendances = event.attendances.select_related("user").order_by(
        "user__first_name", "user__last_name"
    )

    # Count attendance statuses
    status_counts = {}
    for attendance in attendances:
        status_counts[attendance.status] = status_counts.get(attendance.status, 0) + 1

    # Render HTML template
    html_string = render_to_string(
        "workflows/event_pdf.html",
        {
            "event": event,
            "attendances": attendances,
            "status_counts": status_counts,
            "request": request,
        },
    )

    # Generate PDF
    html = HTML(string=html_string)
    css = CSS(
        string="""
        @page {
            size: A4;
            margin: 2cm;
        }
        body {
            font-family: Arial, sans-serif;
            font-size: 12px;
            line-height: 1.4;
        }
        .header {
            border-bottom: 2px solid #333;
            padding-bottom: 10px;
            margin-bottom: 20px;
        }
        .section {
            margin-bottom: 20px;
        }
        .section-title {
            font-weight: bold;
            font-size: 14px;
            margin-bottom: 10px;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 15px;
        }
        th, td {
            border: 1px solid #ddd;
            padding: 8px;
            text-align: left;
        }
        th {
            background-color: #f5f5f5;
            font-weight: bold;
        }
        .status-badge {
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 10px;
            font-weight: bold;
        }
        .status-attended { background-color: #d4edda; color: #155724; }
        .status-absent { background-color: #f8d7da; color: #721c24; }
        .status-confirmed { background-color: #d1ecf1; color: #0c5460; }
        .status-invited { background-color: #fff3cd; color: #856404; }
        .status-apology { background-color: #e2e3e5; color: #383d41; }
    """
    )

    pdf = html.write_pdf(stylesheets=[css])

    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="{event.title}_meeting_report.pdf"'
    )
    return response


@login_required
@htmx_partial("workflows/event_attendance_edit.html")
def event_attendance_edit(request, pk):
    """Edit attendance records for an event."""
    event = get_object_or_404(Event, pk=pk)

    # Check permissions (organizer or group member)
    user_groups = request.user.memberships.filter(is_active=True).values_list(
        "group", flat=True
    )
    if event.organizer != request.user and event.group.id not in user_groups:
        messages.error(
            request, "You don't have permission to edit attendance for this event."
        )
        return redirect("event_detail", pk=pk)

    from .forms import EventAttendanceBulkForm

    if request.method == "POST":
        form = EventAttendanceBulkForm(event, request.POST)
        if form.is_valid():
            updated_count = form.save()
            messages.success(request, f"Updated {updated_count} attendance records.")
            return redirect("event_detail", pk=pk)
    else:
        form = EventAttendanceBulkForm(event)

    context = {
        "form": form,
        "event": event,
        "attendances": event.attendances.select_related("user").order_by(
            "user__first_name", "user__last_name"
        ),
    }
    return context


@login_required
def comment_delete(request, pk, comment_pk):
    comment = get_object_or_404(Comment, pk=comment_pk, workflow_id=pk)
    if comment.user != request.user:
        messages.error(request, "You can only delete your own comments.")
    else:
        comment.delete()
        messages.success(request, "Comment deleted.")
    return redirect("workflow_detail", pk=pk)


@login_required
@require_http_methods(["POST"])
def workflow_delete(request, pk):
    """Safely delete a workflow if the user has delete permission."""
    workflow = get_object_or_404(Workflow, pk=pk)

    if not workflow.can_user_delete(request.user):
        messages.error(request, "You do not have permission to delete this workflow.")
        return redirect("workflow_detail", pk=pk)

    workflow._audit_user = request.user
    workflow_title = workflow.title
    workflow.delete()
    messages.success(request, f"Workflow '{workflow_title}' deleted successfully.")
    return redirect("workflow_list")


@login_required
@htmx_partial("workflows/workflow_edit.html")
def workflow_edit(request, pk):
    """Edit a workflow"""
    workflow = get_object_or_404(Workflow, pk=pk)

    # Check if user can edit this workflow
    if not workflow.can_user_edit(request.user):
        messages.error(request, "You do not have permission to edit this workflow.")
        return redirect("workflow_detail", pk=pk)

    if request.method == "GET":
        workflow_type = workflow.workflow_type

        # Get available events for the user's groups
        user_groups = request.user.memberships.filter(is_active=True).values_list(
            "group", flat=True
        )
        available_events = Event.objects.filter(
            group__in=user_groups, status__in=["scheduled", "in_progress"]
        ).order_by("start_datetime")

        from .forms import get_workflow_profile_form_class

        profile_form_class = get_workflow_profile_form_class(workflow_type)
        profile_form = (
            profile_form_class(instance=workflow.typed) if profile_form_class else None
        )

        context = {
            "workflow": workflow,
            "workflow_types": WorkflowType.objects.all(),
            "profile_form": profile_form,
            "available_events": available_events,
        }
        return context

    # POST
    workflow_type_id = request.POST.get("workflow_type")
    title = (request.POST.get("title") or "").strip()
    description = (request.POST.get("description") or "").strip()
    priority = request.POST.get("priority") or "medium"
    deadline = request.POST.get("deadline")
    event_id = request.POST.get("event") or None

    if not workflow_type_id or not title:
        messages.error(request, "Workflow type and title are required.")
        return redirect("workflow_edit", pk=pk)

    workflow_type = get_object_or_404(WorkflowType, pk=workflow_type_id)

    from .forms import get_workflow_profile_form_class

    # Validate explicit, type-specific fields before mutating the workflow.
    profile_form_class = get_workflow_profile_form_class(workflow_type)
    profile_form = None
    if profile_form_class:
        profile_form = profile_form_class(request.POST, instance=workflow.typed)
        if not profile_form.is_valid():
            for field_errors in profile_form.errors.values():
                for error in field_errors:
                    messages.error(request, error)
            return redirect("workflow_edit", pk=pk)

    # Capture old values before saving for notification diffing
    old_assigned_to = workflow.assigned_to
    old_referred_to = (
        workflow.referred_to_groups.first()
        if workflow.referred_to_groups.exists()
        else None
    )

    assigned_to_id = request.POST.get("assigned_to") or None
    referred_to_id = request.POST.get("referred_to") or None

    new_assigned_to = None
    if assigned_to_id:
        try:
            new_assigned_to = User.objects.get(pk=assigned_to_id)
        except User.DoesNotExist:
            pass

    new_referred_to = None
    if referred_to_id:
        try:
            new_referred_to = Group.objects.get(pk=referred_to_id)
        except Group.DoesNotExist:
            pass

    # Get event if provided
    new_event = None
    if event_id:
        try:
            new_event = Event.objects.get(pk=event_id)
        except Event.DoesNotExist:
            pass

    # Update workflow
    workflow.workflow_type = workflow_type
    workflow.title = title
    workflow.description = description
    workflow.priority = priority
    workflow.assigned_to = new_assigned_to
    # Note: referrals are now managed through the referral system, not direct assignment
    workflow.event = new_event
    if deadline:
        workflow.deadline = deadline
    workflow.save()

    if profile_form:
        profile = profile_form.save(commit=False)
        profile.workflow = workflow
        profile.save()

    actor = request.user.get_full_name() or request.user.username

    # Notify newly assigned user
    if new_assigned_to and new_assigned_to != old_assigned_to:
        _create_notification(
            user=new_assigned_to,
            verb=Notification.VERB_ASSIGNED,
            title=f"Workflow assigned to you: '{workflow.title}'",
            message=f"{actor} assigned '{workflow.title}' to you.",
            workflow=workflow,
        )

    # Notify members of newly referred-to group
    if new_referred_to and new_referred_to != old_referred_to:
        referred_roles = (
            new_referred_to.members.filter(is_active=True)
            .values_list("role", flat=True)
            .distinct()
        )
        _notify_group_members(
            group=new_referred_to,
            roles=referred_roles,
            verb=Notification.VERB_REFERRED,
            title=f"Workflow referred to your group: '{workflow.title}'",
            message=f"{actor} referred '{workflow.title}' to {new_referred_to.name}.",
            workflow=workflow,
            exclude_user=request.user,
        )

    messages.success(request, f"Workflow updated: {workflow.title}")
    return redirect("workflow_detail", pk=workflow.pk)


@login_required
@require_http_methods(["POST"])
def workflow_refer(request, pk):
    """
    Refer a workflow to another group, or recall an active referral.
    POST body (form-encoded):
      action      = "refer" | "recall"
      group_id    = <Group pk>          (for refer)
      reason      = <text>              (optional for refer)
      recall_reason = <text>            (optional for recall)
    """
    workflow = get_object_or_404(Workflow, pk=pk)

    if not workflow.can_user_edit(request.user):
        return JsonResponse({"error": "Permission denied."}, status=403)

    action = request.POST.get("action")

    if action == "refer":
        group_id = request.POST.get("group_id")
        reason = (request.POST.get("reason") or "").strip()
        deadline_str = request.POST.get("deadline", "").strip()

        if not group_id:
            return JsonResponse({"error": "group_id is required."}, status=400)

        target_group = get_object_or_404(Group, pk=group_id)

        # Check if current state allows referrals
        if not workflow.current_state.allows_referrals:
            return JsonResponse(
                {
                    "error": f"Workflows in state '{workflow.current_state.name}' cannot be referred."
                },
                status=400,
            )

        # Check if workflow is already referred to this group
        if workflow.referred_to_groups.filter(id=target_group.id).exists():
            return JsonResponse(
                {
                    "error": f"This workflow is already referred to '{target_group.name}'."
                },
                status=400,
            )

        # Parse deadline if provided
        deadline = None
        if deadline_str:
            try:
                from datetime import datetime

                deadline = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
            except ValueError:
                return JsonResponse(
                    {
                        "error": "Invalid deadline format. Use ISO format (YYYY-MM-DDTHH:MM:SS)."
                    },
                    status=400,
                )

        # Create referral without config dependency
        referral = WorkflowReferral.objects.create(
            workflow=workflow,
            referred_to=target_group,
            config=None,  # No longer using referral configs
            reason=reason,
            deadline=deadline,
            referred_by=request.user,
        )

        # The workflow's referred_to_groups will be updated automatically by the referral's save method

        # Notify all active members of the referred group
        actor = request.user.get_full_name() or request.user.username
        all_roles = (
            target_group.members.filter(is_active=True)
            .values_list("role", flat=True)
            .distinct()
        )
        _notify_group_members(
            group=target_group,
            roles=all_roles,
            verb=Notification.VERB_REFERRED,
            title=f"Workflow referred to your group: '{workflow.title}'",
            message=f"{actor} referred '{workflow.title}' to {target_group.name}"
            + (f": {reason}" if reason else "."),
            workflow=workflow,
            exclude_user=request.user,
        )

        # Audit log
        AuditLog.objects.create(
            content_type=ContentType.objects.get_for_model(workflow),
            object_id=workflow.pk,
            action="update",
            user=request.user,
            ip_address=request.META.get("HTTP_X_FORWARDED_FOR", "")
            .split(",")[0]
            .strip()
            or request.META.get("REMOTE_ADDR"),
            changes={"referred_to": target_group.name, "reason": reason},
        )

        return JsonResponse(
            {
                "success": True,
                "referral_id": str(referral.pk),
                "referred_to": target_group.name,
                "referred_at": referral.referred_at.strftime("%Y-%m-%d %H:%M"),
                "label": "",  # No longer using config labels
            }
        )

    elif action == "recall":
        group_id = request.POST.get("group_id")
        recall_reason = (request.POST.get("recall_reason") or "").strip()

        if not group_id:
            return JsonResponse(
                {"error": "group_id is required for recall."}, status=400
            )

        target_group = get_object_or_404(Group, pk=group_id)

        # Find active referral to this specific group
        active_referral = workflow.referrals.filter(
            referred_to=target_group, recalled_at__isnull=True
        ).first()

        if not active_referral:
            return JsonResponse(
                {"error": f"No active referral to '{target_group.name}' to recall."},
                status=400,
            )
        active_referral.recalled_at = timezone.now()
        active_referral.recalled_by = request.user
        active_referral.recall_reason = recall_reason
        active_referral.save()

        # The workflow's referred_to_groups will be updated automatically by the referral's save method

        AuditLog.objects.create(
            content_type=ContentType.objects.get_for_model(workflow),
            object_id=workflow.pk,
            action="update",
            user=request.user,
            ip_address=request.META.get("HTTP_X_FORWARDED_FOR", "")
            .split(",")[0]
            .strip()
            or request.META.get("REMOTE_ADDR"),
            changes={
                "recalled_referral": active_referral.referred_to.name,
                "recall_reason": recall_reason,
            },
        )

        return JsonResponse({"success": True, "recalled": True})

    return JsonResponse({"error": "Invalid action."}, status=400)


@login_required
def api_referral_targets(request, pk):
    """Return all groups that a workflow can be referred to as JSON or HTML for HTMX."""
    workflow = get_object_or_404(Workflow, pk=pk)

    # Get search query from request
    search_query = request.GET.get("search", "").strip()

    # Get all groups except those the workflow is already referred to
    already_referred = workflow.referred_to_groups.values_list("id", flat=True)
    groups = Group.objects.exclude(id__in=already_referred).order_by("name")

    # Filter by search query if provided
    if search_query:
        groups = groups.filter(name__icontains=search_query)

    # Check if this is an HTMX request (wants HTML)
    if request.headers.get("HX-Request"):
        from django.template.loader import render_to_string

        html = render_to_string(
            "workflows/partials/group_search_results.html",
            {
                "groups": groups,
                "search_query": search_query,
            },
        )
        return HttpResponse(html)

    # Return JSON for regular API requests
    data = [
        {
            "group_id": g.pk,
            "group_name": g.name,
            "label": "",  # No longer using config labels
        }
        for g in groups
    ]
    return JsonResponse({"targets": data})


@login_required
def workflow_transition(request, pk, transition_id):
    """Execute a workflow transition"""
    workflow = get_object_or_404(Workflow, pk=pk)
    transition = get_object_or_404(Transition, pk=transition_id)

    # Check if user can perform this transition (applies to both GET and POST)
    available_transitions = workflow.get_available_transitions(request.user)
    if transition not in available_transitions:
        messages.error(
            request, "You do not have permission to perform this transition."
        )
        return redirect("workflow_detail", pk=pk)

    if request.method == "POST":
        comment = request.POST.get("comment", "")

        # Validate comment if required
        if transition.requires_comment and not comment:
            messages.error(request, "A comment is required for this transition.")
            return redirect("workflow_detail", pk=pk)

        from_state = workflow.current_state

        # Log the transition
        transition_log = WorkflowTransitionLog.objects.create(
            workflow=workflow,
            transition=transition,
            from_state=from_state,
            to_state=transition.to_state,
            user=request.user,
            comment=comment,
        )

        # Audit log with IP address
        AuditLog.objects.create(
            content_type=ContentType.objects.get_for_model(workflow),
            object_id=workflow.pk,
            action="transition",
            user=request.user,
            ip_address=request.META.get("HTTP_X_FORWARDED_FOR", "")
            .split(",")[0]
            .strip()
            or request.META.get("REMOTE_ADDR"),
            changes={
                "transition": transition.name,
                "from_state": from_state.name,
                "to_state": transition.to_state.name,
            },
        )

        # Update workflow state
        workflow.current_state = transition.to_state
        workflow.save()

        # Auto-recall all active referrals when reaching a terminal state
        if transition.to_state.is_terminal:
            active_referrals = workflow.active_referrals
            for referral in active_referrals:
                referral.recalled_at = timezone.now()
                referral.recalled_by = request.user
                referral.recall_reason = f"Auto-recalled: workflow reached terminal state '{transition.to_state.name}'."
                referral.save()

        # Always notify the workflow owner (unless they triggered it)
        if workflow.owner != request.user:
            _create_notification(
                user=workflow.owner,
                verb=Notification.VERB_TRANSITION,
                title=f"{workflow.workflow_type.name}: '{workflow.title}' → {transition.to_state.name}",
                message=(
                    f"{request.user.get_full_name() or request.user.username} moved "
                    f"'{workflow.title}' from {from_state.name} to {transition.to_state.name}."
                ),
                workflow=workflow,
            )

        # Also notify assigned user if different from owner and actor
        if workflow.assigned_to and workflow.assigned_to not in (
            workflow.owner,
            request.user,
        ):
            _create_notification(
                user=workflow.assigned_to,
                verb=Notification.VERB_TRANSITION,
                title=f"{workflow.workflow_type.name}: '{workflow.title}' → {transition.to_state.name}",
                message=(
                    f"{request.user.get_full_name() or request.user.username} moved "
                    f"'{workflow.title}' from {from_state.name} to {transition.to_state.name}."
                ),
                workflow=workflow,
            )

        # Enqueue email alerts + in-app notifications for notify_roles members
        notify_roles = transition.notify_roles.all()
        if notify_roles.exists():
            from .tasks import send_transition_alert

            group = workflow.workflow_type.group
            recipient_emails = list(
                group.members.filter(role__in=notify_roles, is_active=True)
                .exclude(user__email="")
                .values_list("user__email", flat=True)
                .distinct()
            )

            site_url = f"{request.scheme}://{request.get_host()}"
            triggered_by = request.user.get_full_name() or request.user.username

            send_transition_alert(
                workflow_id=str(workflow.pk),
                workflow_title=workflow.title,
                workflow_type_name=workflow.workflow_type.name,
                transition_name=transition.name,
                from_state_name=from_state.name,
                to_state_name=transition.to_state.name,
                triggered_by=triggered_by,
                recipient_emails=recipient_emails,
                site_url=site_url,
            )

            # In-app notifications for the same notify_roles members
            notif_title = f"{workflow.workflow_type.name}: '{workflow.title}' → {transition.to_state.name}"
            notif_message = (
                f"{triggered_by} moved '{workflow.title}' from "
                f"{from_state.name} to {transition.to_state.name} "
                f"via '{transition.name}'."
            )
            _notify_group_members(
                group=group,
                roles=notify_roles,
                verb=Notification.VERB_TRANSITION,
                title=notif_title,
                message=notif_message,
                workflow=workflow,
                exclude_user=request.user,
            )

        # Post status update to epetitions
        post_status_update(workflow, comment=transition_log.comment)

        messages.success(
            request, f"Workflow transitioned to {transition.to_state.name}"
        )
        return redirect("workflow_detail", pk=pk)

    context = {
        "workflow": workflow,
        "transition": transition,
    }

    return render(request, "workflows/workflow_transition.html", context)


# ============================================================================
# EVENT VIEWS
# ============================================================================


@login_required
@htmx_partial("workflows/event_list.html")
def event_list(request):
    """List all events"""
    user = request.user
    user_groups = user.memberships.filter(is_active=True).values_list(
        "group", flat=True
    )

    if user.is_superuser:
        # Superusers can see all events
        events = Event.objects.all().select_related(
            "event_type", "group", "venue", "organizer"
        )
    else:
        events = Event.objects.filter(group__in=user_groups).select_related(
            "event_type", "group", "venue", "organizer"
        )

    # Filters
    event_type = request.GET.get("type")
    status = request.GET.get("status")
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")

    if event_type:
        events = events.filter(event_type_id=event_type)
    if status:
        events = events.filter(status=status)
    if date_from:
        events = events.filter(start_datetime__gte=date_from)
    if date_to:
        events = events.filter(start_datetime__lte=date_to)

    # Separate upcoming and past events
    now = timezone.now()
    upcoming_events = events.filter(start_datetime__gte=now).order_by("start_datetime")
    past_events = events.filter(start_datetime__lt=now).order_by("-start_datetime")

    event_types = EventType.objects.all()

    context = {
        "events": events,
        "upcoming_events": upcoming_events,
        "past_events": past_events,
        "event_types": event_types,
        "venues": Venue.objects.all(),
        "organizers": User.objects.all(),  # Organizer is a foreign key to User model
        "groups": Group.objects.all(),
        "filter_params": {
            "event_type": event_type,
            "status": status,
            "date_from": date_from,
            "date_to": date_to,
        },
        "now": now,
    }

    return context


@login_required
def event_detail(request, pk):
    """Detailed view of an event"""
    event = get_object_or_404(Event, pk=pk)

    # Get attendance records
    attendances = event.attendances.all().select_related("user")

    # Check if current user has attendance record
    user_attendance = attendances.filter(user=request.user).first()

    context = {
        "event": event,
        "attendances": attendances,
        "user_attendance": user_attendance,
    }

    return render(request, "workflows/event_detail.html", context)


# ============================================================================
# GROUP VIEWS
# ============================================================================


@login_required
@htmx_partial("workflows/group_list.html")
def group_list(request):
    """List all groups"""
    user = request.user
    search_query = request.GET.get("search", "").strip()

    user_groups = user.memberships.filter(is_active=True).values_list(
        "group", flat=True
    )

    # Get groups user can access (their groups and descendants)
    if user.is_superuser:
        # Superusers can see all groups
        groups = Group.objects.all().select_related("parent")
    else:
        groups = Group.objects.filter(id__in=user_groups).select_related("parent")

    # Filter by search query if provided
    if search_query:
        groups = groups.filter(name__icontains=search_query)

    context = {
        "groups": groups,
        "search_query": search_query,
    }

    # Handle HTMX partial rendering based on URL fragment
    # if request.headers.get("HX-Request"):
    #     # HTMX request - return only the groups_container partial
    #     return render(request, "workflows/group_list.html#groups_container", context)

    # Regular request - return full page
    # return render(request, "workflows/group_list.html", context)
    return context


@login_required
def group_detail(request, pk):
    """Detailed view of a group"""
    group = get_object_or_404(Group, pk=pk)
    member_search = request.GET.get("member_search", "").strip()

    # Get members
    memberships = group.members.filter(is_active=True).select_related("user", "role")

    # Filter members by search query if provided
    if member_search:
        memberships = memberships.filter(
            Q(user__first_name__icontains=member_search)
            | Q(user__last_name__icontains=member_search)
            | Q(user__username__icontains=member_search)
            | Q(role__name__icontains=member_search)
        )

    # Get workflows
    workflows = Workflow.objects.filter(workflow_type__group=group).select_related(
        "workflow_type", "current_state"
    )[:10]

    # Get events
    events = group.events.filter(start_datetime__gte=timezone.now()).order_by(
        "start_datetime"
    )[:5]

    # Get child groups
    children = group.get_children()

    context = {
        "group": group,
        "memberships": memberships,
        "workflows": workflows,
        "events": events,
        "children": children,
        "member_search": member_search,
    }

    # Handle HTMX partial rendering for member search
    if request.headers.get("HX-Request"):
        return render(request, "workflows/group_detail.html#members_table", context)

    return render(request, "workflows/group_detail.html", context)


# ============================================================================
# DIAGRAM VIEW
# ============================================================================


@login_required
def workflow_type_diagram(request, pk):
    """Serve the diagram SVG for a workflow type."""
    workflow_type = get_object_or_404(WorkflowType, pk=pk)

    for filepath in _diagram_paths_for_workflow_type(workflow_type.name):
        if os.path.exists(filepath):
            return FileResponse(open(filepath, "rb"), content_type="image/svg+xml")

    raise Http404("Diagram not yet generated for this workflow type.")


# ============================================================================
# REPORTS VIEWS
# ============================================================================


def _apply_report_filters(workflows, request):
    """Apply period, workflow_type, overdue, and parent_workflow filters to a workflow queryset.
    Returns (filtered_qs, filter_params_dict)."""
    now = timezone.now()
    period = request.GET.get("period", "")
    workflow_type_id = request.GET.get("workflow_type", "")
    overdue_only = request.GET.get("overdue_only", "") == "1"
    parent_workflow_id = request.GET.get("parent_workflow", "")

    if period == "weekly":
        start = now - timedelta(weeks=1)
        workflows = workflows.filter(created_at__gte=start)
    elif period == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        workflows = workflows.filter(created_at__gte=start)
    elif period == "quarterly":
        quarter_start_month = ((now.month - 1) // 3) * 3 + 1
        start = now.replace(
            month=quarter_start_month, day=1, hour=0, minute=0, second=0, microsecond=0
        )
        workflows = workflows.filter(created_at__gte=start)

    if workflow_type_id:
        workflows = workflows.filter(workflow_type_id=workflow_type_id)

    if overdue_only:
        workflows = workflows.filter(deadline__lt=now, current_state__is_terminal=False)

    # Parent workflow filter: include the parent itself + all direct sub-workflows
    parent_workflow = None
    if parent_workflow_id:
        try:
            parent_workflow = Workflow.objects.select_related(
                "workflow_type", "current_state", "owner"
            ).get(pk=parent_workflow_id)
            workflows = workflows.filter(
                Q(pk=parent_workflow_id) | Q(parent_workflow_id=parent_workflow_id)
            )
        except Workflow.DoesNotExist:
            parent_workflow_id = ""

    return workflows, {
        "period": period,
        "workflow_type_id": workflow_type_id,
        "overdue_only": overdue_only,
        "parent_workflow_id": parent_workflow_id,
        "parent_workflow": parent_workflow,
    }


@login_required
@htmx_partial("workflows/reports.html")
def reports(request):
    """Reports and analytics view"""
    user = request.user
    user_groups = user.memberships.filter(is_active=True).values_list(
        "group", flat=True
    )

    # Get report type (activity or overview)
    report_type = request.GET.get("report_type", "overview")

    # Get workflows user can view
    if user.is_superuser:
        # Superusers can see all workflows in reports
        base_workflows = Workflow.objects.all().select_related(
            "workflow_type", "current_state", "owner"
        )
    else:
        base_workflows = (
            Workflow.objects.filter(
                Q(group_access__group__in=user_groups)
                | Q(workflow_type__group__in=user_groups)
                | Q(referred_to_groups__in=user_groups)
            )
            .distinct()
            .select_related("workflow_type", "current_state", "owner")
        )

    # Apply custom report filters
    workflows, filter_params = _apply_report_filters(base_workflows, request)
    filter_params["report_type"] = report_type

    # Workflow statistics
    total_workflows = workflows.count()

    # By workflow type
    by_type = (
        workflows.values("workflow_type__name")
        .annotate(count=Count("id"))
        .order_by("-count")
    )

    # By state
    by_state = (
        workflows.values("current_state__name", "current_state__color")
        .annotate(count=Count("id"))
        .order_by("-count")
    )

    # By priority
    by_priority = (
        workflows.values("priority").annotate(count=Count("id")).order_by("priority")
    )

    # By group
    by_group = (
        workflows.values("workflow_type__group__name")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )

    now = timezone.now()

    # Overdue workflows
    overdue = workflows.filter(
        deadline__lt=now, current_state__is_terminal=False
    ).count()

    # Workflows created this month
    this_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    created_this_month = workflows.filter(created_at__gte=this_month_start).count()

    # Completed workflows (terminal states)
    completed = workflows.filter(current_state__is_terminal=True).count()

    # Active workflows
    active = workflows.filter(current_state__is_terminal=False).count()

    # Recent activity (transition logs) - for activity report
    recent_transitions = None
    hierarchical_parents = None
    if report_type == "activity":
        recent_transitions = (
            WorkflowTransitionLog.objects.filter(workflow__in=workflows)
            .select_related("workflow", "user", "from_state", "to_state", "transition")
            .order_by("-timestamp")[:50]
        )
    elif report_type == "overview":
        # Build hierarchical structure for parent-child workflows
        hierarchical_parents = (
            workflows.filter(parent_workflow__isnull=True)
            .select_related("workflow_type", "current_state", "owner")
            .prefetch_related(
                "sub_workflows__workflow_type",
                "sub_workflows__current_state",
                "sub_workflows__owner",
            )
            .order_by("created_at")
        )

    # Events statistics
    if user.is_superuser:
        events = Event.objects.all()
    else:
        events = Event.objects.filter(group__in=user_groups)
    total_events = events.count()
    upcoming_events = events.filter(start_datetime__gte=now).count()

    # Workflow types available to this user for filtering
    workflow_types = (
        WorkflowType.objects.all().order_by("name")
        if user.is_superuser
        else WorkflowType.objects.filter(group__in=user_groups).order_by("name")
    )

    # Top-level (parent) workflows available to this user for the parent filter
    parent_workflows = base_workflows.filter(parent_workflow__isnull=True).order_by(
        "title"
    )

    context = {
        "workflows": workflows,
        "total_workflows": total_workflows,
        "by_type": by_type,
        "by_state": by_state,
        "by_priority": by_priority,
        "by_group": by_group,
        "overdue": overdue,
        "created_this_month": created_this_month,
        "completed": completed,
        "active": active,
        "recent_transitions": recent_transitions,
        "hierarchical_parents": hierarchical_parents,
        "total_events": total_events,
        "upcoming_events": upcoming_events,
        "workflow_types": workflow_types,
        "parent_workflows": parent_workflows,
        "filter_params": filter_params,
        "report_type": report_type,
        "now": now,
    }

    return context


@login_required
def reports_export_csv(request):
    """Export workflow report data as CSV"""
    import csv

    user = request.user
    user_groups = user.memberships.filter(is_active=True).values_list(
        "group", flat=True
    )
    if user.is_superuser:
        base_workflows = Workflow.objects.all().select_related(
            "workflow_type", "current_state", "owner"
        )
    else:
        base_workflows = (
            Workflow.objects.filter(
                Q(group_access__group__in=user_groups)
                | Q(workflow_type__group__in=user_groups)
                | Q(referred_to_groups__in=user_groups)
            )
            .distinct()
            .select_related("workflow_type", "current_state", "owner")
        )

    workflows, filter_params = _apply_report_filters(base_workflows, request)

    period = filter_params["period"]
    filename_suffix = f"_{period}" if period else ""
    if filter_params["overdue_only"]:
        filename_suffix += "_overdue"
    if filter_params["parent_workflow_id"]:
        filename_suffix += "_tree"

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="workflows_report{filename_suffix}.csv"'
    )
    response["HX-Trigger"] = "exportComplete"

    writer = csv.writer(response)
    now = timezone.now()

    # If filtering by parent, write a tree-style report
    if filter_params["parent_workflow"] and filter_params["parent_workflow_id"]:
        parent = filter_params["parent_workflow"]
        writer.writerow(["Workflow Tree Report"])
        writer.writerow(["Parent Workflow", parent.title])
        writer.writerow(["Type", parent.workflow_type.name])
        writer.writerow(["Status", parent.current_state.name])
        writer.writerow(
            ["Owner", parent.owner.get_full_name() or parent.owner.username]
        )
        writer.writerow(["Generated", now.strftime("%Y-%m-%d %H:%M")])
        writer.writerow([])
        writer.writerow(
            [
                "#",
                "Title",
                "Type",
                "Relationship",
                "State",
                "Terminal?",
                "Priority",
                "Owner",
                "Created",
                "Deadline",
                "Overdue",
            ]
        )
        for i, wf in enumerate(
            workflows.select_related(
                "workflow_type", "current_state", "owner", "relationship_type"
            ),
            1,
        ):
            is_overdue = (
                wf.deadline and wf.deadline < now and not wf.current_state.is_terminal
            )
            role = (
                "Parent"
                if str(wf.pk) == str(filter_params["parent_workflow_id"])
                else (
                    wf.relationship_type.relationship_label
                    if wf.relationship_type
                    else "Sub-workflow"
                )
            )
            writer.writerow(
                [
                    i,
                    wf.title,
                    wf.workflow_type.name,
                    role,
                    wf.current_state.name,
                    "Yes" if wf.current_state.is_terminal else "No",
                    wf.priority.capitalize(),
                    wf.owner.get_full_name() or wf.owner.username,
                    wf.created_at.strftime("%Y-%m-%d %H:%M"),
                    wf.deadline.strftime("%Y-%m-%d %H:%M") if wf.deadline else "",
                    "Yes" if is_overdue else "No",
                ]
            )
    else:
        writer.writerow(
            [
                "ID",
                "Title",
                "Type",
                "State",
                "Priority",
                "Owner",
                "Group",
                "Created",
                "Deadline",
                "Overdue",
            ]
        )
        for wf in workflows:
            is_overdue = (
                wf.deadline and wf.deadline < now and not wf.current_state.is_terminal
            )
            writer.writerow(
                [
                    wf.pk,
                    wf.title,
                    wf.workflow_type.name,
                    wf.current_state.name,
                    wf.get_priority_display()
                    if hasattr(wf, "get_priority_display")
                    else wf.priority,
                    wf.owner.get_full_name() or wf.owner.username,
                    wf.workflow_type.group.name,
                    wf.created_at.strftime("%Y-%m-%d %H:%M"),
                    wf.deadline.strftime("%Y-%m-%d %H:%M") if wf.deadline else "",
                    "Yes" if is_overdue else "No",
                ]
            )

    return response


@login_required
def reports_export_excel(request):
    """Export workflow report data as Excel (.xlsx)"""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    user = request.user
    user_groups = user.memberships.filter(is_active=True).values_list(
        "group", flat=True
    )
    if user.is_superuser:
        base_workflows = Workflow.objects.all().select_related(
            "workflow_type", "current_state", "owner"
        )
    else:
        base_workflows = (
            Workflow.objects.filter(
                Q(group_access__group__in=user_groups)
                | Q(workflow_type__group__in=user_groups)
                | Q(referred_to_groups__in=user_groups)
            )
            .distinct()
            .select_related("workflow_type", "current_state", "owner")
        )

    workflows, filter_params = _apply_report_filters(base_workflows, request)

    period = filter_params["period"]
    period_labels = {
        "weekly": "Weekly",
        "monthly": "Monthly",
        "quarterly": "Quarterly",
    }
    period_label = period_labels.get(period, "All Time")
    filename_suffix = f"_{period}" if period else ""
    if filter_params["overdue_only"]:
        filename_suffix += "_overdue"
    if filter_params["parent_workflow_id"]:
        filename_suffix += "_tree"

    wb = Workbook()

    # ── Summary sheet ──────────────────────────────────────────────────────────
    ws_summary = wb.active
    ws_summary.title = "Summary"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(
        start_color="1F4E79", end_color="1F4E79", fill_type="solid"
    )

    now = timezone.now()
    total = workflows.count()
    active = workflows.filter(current_state__is_terminal=False).count()
    completed = workflows.filter(current_state__is_terminal=True).count()
    overdue = workflows.filter(
        deadline__lt=now, current_state__is_terminal=False
    ).count()

    # Resolve workflow type name for summary
    wt_name = ""
    if filter_params["workflow_type_id"]:
        try:
            wt_name = WorkflowType.objects.get(
                pk=filter_params["workflow_type_id"]
            ).name
        except WorkflowType.DoesNotExist:
            wt_name = ""

    parent_label = (
        filter_params["parent_workflow"].title
        if filter_params["parent_workflow"]
        else "All Workflows"
    )

    summary_rows = [
        ["Metric", "Value"],
        ["Report Period", period_label],
        ["Workflow Type Filter", wt_name or "All Types"],
        ["Parent Workflow Filter", parent_label],
        ["Overdue Only", "Yes" if filter_params["overdue_only"] else "No"],
        ["Total Workflows", total],
        ["Active", active],
        ["Completed", completed],
        ["Overdue", overdue],
        ["Report Generated", now.strftime("%Y-%m-%d %H:%M")],
    ]
    for row in summary_rows:
        ws_summary.append(row)

    for cell in ws_summary[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    ws_summary.column_dimensions["A"].width = 25
    ws_summary.column_dimensions["B"].width = 30

    # ── Workflows sheet ────────────────────────────────────────────────────────
    ws = wb.create_sheet("Workflows")
    headers = [
        "ID",
        "Title",
        "Type",
        "State",
        "Priority",
        "Owner",
        "Group",
        "Created",
        "Deadline",
        "Overdue",
    ]
    ws.append(headers)

    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for wf in workflows:
        is_overdue = (
            wf.deadline and wf.deadline < now and not wf.current_state.is_terminal
        )
        ws.append(
            [
                wf.pk,
                wf.title,
                wf.workflow_type.name,
                wf.current_state.name,
                wf.priority.capitalize(),
                wf.owner.get_full_name() or wf.owner.username,
                wf.workflow_type.group.name,
                wf.created_at.replace(tzinfo=None),
                wf.deadline.replace(tzinfo=None) if wf.deadline else "",
                "Yes" if is_overdue else "No",
            ]
        )

    # Auto-width columns
    for col_idx, _ in enumerate(headers, 1):
        col_letter = get_column_letter(col_idx)
        max_len = max(
            (
                len(str(ws.cell(row=r, column=col_idx).value or ""))
                for r in range(1, ws.max_row + 1)
            ),
            default=10,
        )
        ws.column_dimensions[col_letter].width = min(max_len + 4, 50)

    # ── Workflow Tree sheet (only when parent filter is active) ────────────────
    if filter_params["parent_workflow"] and filter_params["parent_workflow_id"]:
        ws_tree = wb.create_sheet("Workflow Tree")
        tree_headers = [
            "#",
            "Title",
            "Type",
            "Relationship",
            "State",
            "Terminal?",
            "Priority",
            "Owner",
            "Created",
            "Deadline",
            "Overdue",
        ]
        ws_tree.append(tree_headers)
        for cell in ws_tree[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")
        for i, wf in enumerate(
            workflows.select_related(
                "workflow_type", "current_state", "owner", "relationship_type"
            ),
            1,
        ):
            is_overdue = (
                wf.deadline and wf.deadline < now and not wf.current_state.is_terminal
            )
            role = (
                "Parent"
                if str(wf.pk) == str(filter_params["parent_workflow_id"])
                else (
                    wf.relationship_type.relationship_label
                    if wf.relationship_type
                    else "Sub-workflow"
                )
            )
            ws_tree.append(
                [
                    i,
                    wf.title,
                    wf.workflow_type.name,
                    role,
                    wf.current_state.name,
                    "Yes" if wf.current_state.is_terminal else "No",
                    wf.priority.capitalize(),
                    wf.owner.get_full_name() or wf.owner.username,
                    wf.created_at.replace(tzinfo=None),
                    wf.deadline.replace(tzinfo=None) if wf.deadline else "",
                    "Yes" if is_overdue else "No",
                ]
            )
        for col_idx, _ in enumerate(tree_headers, 1):
            col_letter = get_column_letter(col_idx)
            max_len = max(
                (
                    len(str(ws_tree.cell(row=r, column=col_idx).value or ""))
                    for r in range(1, ws_tree.max_row + 1)
                ),
                default=10,
            )
            ws_tree.column_dimensions[col_letter].width = min(max_len + 4, 50)

    # ── By Type sheet ──────────────────────────────────────────────────────────
    ws_type = wb.create_sheet("By Type")
    ws_type.append(["Workflow Type", "Count"])
    for cell in ws_type[1]:
        cell.font = header_font
        cell.fill = header_fill
    by_type = (
        workflows.values("workflow_type__name")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    for row in by_type:
        ws_type.append([row["workflow_type__name"], row["count"]])
    ws_type.column_dimensions["A"].width = 30
    ws_type.column_dimensions["B"].width = 10

    # ── By State sheet ─────────────────────────────────────────────────────────
    ws_state = wb.create_sheet("By State")
    ws_state.append(["State", "Count"])
    for cell in ws_state[1]:
        cell.font = header_font
        cell.fill = header_fill
    by_state = (
        workflows.values("current_state__name")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    for row in by_state:
        ws_state.append([row["current_state__name"], row["count"]])
    ws_state.column_dimensions["A"].width = 30
    ws_state.column_dimensions["B"].width = 10

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    response = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="workflows_report{filename_suffix}.xlsx"'
    )
    response["HX-Trigger"] = "exportComplete"
    return response


@login_required
def reports_recent_activity_pdf(request):
    """Export activity or overview report as a nicely formatted PDF using WeasyPrint"""
    from django.template.loader import render_to_string
    from weasyprint import HTML

    # Get user's accessible workflows
    user = request.user
    user_groups = user.memberships.filter(is_active=True).values_list(
        "group", flat=True
    )
    if user.is_superuser:
        base_workflows = Workflow.objects.all().select_related(
            "workflow_type", "current_state", "owner"
        )
    else:
        base_workflows = (
            Workflow.objects.filter(
                Q(group_access__group__in=user_groups)
                | Q(workflow_type__group__in=user_groups)
                | Q(referred_to_groups__in=user_groups)
            )
            .distinct()
            .select_related("workflow_type", "current_state", "owner")
        )

    # Get report type (activity or overview)
    report_type = request.GET.get("report_type", "overview")

    # Check if this is a filtered report
    has_filters = any(
        [
            request.GET.get("period"),
            request.GET.get("workflow_type"),
            request.GET.get("overdue_only"),
            request.GET.get("parent_workflow"),
        ]
    )

    # Apply custom report filters
    workflows, filter_params = _apply_report_filters(base_workflows, request)

    # Enrich filter_params with workflow type name if applicable
    if filter_params.get("workflow_type_id"):
        try:
            wf_type = WorkflowType.objects.get(pk=filter_params["workflow_type_id"])
            filter_params["workflow_type_name"] = wf_type.name
        except WorkflowType.DoesNotExist:
            filter_params["workflow_type_name"] = None

    # Prepare data based on report type
    recent_transitions = None
    overview_stats = None

    if report_type == "activity":
        # Get recent transitions for activity report
        recent_transitions = (
            WorkflowTransitionLog.objects.filter(workflow__in=workflows)
            .select_related("workflow", "user", "from_state", "to_state", "transition")
            .order_by("-timestamp")[:50]
        )
        report_title = "Activity Report" if has_filters else "Recent Activity Report"
    else:
        # Overview report - collect statistics
        now = timezone.now()
        overview_stats = {
            "by_type": workflows.values("workflow_type__name")
            .annotate(count=Count("id"))
            .order_by("-count"),
            "by_state": workflows.values("current_state__name", "current_state__color")
            .annotate(count=Count("id"))
            .order_by("-count"),
            "by_priority": workflows.values("priority")
            .annotate(count=Count("id"))
            .order_by("priority"),
            "total": workflows.count(),
            "completed": workflows.filter(current_state__is_terminal=True).count(),
            "active": workflows.filter(current_state__is_terminal=False).count(),
            "overdue": workflows.filter(
                deadline__lt=now, current_state__is_terminal=False
            ).count(),
        }

        # Build hierarchical structure for parent-child workflows
        parent_workflows = (
            workflows.filter(parent_workflow__isnull=True)
            .select_related("workflow_type", "current_state", "owner")
            .prefetch_related(
                "sub_workflows__workflow_type",
                "sub_workflows__current_state",
                "sub_workflows__owner",
            )
            .order_by("created_at")
        )

        report_title = "Overview Report"

    # Prepare context for template
    context = {
        "workflows": workflows if report_type == "overview" else None,
        "recent_transitions": recent_transitions,
        "overview_stats": overview_stats,
        "parent_workflows": parent_workflows if report_type == "overview" else None,
        "generated_on": timezone.now(),
        "generated_by": user,
        "report_title": report_title,
        "report_type": report_type,
        "filter_params": filter_params if has_filters else None,
        "total_count": workflows.count()
        if report_type == "overview"
        else (len(recent_transitions) if recent_transitions else 0),
    }

    # Render HTML template
    html_string = render_to_string("workflows/pdf/report_export.html", context)

    # Generate PDF with base_url to resolve static files

    base_url = request.build_absolute_uri("/")[:-1]  # Remove trailing slash
    html = HTML(string=html_string, base_url=base_url)
    pdf = html.write_pdf()

    # Create response
    response = HttpResponse(pdf, content_type="application/pdf")
    filename_prefix = f"{report_type}_report"
    response["Content-Disposition"] = 'attachment; filename="{}_{}.pdf"'.format(
        filename_prefix, timezone.now().strftime("%Y%m%d_%H%M%S")
    )
    response["HX-Trigger"] = "exportComplete"

    return response


# ============================================================================
# NOTIFICATION VIEWS
# ============================================================================


def _create_notification(user, verb, title, message, workflow=None):
    """Create a single in-app notification for a user."""
    Notification.objects.create(
        user=user,
        verb=verb,
        title=title,
        message=message,
        workflow=workflow,
    )


def _notify_group_members(
    group, roles, verb, title, message, workflow=None, exclude_user=None
):
    """
    Create notifications for all active group members whose role is in `roles`.
    Optionally exclude the user who triggered the action.
    """
    qs = group.members.filter(role__in=roles, is_active=True).select_related("user")
    if exclude_user:
        qs = qs.exclude(user=exclude_user)
    seen = set()
    for membership in qs:
        if membership.user_id not in seen:
            seen.add(membership.user_id)
            _create_notification(membership.user, verb, title, message, workflow)


def notifications_json(request):
    """Return the current user's unread notifications as JSON for the navbar."""
    if not request.user.is_authenticated:
        return JsonResponse({"unread_count": 0, "notifications": []}, status=200)
    try:
        notifs = (
            Notification.objects.filter(user=request.user)
            .select_related("workflow")
            .order_by("-created_at")[:30]
        )
        unread_count = Notification.objects.filter(
            user=request.user, is_read=False
        ).count()

        data = [
            {
                "id": n.pk,
                "verb": n.verb,
                "title": n.title,
                "message": n.message,
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat(),
                "workflow_id": n.workflow_id,
                "workflow_title": n.workflow.title if n.workflow else None,
            }
            for n in notifs
        ]
        return JsonResponse({"unread_count": unread_count, "notifications": data})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_http_methods(["POST"])
def notifications_mark_read(request):
    """Mark one or all notifications as read."""
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False}, status=200)
    notif_id = request.POST.get("id")
    if notif_id:
        Notification.objects.filter(user=request.user, pk=notif_id).update(is_read=True)
    else:
        Notification.objects.filter(user=request.user, is_read=False).update(
            is_read=True
        )
    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def notifications_clear(request):
    """Delete all notifications for the current user."""
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False}, status=200)
    Notification.objects.filter(user=request.user).delete()
    return JsonResponse({"ok": True})


# ============================================================================
# SHAREPOINT API VIEWS
# ============================================================================


@require_http_methods(["GET"])
def sharepoint_test(request):
    """Test SharePoint configuration and return debug info."""
    try:
        from django.conf import settings

        debug_info = {
            "settings": {
                "SHAREPOINT_CLIENT_ID": bool(settings.SHAREPOINT_CLIENT_ID),
                "SHAREPOINT_CLIENT_SECRET": bool(settings.SHAREPOINT_CLIENT_SECRET),
                "SHAREPOINT_TENANT_ID": settings.SHAREPOINT_TENANT_ID,
                "SHAREPOINT_TOKEN_URL": settings.SHAREPOINT_TOKEN_URL,
                "SHAREPOINT_SCOPE": settings.SHAREPOINT_SCOPE,
            },
            "cached_tokens": list(
                SharePointToken.objects.values_list("id", "is_active", "expires_at")
            ),
        }

        # Try to get a token
        try:
            token_data = get_application_token()
            debug_info["token_test"] = "SUCCESS"
            debug_info["token_keys"] = list(token_data.keys())
        except Exception as e:
            debug_info["token_test"] = f"FAILED: {str(e)}"

        return JsonResponse(debug_info)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_http_methods(["GET"])
@login_required
def sharepoint_sites(request):
    """Get SharePoint sites where the current user is a member."""
    try:
        # First try to get token
        # print("Attempting to get SharePoint token...")
        # token_data = get_application_token()
        # print("Token obtained successfully")

        # Run async function in sync context
        # loop = asyncio.new_event_loop()
        # asyncio.set_event_loop(loop)
        # sites_data = loop.run_until_complete(get_all_sites(token_data))
        # loop.close()

        # print("Sites data received:", sites_data)

        # Get site IDs where current user is a member
        user_member_sites = SiteMember.objects.filter(user=request.user).values_list(
            "site__site_id", flat=True
        )

        # Get Site info for user's memberships
        sites = Site.objects.filter(site_id__in=user_member_sites).values_list(
            "site_id", "name", "url", "is_personal_site", flat=True
        )

        print(sites)

        # Update local database with sites and filter for user's memberships
        # sites = []
        # for site_info in sites_data.get("value", []):
        #     site_id = site_info["id"]

        #     # Update or create site in database
        #     site, created = Site.objects.update_or_create(
        #         site_id=site_id,
        #         defaults={
        #             "name": site_info.get(
        #                 "displayName", site_info.get("name", "Unknown")
        #             ),
        #             "url": site_info.get("webUrl", ""),
        #             "is_personal_site": site_info.get("isPersonalSite", False),
        #         },
        #     )

        #     # Only include sites where user is a member
        #     if site_id in user_member_sites:
        #         sites.append(
        #             {
        #                 "id": site.site_id,
        #                 "name": site.name,
        #                 "url": site.url,
        #                 "is_personal_site": site.is_personal_site,
        #             }
        #         )

        return JsonResponse({"sites": sites})

    except Exception as e:
        print("Error in sharepoint_sites:", str(e))
        import traceback

        traceback.print_exc()
        return JsonResponse({"error": str(e)}, status=500)


@require_http_methods(["GET"])
@login_required
def sharepoint_site_drives(request, site_id):
    """Get all drives for a specific SharePoint site."""
    try:
        # Check if user is a member of this site
        if not SiteMember.objects.filter(
            user=request.user, site__site_id=site_id
        ).exists():
            return JsonResponse(
                {"error": "Access denied: You are not a member of this site"},
                status=403,
            )

        token_data = get_application_token()

        # Run async function in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        drives_data = loop.run_until_complete(get_site_drives(token_data, site_id))
        loop.close()

        # Update local database with drives
        site = Site.objects.get(site_id=site_id)
        drives = []
        for drive_info in drives_data.get("value", []):
            drive, created = Drive.objects.update_or_create(
                site=site,
                drive_id=drive_info["id"],
                defaults={"name": drive_info.get("name", "Unknown")},
            )
            drives.append(
                {"id": drive.drive_id, "name": drive.name, "site_id": site.site_id}
            )

        return JsonResponse({"drives": drives})

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_http_methods(["GET"])
@login_required
def sharepoint_drive_folders(request, drive_id):
    """Get root folders for a specific drive."""
    try:
        token_data = get_application_token()

        # Run async function in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        items_data = loop.run_until_complete(get_drive_items(token_data, drive_id))
        loop.close()

        # Filter for folders only
        folders = []
        for item in items_data.get("value", []):
            if "folder" in item:
                folders.append(
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "web_url": item.get("webUrl", ""),
                        "parent_reference": item.get("parentReference", {}),
                    }
                )

        # Create response with appropriate messaging
        response_data = {"folders": folders}

        if not folders:
            response_data["message"] = (
                "No folders in the root directory. You can select files directly from the root folder."
            )
            response_data["show_root_files"] = (
                True  # Flag to indicate UI should show root files option
            )
        else:
            response_data["message"] = (
                f"Found {len(folders)} folder(s) in the root directory"
            )
            response_data["show_root_files"] = True  # Still allow root file access

        return JsonResponse(response_data)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_http_methods(["GET"])
@login_required
def sharepoint_folder_items(request, folder_id):
    """Get items in a specific SharePoint folder or root folder."""
    try:
        # Get drive_id from query parameter or fetch from folder info
        drive_id = request.GET.get("drive_id")
        if not drive_id:
            # For root folder, we can't get drive_id from folder since it doesn't exist in DB
            if folder_id == "root":
                return JsonResponse(
                    {"error": "drive_id parameter required for root folder"}, status=400
                )
            # Try to get drive_id from the folder itself
            folder = SharePointFolder.objects.filter(folder_id=folder_id).first()
            if folder:
                drive_id = folder.drive.drive_id
            else:
                return JsonResponse(
                    {"error": "drive_id parameter required"}, status=400
                )

        token_data = get_application_token()

        # Run async function in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        items_data = loop.run_until_complete(
            get_folder_items(token_data, drive_id, folder_id)
        )
        loop.close()

        # Process items to separate files and folders
        files = []
        folders = []

        for item in items_data.get("value", []):
            if item.get("folder"):
                # This is a folder
                folders.append(
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "parent_reference": item.get("parentReference", {}),
                    }
                )
            elif item.get("file"):
                # This is a file
                files.append(
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "size": item.get("size", 0),
                        "mimetype": item.get("file", {}).get("mimeType", ""),
                        "web_url": item.get("webUrl", ""),
                        "download_url": item.get("@microsoft.graph.downloadUrl", ""),
                        "created_at": item.get("createdDateTime"),
                        "modified_at": item.get("lastModifiedDateTime"),
                    }
                )

        # Create response with appropriate messaging
        response_data = {"files": files, "folders": folders}

        # Add message about folder location
        if folder_id == "root":
            response_data["folder_info"] = {"name": "Root Folder", "is_root": True}
        else:
            # Try to get folder name from the items data or database
            folder_name = "Unknown Folder"
            if folder_id != "root":
                folder = SharePointFolder.objects.filter(folder_id=folder_id).first()
                if folder:
                    folder_name = folder.get_full_path()
            response_data["folder_info"] = {"name": folder_name, "is_root": False}

        # Add message if no files or folders
        if not files and not folders:
            if folder_id == "root":
                response_data["message"] = "No files or folders in the root directory"
            else:
                response_data["message"] = "No files or folders in this directory"

        return JsonResponse(response_data)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@htmx_partial("workflows/admin/sharepoint_sites.html")
def sharepoint_admin_sites(request):
    """SharePoint administration - manage SharePoint sites"""
    if not request.user.is_staff and not request.user.is_superuser:
        messages.error(request, "You do not have permission to access this page.")
        return redirect("dashboard")

    sites = Site.objects.all().select_related().order_by("name")

    search = request.GET.get("search")
    is_personal = request.GET.get("is_personal")

    if search:
        sites = sites.filter(
            Q(name__icontains=search)
            | Q(url__icontains=search)
            | Q(site_id__icontains=search)
        )
    if is_personal:
        sites = sites.filter(is_personal_site=is_personal == "true")

    # Get member counts for each site
    sites_with_counts = []
    team_sites_count = 0
    personal_sites_count = 0

    for site in sites:
        member_count = SiteMember.objects.filter(site=site).count()
        site_data = {"site": site, "member_count": member_count}
        sites_with_counts.append(site_data)

        if site.is_personal_site:
            personal_sites_count += 1
        else:
            team_sites_count += 1

    context = {
        "sites_with_counts": sites_with_counts,
        "team_sites_count": team_sites_count,
        "personal_sites_count": personal_sites_count,
    }

    return context
    # return render(request, "workflows/admin/sharepoint_sites.html", context)


@login_required
@htmx_partial("workflows/admin/sharepoint_members.html")
def sharepoint_admin_members(request):
    """SharePoint administration - manage site memberships"""
    if not request.user.is_staff and not request.user.is_superuser:
        messages.error(request, "You do not have permission to access this page.")
        return redirect("dashboard")

    site_members = (
        SiteMember.objects.all()
        .select_related("site", "user")
        .order_by("site__name", "user__username")
    )

    search = request.GET.get("search")
    site_filter = request.GET.get("site")

    if search:
        site_members = site_members.filter(
            Q(user__username__icontains=search)
            | Q(user__first_name__icontains=search)
            | Q(user__last_name__icontains=search)
            | Q(site__name__icontains=search)
        )
    if site_filter:
        site_members = site_members.filter(site__site_id=site_filter)

    # Get all sites for filter dropdown
    sites = Site.objects.all().order_by("name")

    # All users for the add-member modal
    all_users = User.objects.all().order_by("last_name", "first_name")

    # Calculate statistics
    unique_users_count = len(set(member.user_id for member in site_members))
    team_sites_count = sum(
        1 for member in site_members if not member.site.is_personal_site
    )
    personal_sites_count = sum(
        1 for member in site_members if member.site.is_personal_site
    )

    context = {
        "site_members": site_members,
        "sites": sites,
        "all_users": all_users,
        "unique_users_count": unique_users_count,
        "team_sites_count": team_sites_count,
        "personal_sites_count": personal_sites_count,
    }

    return context


@login_required
@require_http_methods(["POST"])
def api_sharepoint_members_create(request):
    """API: Add a user to a SharePoint site."""
    if not request.user.is_staff and not request.user.is_superuser:
        return JsonResponse(
            {"success": False, "error": "Permission denied."}, status=403
        )

    user_id = request.POST.get("user_id")
    site_id = request.POST.get("site_id")

    if not user_id or not site_id:
        return JsonResponse(
            {"success": False, "error": "User and site are required."}, status=400
        )

    try:
        user = User.objects.get(pk=user_id)
        site = Site.objects.get(site_id=site_id)
    except User.DoesNotExist:
        return JsonResponse({"success": False, "error": "User not found."}, status=404)
    except Site.DoesNotExist:
        return JsonResponse({"success": False, "error": "Site not found."}, status=404)

    _, created = SiteMember.objects.get_or_create(user=user, site=site)
    if not created:
        return JsonResponse(
            {"success": False, "error": "This user is already a member of that site."},
            status=400,
        )

    return JsonResponse({"success": True})


@login_required
@require_http_methods(["DELETE"])
def api_sharepoint_members_delete(request, member_id):
    """API: Remove a user from a SharePoint site."""
    if not request.user.is_staff and not request.user.is_superuser:
        return JsonResponse(
            {"success": False, "error": "Permission denied."}, status=403
        )

    try:
        member = SiteMember.objects.get(pk=member_id)
        member.delete()
        return JsonResponse({"success": True})
    except SiteMember.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Member not found."}, status=404
        )


@login_required
def attachment_link_sharepoint(request):
    """Link an existing SharePoint file as an attachment."""
    try:
        data = json.loads(request.body)
        file_id = data.get("file_id")
        site_id = data.get("site_id")
        drive_id = data.get("drive_id")
        folder_id = data.get("folder_id")
        attachment_type = data.get("type", "document")
        workflow_id = data.get("workflow_id")

        if not all([file_id, site_id, drive_id]):
            return JsonResponse(
                {"error": "file_id, site_id, and drive_id required"}, status=400
            )

        # Check if user is a member of this site
        if not SiteMember.objects.filter(
            user=request.user, site__site_id=site_id
        ).exists():
            return JsonResponse(
                {"error": "Access denied: You are not a member of this site"},
                status=403,
            )

        # Get SharePoint objects
        site = Site.objects.get(site_id=site_id)
        drive = Drive.objects.get(drive_id=drive_id)
        folder = (
            SharePointFolder.objects.filter(folder_id=folder_id).first()
            if folder_id
            else None
        )

        # Get file metadata from SharePoint
        token_data = get_application_token()

        # Run async function in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def fetch_file_metadata():
            file_url = (
                f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{file_id}"
            )
            headers = {"Authorization": f"Bearer {token_data['access_token']}"}

            async with httpx.AsyncClient() as client:
                response = await client.get(file_url, headers=headers)
                response.raise_for_status()
                return response.json()

        file_data = loop.run_until_complete(fetch_file_metadata())
        loop.close()

        # Create attachment record
        attachment = Attachment.objects.create(
            name=file_data["name"],
            drive_id=drive_id,
            item_id=file_id,
            mimetype=file_data.get("file", {}).get("mimeType", ""),
            size=file_data.get("size", 0),
            download_url=file_data.get("@microsoft.graph.downloadUrl", ""),
            sharepoint_web_url=file_data.get("webUrl", ""),
            sharepoint_site=site,
            sharepoint_drive=drive,
            sharepoint_folder=folder,
            sharepoint_folder_path=folder.get_full_path() if folder else "",
            type=attachment_type,
            uploaded_by=request.user if request.user.is_authenticated else None,
        )

        # Link to workflow if provided
        if workflow_id:
            try:
                workflow = Workflow.objects.get(pk=workflow_id)
                attachment.related_workflow = workflow
                attachment.save()
                workflow.attachments.add(attachment)
            except Workflow.DoesNotExist:
                pass

        return JsonResponse(
            {
                "success": True,
                "attachment": {
                    "id": attachment.id,
                    "name": attachment.name,
                    "size": attachment.size,
                    "url": attachment.get_sharepoint_url(),
                    "type": attachment.type,
                },
            }
        )

    except IntegrityError as e:
        error_msg = str(e).lower()
        if "unique constraint" in error_msg and "item_id" in error_msg:
            return JsonResponse(
                {"error": "This file is already attached to this workflow"}, status=400
            )
        return JsonResponse(
            {"error": "Database error: Unable to save attachment"}, status=500
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def attachment_upload(request):
    """Upload a file to SharePoint."""
    try:
        if "file" not in request.FILES:
            return JsonResponse({"error": "No file provided"}, status=400)

        file_obj = request.FILES["file"]
        site_id = request.POST.get("site_id")
        drive_id = request.POST.get("drive_id")
        folder_id = request.POST.get("folder_id")
        attachment_type = request.POST.get("type", "document")
        workflow_id = request.POST.get("workflow_id")

        if not all([site_id, drive_id]):
            return JsonResponse({"error": "site_id and drive_id required"}, status=400)

        # Get SharePoint objects
        site = Site.objects.get(site_id=site_id)
        drive = Drive.objects.get(drive_id=drive_id)

        # Read file content
        file_content = file_obj.read()

        # Get token and upload
        token_data = get_application_token()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Use root folder if no folder_id provided
        target_folder_id = folder_id if folder_id else "root"
        upload_result = loop.run_until_complete(
            upload_file(
                token_data, drive_id, target_folder_id, file_obj.name, file_content
            )
        )
        loop.close()

        # Create attachment record
        attachment = Attachment.objects.create(
            name=file_obj.name,
            drive_id=drive_id,
            item_id=upload_result["id"],
            mimetype=upload_result.get("file", {}).get("mimeType", ""),
            size=upload_result.get("size", 0),
            download_url=upload_result.get("@microsoft.graph.downloadUrl", ""),
            sharepoint_web_url=upload_result.get("webUrl", ""),
            sharepoint_site=site,
            sharepoint_drive=drive,
            type=attachment_type,
            uploaded_by=request.user if request.user.is_authenticated else None,
        )

        # Link to workflow if provided
        if workflow_id:
            try:
                workflow = Workflow.objects.get(pk=workflow_id)
                attachment.related_workflow = workflow
                attachment.save()
                # Also add to workflow's many-to-many relationship
                workflow.attachments.add(attachment)
            except Workflow.DoesNotExist:
                pass  # Workflow doesn't exist, but attachment is still created

        return JsonResponse(
            {
                "success": True,
                "attachment": {
                    "id": attachment.id,
                    "name": attachment.name,
                    "size": attachment.size,
                    "url": attachment.get_sharepoint_url(),
                    "type": attachment.type,
                },
            }
        )

    except IntegrityError as e:
        error_msg = str(e).lower()
        if "unique constraint" in error_msg and "item_id" in error_msg:
            return JsonResponse(
                {"error": "This file is already exists in this location"}, status=400
            )
        return JsonResponse(
            {"error": "Database error: Unable to save attachment"}, status=500
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_http_methods(["GET", "DELETE"])
def attachment_detail(request, pk):
    """Get or delete attachment details."""
    try:
        attachment = Attachment.objects.get(pk=pk)

        if request.method == "GET":
            return JsonResponse(
                {
                    "id": attachment.id,
                    "name": attachment.name,
                    "size": attachment.size,
                    "mimetype": attachment.mimetype,
                    "url": attachment.get_sharepoint_url(),
                    "type": attachment.type,
                    "created_at": attachment.created_at.isoformat(),
                    "uploaded_by": attachment.uploaded_by.username
                    if attachment.uploaded_by
                    else None,
                }
            )

        elif request.method == "DELETE":
            attachment.delete()
            return JsonResponse({"success": True})

    except Attachment.DoesNotExist:
        return JsonResponse({"error": "Attachment not found"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@htmx_partial("workflows/attachments.html")
def workflow_attachments(request, pk):
    """View and manage attachments for a specific workflow."""
    workflow = get_object_or_404(Workflow, pk=pk)

    # Check if user can view this workflow
    if not workflow.can_user_view(request.user):
        messages.error(request, "You don't have permission to view this workflow.")
        return redirect("workflow_list")

    context = {
        "workflow": workflow,
    }

    return context


# ============================================================================
# ADMIN VIEWS
# ============================================================================


@login_required
@htmx_partial("workflows/admin/user_admin.html")
def user_admin(request):
    """User administration - manage users and their basic information"""
    if not request.user.is_staff and not request.user.is_superuser:
        messages.error(request, "You do not have permission to access this page.")
        return redirect("dashboard")

    users = User.objects.all().select_related("department", "supervisor")

    search = request.GET.get("search")
    employee_type = request.GET.get("employee_type")
    is_active = request.GET.get("is_active")

    if search:
        users = users.filter(
            Q(username__icontains=search)
            | Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
            | Q(email__icontains=search)
        )
    if employee_type:
        users = users.filter(employee_type=employee_type)
    if is_active:
        users = users.filter(is_active=is_active == "true")

    context = {
        "users": users,
        "employee_types": User.EMPLOYEE_TYPE_CHOICES,
    }

    return context


@login_required
@htmx_partial("workflows/admin/user_admin_groups.html")
def user_admin_groups(request):
    """User administration - manage user group memberships"""
    if not request.user.is_staff and not request.user.is_superuser:
        messages.error(request, "You do not have permission to access this page.")
        return redirect("dashboard")

    memberships = GroupMembership.objects.all().select_related("user", "group", "role")

    user_filter = request.GET.get("user")
    group_filter = request.GET.get("group")
    role_filter = request.GET.get("role")
    is_active = request.GET.get("is_active")

    if user_filter:
        memberships = memberships.filter(user_id=user_filter)
    if group_filter:
        memberships = memberships.filter(group_id=group_filter)
    if role_filter:
        memberships = memberships.filter(role_id=role_filter)
    if is_active:
        memberships = memberships.filter(is_active=is_active == "true")

    users = User.objects.all()
    groups = Group.objects.all()
    roles = Role.objects.all()

    context = {
        "memberships": memberships,
        "users": users,
        "groups": groups,
        "roles": roles,
    }

    return context


@login_required
@htmx_partial("workflows/admin/user_admin_roles.html")
def user_admin_roles(request):
    """User administration - manage user roles and permissions"""
    if not request.user.is_staff and not request.user.is_superuser:
        messages.error(request, "You do not have permission to access this page.")
        return redirect("dashboard")

    roles = Role.objects.all()

    context = {
        "roles": roles,
    }

    return context


@login_required
@htmx_partial("workflows/admin/user_admin_workflow_types.html")
def user_admin_workflow_types(request):
    """User administration - manage workflow types"""
    if not request.user.is_staff and not request.user.is_superuser:
        messages.error(request, "You do not have permission to access this page.")
        return redirect("dashboard")

    workflow_types = WorkflowType.objects.all().prefetch_related(
        "states", "transitions"
    )

    active_workflow_types = WorkflowType.objects.filter(enabled=True).prefetch_related(
        "states", "transitions"
    )

    context = {
        "workflow_types": workflow_types,
        "active_workflow_types": active_workflow_types,
    }

    return context


@login_required
@htmx_partial("workflows/admin/workflow_type_create.html")
def workflow_type_create(request):
    """Create a new workflow type template"""
    if not request.user.is_staff and not request.user.is_superuser:
        messages.error(request, "You do not have permission to access this page.")
        return redirect("dashboard")

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        group_id = request.POST.get("group")  # This comes from the hidden field
        enabled = request.POST.get("enabled") == "on"
        create_roles_str = request.POST.get("create_roles", "")
        notify_roles_str = request.POST.get("notify_roles", "")

        # Parse comma-separated roles from dropdown
        create_roles = (
            [
                role_id.strip()
                for role_id in create_roles_str.split(",")
                if role_id.strip()
            ]
            if create_roles_str
            else []
        )
        notify_roles = (
            [
                role_id.strip()
                for role_id in notify_roles_str.split(",")
                if role_id.strip()
            ]
            if notify_roles_str
            else []
        )

        # Validation
        errors = []
        if not name:
            errors.append("Template name is required.")
        elif len(name) > 100:
            errors.append("Template name cannot exceed 100 characters.")

        if not group_id:
            errors.append(
                "Owner group is required. Please select a valid group from the dropdown."
            )

        if not create_roles:
            errors.append("At least one create role must be selected.")

        if errors:
            for error in errors:
                messages.error(request, error)
        else:
            try:
                group = Group.objects.get(id=group_id)

                # Check if workflow type with this name already exists
                if WorkflowType.objects.filter(name__iexact=name).exists():
                    messages.error(
                        request,
                        f"A workflow template with the name '{name}' already exists.",
                    )
                else:
                    workflow_type = WorkflowType.objects.create(
                        name=name,
                        description=description,
                        group=group,
                        enabled=enabled,
                    )
                    workflow_type.create_roles.set(create_roles)
                    workflow_type.notify_roles.set(notify_roles)

                    # Validate the workflow type
                    try:
                        workflow_type.clean()
                        workflow_type.save()
                        messages.success(
                            request, f"Workflow template '{name}' created successfully."
                        )
                        return redirect("admin_workflow_types")
                    except ValidationError as e:
                        workflow_type.delete()
                        for error in e.messages:
                            messages.error(request, error)

            except Group.DoesNotExist:
                messages.error(
                    request,
                    "Selected group does not exist. Please select a valid group.",
                )
            except Exception as e:
                messages.error(request, f"Error creating workflow template: {str(e)}")

    groups = Group.objects.all()
    roles = Role.objects.all()

    # Create a mapping of group IDs to available roles
    group_roles = {}
    group_member_counts = {}
    # groups_json = []

    for group in groups:
        # Get unique roles available in this group through memberships
        group_id_str = str(group.id)
        available_roles = group.members.values_list("role__id", "role__name").distinct()
        group_roles[group_id_str] = [
            {"id": str(role_id), "name": role_name}
            for role_id, role_name in available_roles
        ]

        group_member_counts[group_id_str] = group.members.count()

    groups_json = [{"id": str(g.id), "name": g.name} for g in groups]

    roles_json = [{"id": str(r.id), "name": r.name} for r in roles]
    context = {
        "groups": groups_json,
        "roles": roles_json,
        "group_roles": json.dumps(group_roles),
        "group_member_counts": group_member_counts,
    }

    return render(request, "workflows/admin/workflow_type_create.html", context)


@login_required
@htmx_partial("workflows/admin/workflow_type_edit.html")
def workflow_type_edit(request, pk):
    """Edit an existing workflow type template"""
    if not request.user.is_staff and not request.user.is_superuser:
        messages.error(request, "You do not have permission to access this page.")
        return redirect("dashboard")

    workflow_type = get_object_or_404(WorkflowType, pk=pk)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        group_id = request.POST.get("group")
        enabled = request.POST.get("enabled") == "on"
        create_roles_str = request.POST.get("create_roles", "")
        notify_roles_str = request.POST.get("notify_roles")

        # Parse comma-separated roles from dropdown
        create_roles = (
            [
                role_id.strip()
                for role_id in create_roles_str.split(",")
                if role_id.strip()
            ]
            if create_roles_str
            else []
        )
        notify_roles = None
        if notify_roles_str is not None:
            notify_roles = [
                role_id.strip()
                for role_id in notify_roles_str.split(",")
                if role_id.strip()
            ]

        # Validation
        errors = []
        if not name:
            errors.append("Template name is required.")
        elif len(name) > 100:
            errors.append("Template name cannot exceed 100 characters.")

        if not group_id:
            errors.append(
                "Owner group is required. Please select a valid group from the dropdown."
            )

        if not create_roles:
            errors.append("At least one create role must be selected.")

        if errors:
            for error in errors:
                messages.error(request, error)
        else:
            try:
                group = Group.objects.get(id=group_id)

                # Check if workflow type with this name already exists (excluding current)
                if (
                    WorkflowType.objects.filter(name__iexact=name)
                    .exclude(pk=workflow_type.pk)
                    .exists()
                ):
                    messages.error(
                        request,
                        f"A workflow template with the name '{name}' already exists.",
                    )
                else:
                    # Update workflow type
                    workflow_type.name = name
                    workflow_type.description = description
                    workflow_type.group = group
                    workflow_type.enabled = enabled
                    workflow_type.save()
                    workflow_type.create_roles.set(create_roles)
                    if notify_roles is not None:
                        workflow_type.notify_roles.set(notify_roles)

                    # Validate the workflow type
                    try:
                        workflow_type.clean()
                        workflow_type.save()
                        messages.success(
                            request, f"Workflow template '{name}' updated successfully."
                        )
                        return redirect("admin_workflow_types")
                    except ValidationError as e:
                        for error in e.messages:
                            messages.error(request, error)

            except Group.DoesNotExist:
                messages.error(
                    request,
                    "Selected group does not exist. Please select a valid group.",
                )
            except Exception as e:
                messages.error(request, f"Error updating workflow template: {str(e)}")

    groups = Group.objects.all()
    roles = Role.objects.all()

    # Create a mapping of group IDs to available roles
    group_roles = {}
    for group in groups:
        # Get unique roles available in this group through memberships
        available_roles = group.members.values_list("role__id", "role__name").distinct()
        group_roles[str(group.id)] = [
            {"id": str(role_id), "name": role_name}
            for role_id, role_name in available_roles
        ]

    context = {
        "workflow_type": workflow_type,
        "groups": groups,
        "roles": roles,
        "group_roles": json.dumps(group_roles),
        "is_edit": True,
    }

    return context


@login_required
@htmx_partial("workflows/admin/group_admin.html")
def group_admin(request):
    """Group administration - manage groups and their hierarchies"""
    if not request.user.is_staff and not request.user.is_superuser:
        messages.error(request, "You do not have permission to access this page.")
        return redirect("dashboard")

    groups = Group.objects.all().select_related("parent")

    search = request.GET.get("search")
    group_type = request.GET.get("group_type")

    if search:
        groups = groups.filter(
            Q(name__icontains=search) | Q(description__icontains=search)
        )
    if group_type:
        groups = groups.filter(group_type=group_type)

    context = {
        "groups": groups,
    }

    return context


@login_required
@htmx_partial("workflows/admin/workflow_type_states.html")
def workflow_type_states(request, pk):
    """Manage workflow states for a workflow type"""
    workflow_type = get_object_or_404(WorkflowType, pk=pk)

    # Check permissions - user should be admin or have appropriate roles
    if not request.user.is_staff:
        messages.error(request, "You do not have permission to manage workflow states.")
        return redirect("admin_workflow_types")

    if request.method == "POST":
        action = request.POST.get("action")

        try:
            if action == "create":
                name = request.POST.get("name", "").strip()
                description = request.POST.get("description", "").strip()
                order = request.POST.get("order", 0)
                is_initial = request.POST.get("is_initial") == "on"
                is_terminal = request.POST.get("is_terminal") == "on"

                if not name:
                    return JsonResponse(
                        {"success": False, "error": "State name is required"}
                    )

                # If this is set as initial, unset all other initial states
                if is_initial:
                    State.objects.filter(
                        workflow_type=workflow_type, is_initial=True
                    ).update(is_initial=False)

                # If this is set as terminal, unset all other terminal states
                if is_terminal:
                    State.objects.filter(
                        workflow_type=workflow_type, is_terminal=True
                    ).update(is_terminal=False)

                state = State.objects.create(
                    workflow_type=workflow_type,
                    name=name,
                    description=description,
                    order=int(order),
                    is_initial=is_initial,
                    is_terminal=is_terminal,
                )

                return JsonResponse(
                    {
                        "success": True,
                        "state": {
                            "id": state.pk,
                            "name": state.name,
                            "description": state.description,
                            "order": state.order,
                            "is_initial": state.is_initial,
                            "is_terminal": state.is_terminal,
                        },
                    }
                )

            elif action == "get_state":
                state_id = request.POST.get("state_id")
                print(f"DEBUG GET_STATE: Received state_id: {state_id}")
                print(f"DEBUG GET_STATE: workflow_type.pk: {workflow_type.pk}")

                # First check if state exists at all
                try:
                    state_check = State.objects.get(pk=state_id)
                    print(
                        f"DEBUG GET_STATE: Found state with ID {state_id}: {state_check.name}"
                    )
                    print(
                        f"DEBUG GET_STATE: State's workflow_type: {state_check.workflow_type.pk}"
                    )
                except State.DoesNotExist:
                    print(f"DEBUG GET_STATE: No state found with ID {state_id}")
                    return JsonResponse(
                        {
                            "success": False,
                            "error": f"No state found with ID {state_id}",
                        }
                    )

                state = get_object_or_404(
                    State, pk=state_id, workflow_type=workflow_type
                )
                print("DEBUG GET_STATE: Successfully retrieved state for workflow type")

                return JsonResponse(
                    {
                        "success": True,
                        "state": {
                            "id": state.pk,
                            "name": state.name,
                            "description": state.description,
                            "order": state.order,
                            "is_initial": state.is_initial,
                            "is_terminal": state.is_terminal,
                        },
                    }
                )

            elif action == "edit":
                state_id = request.POST.get("state_id")
                print(f"DEBUG: Received state_id: {state_id}")
                print(f"DEBUG: workflow_type.pk: {workflow_type.pk}")
                print(f"DEBUG: workflow_type.name: {workflow_type.name}")

                # First check if state exists at all
                try:
                    state_check = State.objects.get(pk=state_id)
                    print(f"DEBUG: Found state with ID {state_id}: {state_check.name}")
                    print(
                        f"DEBUG: State's workflow_type: {state_check.workflow_type.pk}"
                    )
                except State.DoesNotExist:
                    print(f"DEBUG: No state found with ID {state_id}")
                    return JsonResponse(
                        {
                            "success": False,
                            "error": f"No state found with ID {state_id}",
                        }
                    )

                state = get_object_or_404(
                    State, pk=state_id, workflow_type=workflow_type
                )
                print("DEBUG: Successfully retrieved state for workflow type")

                name = request.POST.get("name", "").strip()
                description = request.POST.get("description", "").strip()
                order = request.POST.get("order", 0)
                is_initial = request.POST.get("is_initial") == "on"
                is_terminal = request.POST.get("is_terminal") == "on"

                # Debug logging
                print(
                    f"Edit state - ID: {state_id}, Name: {name}, is_initial: {is_initial}, is_terminal: {is_terminal}"
                )

                if not name:
                    return JsonResponse(
                        {"success": False, "error": "State name is required"}
                    )

                # If this is set as initial, unset all other initial states
                if is_initial and not state.is_initial:
                    State.objects.filter(
                        workflow_type=workflow_type, is_initial=True
                    ).update(is_initial=False)

                # If this is set as terminal, unset all other terminal states
                if is_terminal and not state.is_terminal:
                    State.objects.filter(
                        workflow_type=workflow_type, is_terminal=True
                    ).update(is_terminal=False)

                state.name = name
                state.description = description
                state.order = int(order)
                state.is_initial = is_initial
                state.is_terminal = is_terminal

                try:
                    state.save()
                    print(f"State saved successfully: {state.name}")
                except Exception as e:
                    print(f"Error saving state: {str(e)}")
                    return JsonResponse(
                        {"success": False, "error": f"Error saving state: {str(e)}"}
                    )

                return JsonResponse(
                    {
                        "success": True,
                        "state": {
                            "id": state.pk,
                            "name": state.name,
                            "description": state.description,
                            "order": state.order,
                            "is_initial": state.is_initial,
                            "is_terminal": state.is_terminal,
                        },
                    }
                )

            elif action == "delete":
                state_id = request.POST.get("state_id")
                state = get_object_or_404(
                    State, pk=state_id, workflow_type=workflow_type
                )

                # Check if state is being used by any transitions
                transition_count = Transition.objects.filter(
                    Q(from_state=state) | Q(to_state=state)
                ).count()

                if transition_count > 0:
                    return JsonResponse(
                        {
                            "success": False,
                            "error": f"Cannot delete state: it is used by {transition_count} transition(s)",
                        }
                    )

                state.delete()
                return JsonResponse({"success": True})

        except Exception as e:
            print(f"Unexpected error in workflow_type_states: {str(e)}")
            return JsonResponse(
                {"success": False, "error": f"Unexpected error: {str(e)}"}
            )

    # GET request
    states = State.objects.filter(workflow_type=workflow_type).order_by("order", "name")

    context = {
        "workflow_type": workflow_type,
        "states": states,
    }

    return context


@login_required
@htmx_partial("workflows/admin/workflow_type_transitions.html")
def workflow_type_transitions(request, pk):
    """Manage workflow transitions for a workflow type"""
    workflow_type = get_object_or_404(WorkflowType, pk=pk)

    # Check permissions - user should be admin or have appropriate roles
    if not request.user.is_staff:
        messages.error(
            request, "You do not have permission to manage workflow transitions."
        )
        return redirect("admin_workflow_types")

    if request.method == "POST":
        action = request.POST.get("action")

        try:
            if action == "create":
                name = request.POST.get("name", "").strip()
                from_state_id = request.POST.get("from_state")
                to_state_id = request.POST.get("to_state")
                role_ids = request.POST.getlist("roles")

                if not name:
                    return JsonResponse(
                        {"success": False, "error": "Transition name is required"}
                    )

                if not from_state_id or not to_state_id:
                    return JsonResponse(
                        {
                            "success": False,
                            "error": "Both from and to states are required",
                        }
                    )

                if from_state_id == to_state_id:
                    return JsonResponse(
                        {
                            "success": False,
                            "error": "From and to states cannot be the same",
                        }
                    )

                from_state = get_object_or_404(
                    State, pk=from_state_id, workflow_type=workflow_type
                )
                to_state = get_object_or_404(
                    State, pk=to_state_id, workflow_type=workflow_type
                )

                transition = Transition.objects.create(
                    workflow_type=workflow_type,
                    name=name,
                    from_state=from_state,
                    to_state=to_state,
                )

                # Add allowed roles
                if role_ids:
                    transition.allowed_roles.set(Role.objects.filter(id__in=role_ids))

                # Add notify roles
                notify_role_ids = request.POST.getlist("notify_roles")
                transition.notify_roles.set(Role.objects.filter(id__in=notify_role_ids))

                return JsonResponse(
                    {
                        "success": True,
                        "transition": {
                            "id": transition.pk,
                            "name": transition.name,
                            "from_state": transition.from_state.name,
                            "to_state": transition.to_state.name,
                            "roles": [r.name for r in transition.allowed_roles.all()],
                            "notify_roles": [
                                r.name for r in transition.notify_roles.all()
                            ],
                        },
                    }
                )

            elif action == "get_transition":
                transition_id = request.POST.get("transition_id")
                transition = get_object_or_404(
                    Transition, pk=transition_id, workflow_type=workflow_type
                )

                return JsonResponse(
                    {
                        "success": True,
                        "transition": {
                            "id": transition.pk,
                            "name": transition.name,
                            "from_state": transition.from_state.pk,
                            "to_state": transition.to_state.pk,
                            "roles": [r.pk for r in transition.allowed_roles.all()],
                            "notify_roles": [
                                r.pk for r in transition.notify_roles.all()
                            ],
                        },
                    }
                )

            elif action == "edit":
                transition_id = request.POST.get("transition_id")
                transition = get_object_or_404(
                    Transition, pk=transition_id, workflow_type=workflow_type
                )

                name = request.POST.get("name", "").strip()
                from_state_id = request.POST.get("from_state")
                to_state_id = request.POST.get("to_state")
                role_ids = request.POST.getlist("roles")

                if not name:
                    return JsonResponse(
                        {"success": False, "error": "Transition name is required"}
                    )

                if not from_state_id or not to_state_id:
                    return JsonResponse(
                        {
                            "success": False,
                            "error": "Both from and to states are required",
                        }
                    )

                if from_state_id == to_state_id:
                    return JsonResponse(
                        {
                            "success": False,
                            "error": "From and to states cannot be the same",
                        }
                    )

                from_state = get_object_or_404(
                    State, pk=from_state_id, workflow_type=workflow_type
                )
                to_state = get_object_or_404(
                    State, pk=to_state_id, workflow_type=workflow_type
                )

                transition.name = name
                transition.from_state = from_state
                transition.to_state = to_state
                transition.save()

                # Update allowed roles
                transition.allowed_roles.set(Role.objects.filter(id__in=role_ids))

                # Update notify roles
                notify_role_ids = request.POST.getlist("notify_roles")
                transition.notify_roles.set(Role.objects.filter(id__in=notify_role_ids))

                return JsonResponse(
                    {
                        "success": True,
                        "transition": {
                            "id": transition.pk,
                            "name": transition.name,
                            "from_state": transition.from_state.name,
                            "to_state": transition.to_state.name,
                            "roles": [r.name for r in transition.allowed_roles.all()],
                            "notify_roles": [
                                r.name for r in transition.notify_roles.all()
                            ],
                        },
                    }
                )

            elif action == "delete":
                transition_id = request.POST.get("transition_id")
                transition = get_object_or_404(
                    Transition, pk=transition_id, workflow_type=workflow_type
                )

                transition.delete()
                return JsonResponse({"success": True})

        except Exception as e:
            print(f"Error in workflow_type_transitions: {str(e)}")
            return JsonResponse({"success": False, "error": f"Error: {str(e)}"})

    # GET request
    transitions = Transition.objects.filter(workflow_type=workflow_type).order_by(
        "name"
    )
    states = State.objects.filter(workflow_type=workflow_type).order_by("order", "name")

    # Get available roles from group owner
    available_roles = Role.objects.filter(id__in=workflow_type.group_owner_roles())

    context = {
        "workflow_type": workflow_type,
        "transitions": transitions,
        "states": states,
        "available_roles": available_roles,
    }

    return context


@login_required
@htmx_partial("workflows/admin/workflow_type_referral_configs.html")
def workflow_type_referral_configs(request, pk):
    """Manage referral targets for a workflow type"""
    workflow_type = get_object_or_404(WorkflowType, pk=pk)

    if not request.user.is_staff:
        messages.error(
            request, "You do not have permission to manage referral configs."
        )
        return redirect("admin_workflow_types")

    if request.method == "POST":
        action = request.POST.get("action")

        try:
            if action == "create":
                group_id = request.POST.get("group_id")
                label = request.POST.get("label", "").strip()
                transition_role_ids = request.POST.getlist("transition_roles")
                edit_role_ids = request.POST.getlist("edit_roles")

                if not group_id:
                    return JsonResponse(
                        {"success": False, "error": "Target group is required."}
                    )

                target_group = get_object_or_404(Group, pk=group_id)

                if WorkflowTypeReferralConfig.objects.filter(
                    workflow_type=workflow_type, target_group=target_group
                ).exists():
                    return JsonResponse(
                        {
                            "success": False,
                            "error": f"A referral config for '{target_group.name}' already exists.",
                        }
                    )

                config = WorkflowTypeReferralConfig.objects.create(
                    workflow_type=workflow_type,
                    target_group=target_group,
                    label=label,
                )
                if transition_role_ids:
                    config.referred_transition_roles.set(
                        Role.objects.filter(id__in=transition_role_ids)
                    )
                if edit_role_ids:
                    config.referred_edit_roles.set(
                        Role.objects.filter(id__in=edit_role_ids)
                    )

                return JsonResponse(
                    {
                        "success": True,
                        "config": {
                            "id": config.pk,
                            "group_name": target_group.name,
                            "label": config.label,
                            "transition_roles": [
                                r.name for r in config.referred_transition_roles.all()
                            ],
                            "edit_roles": [
                                r.name for r in config.referred_edit_roles.all()
                            ],
                        },
                    }
                )

            elif action == "get":
                config_id = request.POST.get("config_id")
                config = get_object_or_404(
                    WorkflowTypeReferralConfig,
                    pk=config_id,
                    workflow_type=workflow_type,
                )
                return JsonResponse(
                    {
                        "success": True,
                        "config": {
                            "id": config.pk,
                            "group_id": config.target_group.pk,
                            "group_name": config.target_group.name,
                            "label": config.label,
                            "transition_role_ids": list(
                                config.referred_transition_roles.values_list(
                                    "id", flat=True
                                )
                            ),
                            "edit_role_ids": list(
                                config.referred_edit_roles.values_list("id", flat=True)
                            ),
                        },
                    }
                )

            elif action == "edit":
                config_id = request.POST.get("config_id")
                config = get_object_or_404(
                    WorkflowTypeReferralConfig,
                    pk=config_id,
                    workflow_type=workflow_type,
                )
                label = request.POST.get("label", "").strip()
                transition_role_ids = request.POST.getlist("transition_roles")
                edit_role_ids = request.POST.getlist("edit_roles")

                config.label = label
                config.save()
                config.referred_transition_roles.set(
                    Role.objects.filter(id__in=transition_role_ids)
                )
                config.referred_edit_roles.set(
                    Role.objects.filter(id__in=edit_role_ids)
                )

                return JsonResponse(
                    {
                        "success": True,
                        "config": {
                            "id": config.pk,
                            "group_name": config.target_group.name,
                            "label": config.label,
                            "transition_roles": [
                                r.name for r in config.referred_transition_roles.all()
                            ],
                            "edit_roles": [
                                r.name for r in config.referred_edit_roles.all()
                            ],
                        },
                    }
                )

            elif action == "delete":
                config_id = request.POST.get("config_id")
                config = get_object_or_404(
                    WorkflowTypeReferralConfig,
                    pk=config_id,
                    workflow_type=workflow_type,
                )
                config.delete()
                return JsonResponse({"success": True})

        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)})

    # GET
    configs = WorkflowTypeReferralConfig.objects.filter(
        workflow_type=workflow_type
    ).prefetch_related(
        "target_group", "referred_transition_roles", "referred_edit_roles"
    )
    all_groups = Group.objects.order_by("name")
    all_roles = Role.objects.order_by("name")

    context = {
        "workflow_type": workflow_type,
        "configs": configs,
        "all_groups": all_groups,
        "all_roles": all_roles,
    }
    return context


@login_required
@htmx_partial("workflows/admin/role_admin.html")
def role_admin(request):
    """Role administration - manage roles and their permissions"""
    if not request.user.is_staff and not request.user.is_superuser:
        messages.error(request, "You do not have permission to access this page.")
        return redirect("dashboard")

    roles = Role.objects.all()

    search = request.GET.get("search")
    if search:
        roles = roles.filter(
            Q(name__icontains=search) | Q(description__icontains=search)
        )

    context = {
        "roles": roles,
    }

    return context


@login_required
def workflow_report(request, pk):
    """Generate comprehensive workflow report."""
    workflow = get_object_or_404(Workflow, pk=pk)

    # Check if user can view this workflow
    if not workflow.can_user_view(request.user):
        messages.error(request, "You do not have permission to view this workflow.")
        return redirect("workflow_list")

    export_format = request.GET.get("format", "html")

    if export_format == "pdf":
        from .report_utils import export_workflow_to_pdf

        return export_workflow_to_pdf(workflow, request.user)
    else:
        from .report_utils import generate_workflow_report_html

        html_content = generate_workflow_report_html(workflow, request.user)
        return HttpResponse(html_content)


@login_required
@require_http_methods(["POST"])
def workflow_share_email(request, pk):
    """Share workflow via email."""
    workflow = get_object_or_404(Workflow, pk=pk)

    # Check if user can view this workflow
    if not workflow.can_user_view(request.user):
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    recipient_email = request.POST.get("email")
    custom_message = request.POST.get("message", "")

    if not recipient_email:
        return JsonResponse({"success": False, "error": "Email address is required"})

    try:
        from django.core import mail

        # Build the workflow URL
        workflow_url = request.build_absolute_uri(f"/workflows/{workflow.pk}/")
        report_url = request.build_absolute_uri(
            f"/workflows/{workflow.pk}/report/?format=html"
        )

        # Render email content
        # For HTML email
        # context = {
        #     "workflow": workflow,
        #     "workflow_url": workflow_url,
        #     "report_url": report_url,
        #     "sender": request.user,
        #     "custom_message": custom_message,
        # }

        # # Render HTML email from template
        # from django.template.loader import render_to_string
        # html_message = render_to_string('emails/workflow_share.html', context)

        # Plain text fallback (for email clients that don't support HTML)
        subject = f"Workflow Shared: {workflow.title}"
        plain_message = f"""
Hello,

{request.user.get_full_name() or request.user.username} has shared a workflow with you:

Workflow: {workflow.title}
Type: {workflow.workflow_type.name}
Status: {workflow.current_state.name}

{custom_message}

View Workflow: {workflow_url}
View Report: {report_url}

---
Parliament Workflow System
"""

        # Use connection reuse for better performance (Django 6.0 best practice)
        # Even for single-recipient emails, this is more efficient
        connection = mail.get_connection()
        try:
            connection.open()
            send_mail(
                subject=subject,
                message=plain_message,
                from_email=None,  # Uses DEFAULT_FROM_EMAIL from settings
                recipient_list=[recipient_email],
                # html_message=html_message,  # This is the key parameter for HTML emails
                fail_silently=False,
                connection=connection,
            )
        finally:
            connection.close()

        # Log the share action
        AuditLog.objects.create(
            object_id=workflow.pk,
            content_type=ContentType.objects.get_for_model(workflow),
            user=request.user,
            action="shared",
            changes={"shared_via_email": recipient_email},
        )

        return JsonResponse({"success": True})

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)})


# ============================================================================
# USER DELEGATION VIEWS
# ============================================================================


def user_has_delegation_permission(user):
    """Check if user has permission to manage delegations."""
    if user.is_superuser:
        return True

    # Check if user has any role with delegation permissions
    user_roles = user.memberships.filter(is_active=True).values_list("role", flat=True)

    # Check if any of the user's roles have delegation permissions
    # This is a placeholder - you might want to add a specific permission to Role model
    # For now, we'll check if user has any management permissions
    return (
        Role.objects.filter(id__in=user_roles, can_manage_permissions=True).exists()
        or user.is_staff
    )


@login_required
@htmx_partial("workflows/delegation_list.html")
def delegation_list(request):
    """List and search user delegations."""
    if not user_has_delegation_permission(request.user):
        messages.error(request, "You do not have permission to manage delegations.")
        return redirect("dashboard")

    from .forms import UserDelegationSearchForm

    form = UserDelegationSearchForm(request.GET)
    delegations = UserDelegation.objects.select_related(
        "delegator", "delegatee", "approved_by", "revoked_by"
    ).prefetch_related("workflows", "groups")

    # Apply filters
    if form.is_valid():
        cleaned_data = form.cleaned_data

        # Search by name
        if cleaned_data.get("search"):
            search_term = cleaned_data["search"]
            delegations = delegations.filter(
                Q(delegator__first_name__icontains=search_term)
                | Q(delegator__last_name__icontains=search_term)
                | Q(delegator__username__icontains=search_term)
                | Q(delegatee__first_name__icontains=search_term)
                | Q(delegatee__last_name__icontains=search_term)
                | Q(delegatee__username__icontains=search_term)
            )

        # Filter by status
        if cleaned_data.get("status"):
            delegations = delegations.filter(status=cleaned_data["status"])

        # Filter by delegator
        if cleaned_data.get("delegator"):
            delegations = delegations.filter(delegator=cleaned_data["delegator"])

        # Filter by delegatee
        if cleaned_data.get("delegatee"):
            delegations = delegations.filter(delegatee=cleaned_data["delegatee"])

    # Order by most recent first
    delegations = delegations.order_by("-created_at")

    context = {
        "delegations": delegations,
        "search_form": form,
    }
    return context  # For HTMX partials


@login_required
@htmx_partial("workflows/delegation_create.html")
def delegation_create(request):
    """Create a new user delegation."""
    if not user_has_delegation_permission(request.user):
        messages.error(request, "You do not have permission to create delegations.")
        return redirect("dashboard")

    from .forms import UserDelegationForm

    if request.method == "POST":
        form = UserDelegationForm(request.user, request.POST)
        if form.is_valid():
            delegation = form.save()
            messages.success(
                request,
                f"Delegation to {delegation.delegatee.get_full_name() or delegation.delegatee.username} has been created.",
            )
            return redirect("delegation_list")
    else:
        form = UserDelegationForm(request.user)

    context = {
        "form": form,
        "title": "Create Delegation",
    }

    return context


@login_required
@htmx_partial("workflows/delegation_detail.html")
def delegation_detail(request, pk):
    """View delegation details."""
    if not user_has_delegation_permission(request.user):
        messages.error(request, "You do not have permission to view delegations.")
        return redirect("dashboard")

    delegation = get_object_or_404(
        UserDelegation.objects.select_related(
            "delegator", "delegatee", "approved_by", "revoked_by"
        ).prefetch_related("workflows", "groups"),
        pk=pk,
    )

    context = {
        "delegation": delegation,
    }

    return context


@login_required
@htmx_partial("workflows/delegation_edit.html")
def delegation_edit(request, pk):
    """Edit an existing user delegation."""
    if not user_has_delegation_permission(request.user):
        messages.error(request, "You do not have permission to edit delegations.")
        return redirect("dashboard")

    delegation = get_object_or_404(UserDelegation, pk=pk)

    # Only allow editing if delegation is not revoked or expired
    if delegation.status in ["revoked", "expired"]:
        messages.error(request, "Cannot edit a revoked or expired delegation.")
        return redirect("delegation_detail", pk=pk)

    from .forms import UserDelegationForm

    if request.method == "POST":
        form = UserDelegationForm(request.user, request.POST, instance=delegation)
        if form.is_valid():
            form.save()
            messages.success(request, "Delegation has been updated.")
            return redirect("delegation_detail", pk=delegation.pk)
    else:
        form = UserDelegationForm(request.user, instance=delegation)

    context = {
        "form": form,
        "delegation": delegation,
        "title": "Edit Delegation",
    }

    return context


@login_required
def delegation_revoke(request, pk):
    """Revoke a user delegation."""
    if not user_has_delegation_permission(request.user):
        messages.error(request, "You do not have permission to revoke delegations.")
        return redirect("dashboard")

    delegation = get_object_or_404(UserDelegation, pk=pk)

    if delegation.status in ["revoked", "expired"]:
        messages.error(request, "This delegation cannot be revoked.")
        return redirect("delegation_detail", pk=pk)

    if request.method == "POST":
        reason = request.POST.get("reason", "")
        delegation.revoke(revoked_by=request.user, reason=reason)

        messages.success(
            request,
            f"Delegation to {delegation.delegatee.get_full_name() or delegation.delegatee.username} has been revoked.",
        )
        return redirect("delegation_detail", pk=pk)

    return redirect("delegation_detail", pk=pk)


@login_required
def delegation_approve(request, pk):
    """Approve a pending user delegation."""
    if not user_has_delegation_permission(request.user):
        messages.error(request, "You do not have permission to approve delegations.")
        return redirect("dashboard")

    if request.method != "POST":
        messages.error(request, "Invalid request method.")
        return redirect("delegation_list")

    delegation = get_object_or_404(UserDelegation, pk=pk)

    if delegation.status != "pending":
        messages.error(request, "Only pending delegations can be approved.")
        return redirect("delegation_detail", pk=pk)

    delegation.approve(request.user)

    messages.success(
        request,
        f"Delegation to {delegation.delegatee.get_full_name() or delegation.delegatee.username} has been approved.",
    )

    return redirect("delegation_detail", pk=pk)


@login_required
def user_search(request):
    """Search users and return HTML results for HTMX."""
    search_query = request.GET.get("search", "")

    users = User.objects.filter(is_active=True).exclude(id=request.user.id)

    if search_query:
        users = users.filter(
            Q(username__icontains=search_query)
            | Q(first_name__icontains=search_query)
            | Q(last_name__icontains=search_query)
        )

    users = users.order_by("first_name", "last_name")[:20]  # Limit to 20 results

    return render(request, "user_search_results.html", {"users": users})


@login_required
def group_search(request):
    """Search groups and return HTML results for HTMX."""
    search_query = request.GET.get("search", "")

    groups = Group.objects.all()

    if search_query:
        groups = groups.filter(Q(name__icontains=search_query))

    groups = groups.order_by("name")[:20]  # Limit to 20 results
    return render(
        request=request,
        template_name="group_list.html#group_container",
        context={"groups": groups},
    )
    # return render(request, "group_search_results.html", {"groups": groups})


@login_required
def delegate_user_search(request):
    """Search users for delegate selection and return HTML results for HTMX."""
    search_query = request.GET.get("search", "")

    # Show all active users - anyone can be a delegate
    users = User.objects.filter(is_active=True)

    if search_query:
        users = users.filter(
            Q(username__icontains=search_query)
            | Q(first_name__icontains=search_query)
            | Q(last_name__icontains=search_query)
        )

    users = users.order_by("first_name", "last_name")[:20]
    # Format users UUID as string
    # users = [{"pk": str(u.pk), "username": u.username} for u in users]

    return render(
        request,
        "workflows/partials/delegate_user_search_results.html",
        {"users": users, "search_query": search_query},
    )


@login_required
@require_http_methods(["POST"])
def delegate_add(request):
    """Add a delegate to the session and return updated delegate list."""
    user_id = request.POST.get("user_id")
    role = request.POST.get("role")

    if not user_id or not role:
        return HttpResponse(
            '<div class="alert alert-danger">User and role are required</div>',
            status=400,
        )

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return HttpResponse(
            '<div class="alert alert-danger">User not found</div>', status=404
        )

    # Get or initialize delegates list from session
    delegates = request.session.get("workflow_delegates", [])
    user_id_str = str(user_id)

    # Check if user already added
    if any(str(d.get("user_id")) == user_id_str for d in delegates):
        return HttpResponse(
            '<div class="alert alert-warning">This user has already been added</div>',
            status=400,
        )

    # Add new delegate
    delegates.append(
        {
            "user_id": user_id_str,
            "user_name": user.get_full_name() or user.username,
            "delegation_role": role,
            "is_mp": user.is_mp,
        }
    )

    request.session["workflow_delegates"] = delegates
    request.session.modified = True

    return render(
        request, "workflows/partials/delegate_list.html", {"delegates": delegates}
    )


@login_required
@require_http_methods(["DELETE"])
def delegate_remove(request, user_id):
    """Remove a delegate from the session and return updated delegate list."""
    delegates = request.session.get("workflow_delegates", [])
    user_id_str = str(user_id)
    delegates = [d for d in delegates if str(d.get("user_id")) != user_id_str]

    request.session["workflow_delegates"] = delegates
    request.session.modified = True

    return render(
        request, "workflows/partials/delegate_list.html", {"delegates": delegates}
    )


@login_required
def delegate_clear_session(request):
    """Clear delegates from session (called when form is loaded)."""
    if "workflow_delegates" in request.session:
        del request.session["workflow_delegates"]
    return HttpResponse(status=204)


@login_required
@require_http_methods(["POST"])
def delegate_populate_session(request):
    """Populate session with existing delegates for edit form."""
    try:
        data = json.loads(request.body)
        delegates = data.get("delegates", [])
        request.session["workflow_delegates"] = delegates
        request.session.modified = True
        return JsonResponse({"success": True})
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid data"}, status=400)


@login_required
def delegate_get_list(request):
    """Get current delegate list from session."""
    delegates = request.session.get("workflow_delegates", [])
    return render(
        request, "workflows/partials/delegate_list.html", {"delegates": delegates}
    )
