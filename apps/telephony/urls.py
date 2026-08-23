from django.urls import path

from . import agent, views

app_name = "telephony"

urlpatterns = [
    # Раздел «Звонки»
    path("", views.panel, name="panel"),
    path("list/", views.call_list, name="call_list"),
    path("<uuid:call_id>/recording/", views.call_recording, name="call_recording"),
    path("client/<uuid:client_id>/calls/", views.client_calls, name="client_calls"),

    # HTTP-приём с АТС (Bearer-токен PBX_AGENT_TOKEN)
    path("agent/ping/", agent.agent_ping, name="agent_ping"),
    path("agent/calls/", agent.agent_calls, name="agent_calls"),
    path("agent/recording/", agent.agent_recording, name="agent_recording"),
]
