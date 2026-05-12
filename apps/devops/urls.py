from django.urls import path

from . import agent, views

app_name = "devops"

urlpatterns = [
    # UI (на dev)
    path("", views.dashboard, name="dashboard"),
    path("run/<int:env_id>/<str:action_type>/", views.run_action, name="run_action"),
    path("actions/<uuid:action_id>/", views.action_detail, name="action_detail"),
    path("actions/<uuid:action_id>/poll/", views.action_poll, name="action_poll"),

    # Agent endpoints (на prod)
    path("agent/ping/", agent.agent_ping, name="agent_ping"),
    path("agent/jobs/", agent.agent_jobs_create, name="agent_jobs_create"),
    path("agent/jobs/<uuid:job_id>/", agent.agent_jobs_detail, name="agent_jobs_detail"),
]
