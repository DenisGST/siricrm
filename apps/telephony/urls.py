from django.urls import path

from . import agent, views, views_missed

app_name = "telephony"

urlpatterns = [
    # Раздел «Звонки»
    path("", views.panel, name="panel"),
    path("list/", views.call_list, name="call_list"),
    path("<uuid:call_id>/recording/", views.call_recording, name="call_recording"),
    path("client/<uuid:client_id>/calls/", views.client_calls, name="client_calls"),
    path("call/", views.place_call, name="place_call"),
    path("alerts/", views.call_alerts, name="call_alerts"),
    path("alerts/<uuid:alert_id>/dismiss/", views.alert_dismiss, name="alert_dismiss"),
    path("alerts/<uuid:alert_id>/comment/", views.alert_comment, name="alert_comment"),
    path("alerts/dismiss-all/", views.alerts_dismiss_all, name="alerts_dismiss_all"),

    # Реестр пропущенных (вкладка раздела «Звонки»)
    path("missed/list/", views_missed.missed_list, name="missed_list"),
    path("missed/<uuid:missed_id>/take/", views_missed.missed_take, name="missed_take"),
    path("missed/<uuid:missed_id>/close/", views_missed.missed_close, name="missed_close"),
    path("missed/<uuid:missed_id>/reopen/", views_missed.missed_reopen, name="missed_reopen"),
    path("missed/<uuid:missed_id>/comment/", views_missed.missed_comment, name="missed_comment"),
    path("missed/<uuid:missed_id>/voicemail/", views_missed.missed_voicemail,
         name="missed_voicemail"),

    # HTTP-приём с АТС (Bearer-токен PBX_AGENT_TOKEN)
    path("agent/ping/", agent.agent_ping, name="agent_ping"),
    path("agent/calls/", agent.agent_calls, name="agent_calls"),
    path("agent/recording/", agent.agent_recording, name="agent_recording"),
    path("agent/missed/", agent.agent_missed, name="agent_missed"),
    path("agent/voicemail/", agent.agent_voicemail, name="agent_voicemail"),
]
