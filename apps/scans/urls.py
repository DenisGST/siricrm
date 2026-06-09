from django.urls import path

from . import agent, views

app_name = "scans"

urlpatterns = [
    # UI лотка (для секретаря)
    path("", views.inbox, name="inbox"),
    path("list/", views.scan_list, name="list"),
    path("upload/", views.manual_upload, name="manual_upload"),
    path("<uuid:scan_id>/assign/modal/", views.assign_modal, name="assign_modal"),
    path("<uuid:scan_id>/assign/", views.assign, name="assign"),
    path("<uuid:scan_id>/discard/", views.discard, name="discard"),
    path("client-search/", views.client_search, name="client_search"),
    path("client-targets/", views.client_targets, name="client_targets"),

    # HTTP-приём от локального агента (Bearer-токен SCAN_AGENT_TOKEN)
    path("agent/ping/", agent.agent_ping, name="agent_ping"),
    path("agent/intake/", agent.agent_intake, name="agent_intake"),
]
