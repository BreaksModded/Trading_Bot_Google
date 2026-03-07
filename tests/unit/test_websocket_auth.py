import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import WebSocket
from api.websocket import websocket_endpoint

@pytest.mark.asyncio
async def test_websocket_auth_from_message_not_query_string():
    # Setup mock websocket
    websocket = AsyncMock(spec=WebSocket)
    websocket.app = MagicMock()
    websocket.app.state.settings.dashboard.username = "admin"
    websocket.app.state.settings.dashboard.password = "demo1234"
    websocket.query_params.get.return_value = "admin"
    
    # Mock receive_text to return the auth message first, then hang (or throw to exit loop)
    # the second call will be in the while True loop
    async def mock_receive():
        if mock_receive.calls == 0:
            mock_receive.calls += 1
            return json.dumps({"type": "auth", "password": "demo1234"})
        else:
            raise Exception("Exit loop")
    
    mock_receive.calls = 0
    websocket.receive_text.side_effect = mock_receive
    
    # Mock manager to isolate state? Actually we can just run it
    
    # Run
    await websocket_endpoint(websocket)
    
    # Verify accept was called
    websocket.accept.assert_called_once()
    # Verify not closed due to auth
    websocket.close.assert_not_called()

@pytest.mark.asyncio
async def test_websocket_auth_timeout():
    websocket = AsyncMock(spec=WebSocket)
    websocket.app = MagicMock()
    websocket.app.state.settings.dashboard.username = "admin"
    websocket.app.state.settings.dashboard.password = "demo1234"
    websocket.query_params.get.return_value = "admin"
    
    # Mock receive_text to sleep longer than timeout
    async def mock_receive():
        await asyncio.sleep(6)
        return ""
        
    websocket.receive_text.side_effect = mock_receive
    
    await websocket_endpoint(websocket)
    
    # Verify closed with 1008
    websocket.close.assert_called_with(code=1008)
