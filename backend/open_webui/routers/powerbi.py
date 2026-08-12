from __future__ import annotations

import asyncio
import logging
import re
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


async def filter_datasets_by_build_permission(items: list[dict], token: str) -> list[dict]:
    semaphore = asyncio.Semaphore(BUILD_PROBE_CONCURRENCY)

    async with aiohttp.ClientSession(
        trust_env=True,
        timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT),
    ) as session:

        async def has_build_permission(dataset_id: str) -> bool:
            async with semaphore:
                try:
                    async with session.post(
                        f'{POWERBI_API_BASE_URL}/datasets/{dataset_id}/executeQueries',
                        headers={'Authorization': f'Bearer {token}'},
                        json=BUILD_PROBE_BODY,
                        ssl=AIOHTTP_CLIENT_SESSION_SSL,
                    ) as resp:
                        # Only 401/403 indicate missing Build permission. Other
                        # failures (push or live-connection datasets returning
                        # 400, throttling) say nothing about permissions — keep
                        # the dataset visible rather than hide it spuriously.
                        return resp.status not in (401, 403)
                except Exception as e:
                    log.debug(f'Power BI Build-permission probe failed for dataset {dataset_id}: {e}')
                    return True

        probed = [item for item in items if item.get('id')]
        results = await asyncio.gather(*(has_build_permission(item['id']) for item in probed))

    return [item for item, has_build in zip(probed, results) if has_build]


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
        items = await filter_datasets_by_build_permission(items, token)

    return {
        'items': items,
        'total': len(items),
    }
