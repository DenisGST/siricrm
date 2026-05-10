from django.urls import path

from . import agent, views

app_name = "devops"

urlpatterns = [
    # UI (на dev)
    path("", views.dashboard, name="dashboard"),

    # Agent endpoints (на prod)
    path("agent/ping/", agent.agent_ping, name="agent_ping"),
    path("agent/jobs/", agent.agent_jobs_create, name="agent_jobs_create"),
    path("agent/jobs/<uuid:job_id>/", agent.agent_jobs_detail, name="agent_jobs_detail"),
]
