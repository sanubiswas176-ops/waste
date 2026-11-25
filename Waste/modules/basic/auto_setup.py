"""Intentionally disabled: account connection must not auto-join chats or start bots.
Users choose and join their own channels/groups and start bots manually.
"""

async def run_after_connect(client):
    return None
