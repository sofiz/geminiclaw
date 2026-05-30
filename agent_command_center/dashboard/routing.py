from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'^ws/agent/(?P<agent_id>[^/]+)/console/$', consumers.AgentConsoleConsumer.as_asgi()),
    re_path(r'^ws/agent/(?P<agent_id>[^/]+)/thoughts/$', consumers.AgentThoughtsConsumer.as_asgi()),
    re_path(r'^ws/agent/global/$', consumers.AgentGlobalConsumer.as_asgi()),
    re_path(r'^ws/agent/test/$', consumers.AgentTestConsumer.as_asgi()),
]
