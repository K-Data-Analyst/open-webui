import logging

from open_webui.models.config import Config

log = logging.getLogger(__name__)


async def get_powerbi_mcp_oauth_token(request, user, server_id: str) -> dict | None:
    """
    Fallback OAuth token for the configured Power BI MCP server.

    The dataset picker authenticates users against the dedicated ``powerbi``
    OAuth client, while an ``oauth_2.1`` MCP connection looks its token up
    under ``mcp:<server_id>`` — a separate grant the user never made. When the
    connection is *the* Power BI MCP server (``powerbi.mcp_server_id``) and the
    integration runs in ``oauth_client`` mode, reuse the ``powerbi`` grant so a
    single "Connect Power BI" covers both browsing and tool calls.

    Returns None when the fallback does not apply or no token is available.
    """
    if not server_id:
        return None

    try:
        config = await Config.get_many('powerbi.enable', 'powerbi.auth_mode', 'powerbi.mcp_server_id')
        if not config.get('powerbi.enable'):
            return None
        if (config.get('powerbi.mcp_server_id') or '') != server_id:
            return None
        if (config.get('powerbi.auth_mode') or 'oauth_client') != 'oauth_client':
            return None

        return await request.app.state.oauth_client_manager.get_oauth_token(user.id, 'powerbi')
    except Exception as e:
        log.error(f'Error resolving Power BI OAuth token for MCP server {server_id}: {e}')
        return None
