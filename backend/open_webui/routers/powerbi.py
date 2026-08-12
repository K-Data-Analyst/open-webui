from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Optional

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL, AIOHTTP_CLIENT_TIMEOUT
from open_webui.models.config import Config
from open_webui.utils.auth import get_verified_user

router = APIRouter()

log = logging.getLogger(__name__)

POWERBI_API_BASE_URL = 'https://api.powerbi.com/v1.0/myorg'

PAGE_SIZE = 30

GUID_PATTERN = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


async def ensure_powerbi_enabled():
    if not await Config.get('powerbi.enable', False):
        raise HTTPException(status_code=404, detail='Power BI integration is not enabled')


async def get_powerbi_access_token(request: Request, user) -> Optional[str]:
    """
    Resolve a Power BI access token for this user.

    Browsing must run as the end user so Power BI itself enforces workspace
    visibility — never fall back to a shared or admin credential here.
    """
    auth_mode = await Config.get('powerbi.auth_mode', 'oauth_client') or 'oauth_client'

    if auth_mode == 'sso':
        # Reuse the Microsoft SSO login token. Only works if the admin added the
        # Power BI scope to MICROSOFT_OAUTH_SCOPE; /status probes to verify.
        from open_webui.utils.middleware import get_system_oauth_token

        oauth_token = await get_system_oauth_token(request, user)
    else:
        oauth_token = await request.app.state.oauth_client_manager.get_oauth_token(user.id, 'powerbi')

    return (oauth_token or {}).get('access_token') or None


async def powerbi_api_get(path: str, token: str, params: Optional[dict] = None) -> dict:
    async with aiohttp.ClientSession(
        trust_env=True,
        timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT),
    ) as session:
        async with session.get(
            f'{POWERBI_API_BASE_URL}{path}',
            headers={'Authorization': f'Bearer {token}'},
            params=params or {},
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        ) as resp:
            if resp.ok:
                return await resp.json()
            detail = await resp.text()
            log.debug(f'Power BI API request to {path} failed: {resp.status} - {detail}')
            raise HTTPException(
                status_code=resp.status,
                detail='Power BI API request failed',
            )


BUILD_PROBE_CONCURRENCY = 8

# The non-admin Power BI API exposes no "my permission on this dataset" field,
# but executeQueries is gated on exactly Build permission — probing it with a
# trivial query tells us whether the user can actually query the dataset.
BUILD_PROBE_BODY = {
    'queries': [{'query': 'EVALUATE ROW("probe", 1)'}],
    'serializerSettings': {'includeNulls': False},
}

CACHE_KEY_PREFIX = 'open-webui:powerbi'

# In-process fallback for deployments without a centralized cache (REDIS_URL).
_local_cache: dict = {}
LOCAL_CACHE_MAX_ENTRIES = 10000


async def get_permission_cache_ttl() -> int:
    try:
        return int(await Config.get('powerbi.permission_cache_ttl', 900) or 900)
    except Exception:
        return 900


async def permission_cache_get(request: Request, key: str):
    redis = getattr(request.app.state, 'redis', None)
    if redis is not None:
        try:
            value = await redis.get(key)
            if value is None:
                return None
            if isinstance(value, bytes):
                value = value.decode('utf-8')
            return json.loads(value)
        except Exception as e:
            log.debug(f'Power BI permission cache get failed for {key}: {e}')

    entry = _local_cache.get(key)
    if entry and entry[1] > time.time():
        return entry[0]
    return None


async def permission_cache_set(request: Request, key: str, value, ttl: int):
    redis = getattr(request.app.state, 'redis', None)
    if redis is not None:
        try:
            await redis.set(key, json.dumps(value), ex=ttl)
            return
        except Exception as e:
            log.debug(f'Power BI permission cache set failed for {key}: {e}')

    if len(_local_cache) >= LOCAL_CACHE_MAX_ENTRIES:
        now = time.time()
        for stale_key in [k for k, (_, expires_at) in _local_cache.items() if expires_at <= now]:
            _local_cache.pop(stale_key, None)
        if len(_local_cache) >= LOCAL_CACHE_MAX_ENTRIES:
            _local_cache.clear()
    _local_cache[key] = (value, time.time() + ttl)


