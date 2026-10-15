"""Optional loopback HTTP agent adapter.

The agent service, its permissions and its lifecycle are managed independently.
Install this adapter explicitly via adapterctl; core never starts this module alone.
"""
from __future__ import annotations

import json
import os
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from agent_adapters import AgentCapabilities

class HTTPAgentAdapter:
    adapter_id = "http-agent"

    def __init__(self, base_url: str, token: str = "") -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("adapter endpoint must be loopback HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("adapter endpoint must not contain credentials or query")
        self.base_url = base_url.rstrip("/")
        self.token = token

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(resume=True,messaging=True,events=True,cancel=True)

    def _request(self, method: str, path: str, payload: dict|None=None):
        raw = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept":"application/json"}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = Request(self.base_url + path,data=raw,headers=headers,method=method)
        with urlopen(request,timeout=15) as response:
            if response.status not in (200,201,202):
                raise RuntimeError("adapter returned unsupported HTTP status")
            raw_response=response.read(65537)
        if len(raw_response)>65536:
            raise ValueError("adapter response too large")
        result=json.loads(raw_response)
        if not isinstance(result,dict):
            raise ValueError("adapter response must be JSON object")
        return result

    def start(self, goal: str, *, project: str, device: str) -> str:
        data=self._request("POST","/sessions",{"goal":goal,"project":project,"device":device})
        session=data.get("session_id")
        if not isinstance(session,str) or not session or len(session)>256:
            raise ValueError("adapter returned invalid session")
        return session

    def status(self, session_id: str):
        return self._request("GET","/sessions/"+quote(session_id,safe=""))

    def send(self, session_id: str, message: str) -> None:
        self._request("POST","/sessions/"+quote(session_id,safe="")+"/messages",{"message":message})

    def resume(self, session_id: str):
        return self._request("POST","/sessions/"+quote(session_id,safe="")+"/resume",{})

    def cancel(self, session_id: str) -> None:
        self._request("POST","/sessions/"+quote(session_id,safe="")+"/cancel",{})

    def events(self, session_id: str, *, after: int=0):
        data=self._request("GET","/sessions/"+quote(session_id,safe="")+"/events?after="+str(after))
        return data.get("events",[])

def make():
    base_url=os.environ.get("LIVINGRUNTIME_HTTP_AGENT_URL","")
    if not base_url:
        raise RuntimeError("HTTP adapter not configured")
    return HTTPAgentAdapter(base_url,os.environ.get("LIVINGRUNTIME_HTTP_AGENT_TOKEN",""))
