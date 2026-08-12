from __future__ import annotations

import asyncio
import base64
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
    request: Request, user_id: str, items: list[dict], token: str
) -> list[dict]:
    ttl = await get_permission_cache_ttl()

    candidates = [item for item in items if item.get('id')]
    results: dict[str, bool] = {}
    to_probe: list[str] = []

    for item in candidates:
        dataset_id = str(item['id']).lower()
        cached = await permission_cache_get(request, f'{CACHE_KEY_PREFIX}:build:{user_id}:{dataset_id}')
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


FABRIC_API_BASE_URL = 'https://api.fabric.microsoft.com/v1'
FABRIC_TOKEN_SCOPE = 'https://api.fabric.microsoft.com/.default'

# App-only token for the Power BI AAD app (client credentials flow). Kept
# in-process deliberately — re-acquiring it is cheap and the secret-derived
# token should not sit in a shared cache.
_sp_token_cache: dict = {}
_sp_token_lock = asyncio.Lock()


async def get_powerbi_service_principal_token() -> Optional[str]:
    config = await Config.get_many('powerbi.client_id', 'powerbi.client_secret', 'powerbi.tenant_id')
    client_id = config.get('powerbi.client_id')
    client_secret = config.get('powerbi.client_secret')
    tenant_id = config.get('powerbi.tenant_id')
    if not (client_id and client_secret and tenant_id):
        return None

    cache_key = (tenant_id, client_id)
    async with _sp_token_lock:
        cached = _sp_token_cache.get(cache_key)
        if cached and cached['expires_at'] > time.time() + 60:
            return cached['token']

        try:
            async with aiohttp.ClientSession(
                trust_env=True,
                timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT),
            ) as session:
                async with session.post(
                    f'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token',
                    data={
                        'grant_type': 'client_credentials',
                        'client_id': client_id,
                        'client_secret': client_secret,
                        'scope': FABRIC_TOKEN_SCOPE,
                    },
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                ) as resp:
                    if not resp.ok:
                        log.warning(f'Power BI service principal token request failed: {resp.status}')
                        return None
                    token_response = await resp.json()
        except Exception as e:
            log.warning(f'Power BI service principal token request failed: {e}')
            return None

        access_token = token_response.get('access_token')
        if not access_token:
            return None

        _sp_token_cache[cache_key] = {
            'token': access_token,
            'expires_at': time.time() + int(token_response.get('expires_in', 3600)),
        }
        return access_token


def extract_user_object_id(access_token: str) -> Optional[str]:
    """Read the AAD object id (oid claim) from the user's access token.

    The OIDC 'sub' claim is app-pairwise and must not be used as a directory
    user id; oid is the stable directory object id the admin API expects.
    """
    try:
        payload_b64 = access_token.split('.')[1]
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get('oid')
    except Exception:
        return None


def _entity_has_build_permission(entity: dict) -> bool:
    details = entity.get('itemAccessDetails') or {}
    permissions = [str(p).lower() for p in (details.get('permissions') or entity.get('permissions') or [])]
    # For semantic models Build surfaces as Explore; Write (workspace
    # admin/member/contributor) implies it.
    return any('explore' in p or 'build' in p or 'write' in p for p in permissions)


async def get_build_dataset_ids_from_admin_api(request: Request, user_id: str, user_token: str) -> Optional[set]:
    """
    Return the set of semantic-model ids the user holds Build permission on,
    via the Fabric admin access-entities API, or None when the lookup is
    unavailable (missing credentials, tenant setting off, API error) so the
    caller can fall back to probing. The admin API is limited to ~200
    requests/hour tenant-wide, so results are cached per user.
    """
    cache_key = f'{CACHE_KEY_PREFIX}:build_ids:{user_id}'
    cached = await permission_cache_get(request, cache_key)
    if cached is not None:
        return {str(dataset_id).lower() for dataset_id in cached}

    user_object_id = extract_user_object_id(user_token)
    if not user_object_id:
        return None

    sp_token = await get_powerbi_service_principal_token()
    if not sp_token:
        return None

    ids = set()
    url = f'{FABRIC_API_BASE_URL}/admin/users/{user_object_id}/access'
    params = {'type': 'SemanticModel'}

    try:
        async with aiohttp.ClientSession(
            trust_env=True,
            timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT),
        ) as session:
            while url:
                async with session.get(
                    url,
                    headers={'Authorization': f'Bearer {sp_token}'},
                    params=params,
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                ) as resp:
                    if not resp.ok:
                        log.warning(
                            f'Fabric admin access-entities lookup failed: {resp.status} - {await resp.text()}'
                        )
                        return None
                    body = await resp.json()

                for entity in body.get('accessEntities') or body.get('value') or []:
                    if entity.get('id') and _entity_has_build_permission(entity):
                        ids.add(str(entity['id']).lower())

                url = body.get('continuationUri')
                params = None
    except Exception as e:
        log.warning(f'Fabric admin access-entities lookup failed: {e}')
        return None

    await permission_cache_set(request, cache_key, sorted(ids), await get_permission_cache_ttl())
    return ids


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
        build_ids = None
        if (await Config.get('powerbi.permission_source', 'probe') or 'probe') == 'admin_api':
            build_ids = await get_build_dataset_ids_from_admin_api(request, user.id, token)
            if build_ids is None:
                log.warning(
                    'Fabric admin permission lookup unavailable — falling back to the executeQueries probe'
                )

        if build_ids is not None:
            items = [item for item in items if str(item.get('id') or '').lower() in build_ids]
        else:
            items = await filter_datasets_by_build_permission(request, user.id, items, token)

    return {
        'items': items,
        'total': len(items),
    }
