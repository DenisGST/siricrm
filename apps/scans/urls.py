from django.urls import path

from . import agent, views

app_name = "scans"

urlpatterns = [
    # UI лотка (для секретаря)
    path("", views.inbox, name="inbox"),
    path("list/", views.scan_list, name="list"),
    path("pending-count/", views.pending_count, name="pending_count"),
    path("upload/", views.manual_upload, name="manual_upload"),
    path("<uuid:scan_id>/assign/modal/", views.assign_modal, name="assign_modal"),
    path("<uuid:scan_id>/assign/", views.assign, name="assign"),
    path("<uuid:scan_id>/discard/", views.discard, name="discard"),
    path("client-search/", views.client_search, name="client_search"),
    path("client-targets/", views.client_targets, name="client_targets"),
    path("counterparty-search/", views.counterparty_search, name="counterparty_search"),
    path("batch/modal/", views.batch_modal, name="batch_modal"),
    path("batch/assign/", views.batch_assign, name="batch_assign"),

    # HTTP-приём от локального агента (Bearer-токен SCAN_AGENT_TOKEN)
    path("agent/ping/", agent.agent_ping, name="agent_ping"),
    path("agent/intake/", agent.agent_intake, name="agent_intake"),
]