async def filter_datasets_by_build_permission(
    request: Request, user_id: str, items: list[dict], token: str, force_refresh: bool = False
) -> list[dict]:
    ttl = await get_permission_cache_ttl()

    candidates = [item for item in items if item.get('id')]
    results: dict[str, bool] = {}
    to_probe: list[str] = []

    for item in candidates:
        dataset_id = str(item['id']).lower()
        cached = (
            None
            if force_refresh
            else await permission_cache_get(request, f'{CACHE_KEY_PREFIX}:build:{user_id}:{dataset_id}')
        )
        if cached is not None:
            results[dataset_id] = bool(cached)
        elif dataset_id not in to_probe:
            to_probe.append(dataset_id)

    if to_probe:
        semaphore = asyncio.Semaphore(BUILD_PROBE_CONCURRENCY)

        async with aiohttp.ClientSession(
            trust_env=True,
            timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT),
        ) as session:

            async def probe(dataset_id: str) -> tuple[bool, bool]:
                """Returns (has_build, definitive)."""
                async with semaphore:
                    try:
                        async with session.post(
                            f'{POWERBI_API_BASE_URL}/datasets/{dataset_id}/executeQueries',
                            headers={'Authorization': f'Bearer {token}'},
                            json=BUILD_PROBE_BODY,
                            ssl=AIOHTTP_CLIENT_SESSION_SSL,
                        ) as resp:
                            # 401/403 → no Build permission. 2xx/400 → Build (400
                            # covers push/live-connection datasets that cannot
                            # serve DAX). Anything else (throttling, 5xx) is
                            # indeterminate: keep the dataset visible but do not
                            # cache the answer.
                            if resp.status in (401, 403):
                                return False, True
                            if resp.ok or resp.status == 400:
                                return True, True
                            return True, False
                    except Exception as e:
                        log.debug(f'Power BI Build-permission probe failed for dataset {dataset_id}: {e}')
                        return True, False

            probe_results = await asyncio.gather(*(probe(dataset_id) for dataset_id in to_probe))

        for dataset_id, (has_build, definitive) in zip(to_probe, probe_results):
            results[dataset_id] = has_build
            if definitive:
                await permission_cache_set(
                    request,
                    f'{CACHE_KEY_PREFIX}:build:{user_id}:{dataset_id}',
                    has_build,
                    ttl,
                )

    return [item for item in candidates if results.get(str(item['id']).lower(), True)]


@router.get('/status')
async def get_status(request: Request, user=Depends(get_verified_user)):
    """
    Report whether Power BI auth is available for this user; drives the
    "Connect" state in the attachment panel.
    """
    config = await Config.get_many('powerbi.enable', 'powerbi.auth_mode', 'powerbi.mcp_server_id')
    if not config.get('powerbi.enable'):
        return {'enabled': False, 'connected': False}

    auth_mode = config.get('powerbi.auth_mode') or 'oauth_client'

    token = None
    try:
        token = await get_powerbi_access_token(request, user)
    except Exception as e:
        log.debug(f'Failed to resolve Power BI token for user {user.id}: {e}')

    connected = token is not None
    if connected and auth_mode == 'sso':
        # The SSO token only reaches Power BI if the login scopes include the
        # Power BI scope — verify with a cheap probe rather than assume.
        try:
            await powerbi_api_get('/groups', token, params={'$top': '1'})
        except HTTPException:
            connected = False

    return {
        'enabled': True,
        'connected': connected,
        'auth_mode': auth_mode,
        'mcp_server_id': config.get('powerbi.mcp_server_id') or None,
    }


@router.get('/workspaces')
async def get_workspaces(
    request: Request,
    query: Optional[str] = None,
    page: Optional[int] = 1,
    user=Depends(get_verified_user),
):
    await ensure_powerbi_enabled()

    token = await get_powerbi_access_token(request, user)
    if not token:
        raise HTTPException(status_code=401, detail='Power BI account is not connected')

    page = max(1, page or 1)
    params = {
        '$top': str(PAGE_SIZE),
        '$skip': str((page - 1) * PAGE_SIZE),
    }
    query = (query or '').strip()
    if query:
        escaped = query.replace("'", "''")
        params['$filter'] = f"contains(name,'{escaped}')"

    res = await powerbi_api_get('/groups', token, params=params)

    items = [
        {
            'id': group.get('id'),
            'name': group.get('name'),
        }
        for group in res.get('value', [])
    ]

    return {
        'items': items,
        'total': res.get('@odata.count'),
        'page': page,
        'limit': PAGE_SIZE,
    }


@router.get('/workspaces/{workspace_id}/datasets')
async def get_workspace_datasets(
    request: Request,
    workspace_id: str,
    query: Optional[str] = None,
    refresh: bool = False,
    user=Depends(get_verified_user),
):
    await ensure_powerbi_enabled()

    if not GUID_PATTERN.match(workspace_id):
        raise HTTPException(status_code=400, detail='Invalid workspace id')

    token = await get_powerbi_access_token(request, user)
    if not token:
        raise HTTPException(status_code=401, detail='Power BI account is not connected')

    res = await powerbi_api_get(f'/groups/{workspace_id}/datasets', token)

    items = [
        {
            'id': dataset.get('id'),
            'name': dataset.get('name'),
            'configured_by': dataset.get('configuredBy'),
            'is_refreshable': dataset.get('isRefreshable'),
        }
        for dataset in res.get('value', [])
    ]

    # The datasets endpoint has no server-side name filter — filter the
    # (workspace-scoped, small) list here to match the panel's search box.
    query = (query or '').strip().lower()
    if query:
        items = [item for item in items if query in (item.get('name') or '').lower()]

    if items and await Config.get('powerbi.require_build_permission', True):
        items = await filter_datasets_by_build_permission(request, user.id, items, token, force_refresh=refresh)

    return {
        'items': items,
        'total': len(items),
    }
