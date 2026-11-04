"""Home Assistant REST transport with exact-target service calls."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import os
import ipaddress
import re
from urllib.parse import urlsplit, quote
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.error import HTTPError, URLError


class HassError(RuntimeError): pass

class _RefuseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        raise HassError('HA API redirect refused; never forward bearer credentials')



def check_entity(value: str, domain: str) -> str:
    if not isinstance(value,str) or not re.fullmatch(re.escape(domain)+r'\.[a-z0-9_]+', value):
        raise ValueError(f'Expected valid {domain} entity id')
    return value


class HomeAssistant:
    def __init__(self, base_url: str, token: str, timeout: float = 5):
        parts = urlsplit(base_url)
        if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('','/'):
            raise ValueError('Home Assistant URL must be a simple http(s) host:port, without credentials, paths or query')
        try:
            ip=ipaddress.ip_address(parts.hostname)
            local_ip=ip.is_private or ip.is_loopback
        except ValueError:
            local_ip=False
        if parts.scheme == 'http' and not (parts.hostname=='localhost' or parts.hostname.endswith('.local') or local_ip):
            raise ValueError('Plain HTTP only allowed for a local/private HA host; use HTTPS for remote access')
        if not token or len(token)<8:raise ValueError('Missing HA access token')
        self.base=base_url.rstrip('/')
        self.token=token
        self.timeout=timeout
        # Never send a local owner token through ambient HTTP(S)_PROXY settings.
        self.opener=build_opener(ProxyHandler({}),_RefuseRedirect())

    def request(self, method: str, path: str, payload: dict | None = None, *, limit=256000):
        if not path.startswith('/api/') or '://' in path or '?' in path:
            raise ValueError('Unexpected HA API path')
        data = json.dumps(payload).encode() if payload is not None else None
        req=Request(self.base+path, data=data, method=method,headers={
            'Authorization':'Bearer '+self.token,'Content-Type':'application/json','Accept':'application/json'})
        try:
            with self.opener.open(req,timeout=self.timeout) as resp:
                if resp.status not in (200,201):raise HassError('HA returned non-success')
                data=resp.read(limit+1)
                if len(data)>limit:raise HassError('HA response exceeded limit')
                return json.loads(data) if data else None
        except (HTTPError,URLError,TimeoutError,ValueError) as e:
            raise HassError(f'HA request failed ({type(e).__name__})') from None

    def states(self) -> list:
        result=self.request('GET','/api/states',limit=3_000_000)
        if not isinstance(result,list) or len(result)>5000:raise HassError('Unexpected state-list response')
        return result

    def configuration(self) -> dict:
        obj=self.request('GET','/api/config')
        if not isinstance(obj,dict):raise HassError('Unexpected Home Assistant config response')
        return obj

    def state(self, entity: str) -> dict:
        if not re.fullmatch(r'[a-z_]+\.[a-z0-9_]+',entity):raise ValueError('Invalid HA entity')
        result=self.request('GET','/api/states/'+quote(entity,safe='._'))
        if not isinstance(result,dict) or result.get('entity_id')!=entity:
            raise HassError('HA entity readback mismatch')
        return result

    def switch(self, entity: str, on: bool) -> None:
        check_entity(entity,'switch')
        self.request('POST','/api/services/switch/'+('turn_on' if on else 'turn_off'), {'entity_id':entity})
        # A successful POST is not a physical charge guarantee.
        returned=self.state(entity).get('state')
        if returned!=('on' if on else 'off'):
            raise HassError('HA command/readback mismatch')
