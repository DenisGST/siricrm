from django.urls import path

from . import agent, views

app_name = "devops"

urlpatterns = [
    # UI (на dev)
    path("", views.dashboard, name="dashboard"),

    # Agent endpoints (на prod)
    path("agent/ping/", agent.agent_ping, name="agent_ping"),
]
