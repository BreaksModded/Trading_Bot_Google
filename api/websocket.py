"""
WebSocket manager for real-time dashboard updates.

Broadcasts bot status, price updates, trade events, and grid state
to connected dashboard clients.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

router = APIRouter()


class ConnectionManager:
    """Manages WebSocket connections for the dashboard."""

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket client connected ({len(self.active_connections)} total)")

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"WebSocket client disconnected ({len(self.active_connections)} total)")

    async def broadcast(self, message: dict) -> None:
        """Send a message to all connected clients."""
        if not self.active_connections:
            return

        data = json.dumps(message)
        disconnected = []

        for connection in self.active_connections:
            try:
                await connection.send_text(data)
            except Exception:
                disconnected.append(connection)

        for conn in disconnected:
            self.disconnect(conn)

    async def send_personal(self, websocket: WebSocket, message: dict) -> None:
        """Send a message to a specific client."""
        try:
            await websocket.send_text(json.dumps(message))
        except Exception:
            self.disconnect(websocket)

    @property
    def client_count(self) -> int:
        return len(self.active_connections)


# Global manager instance
manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time dashboard data.

    Handles bidirectional communication:
    - Server → Client: status updates, price data, trade events
    - Client → Server: commands (subscribe/unsubscribe to specific channels)

    Authentication: 
    - username via query param
    - password via initial JSON message `{"type": "auth", "password": "..."}`
    """
    settings = websocket.app.state.settings
    ws_user = websocket.query_params.get("username", "")
    
    if ws_user != settings.dashboard.username:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    
    # Wait for auth message within 5 seconds
    try:
        data = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
        message = json.loads(data)
        if message.get("type") != "auth" or message.get("password") != settings.dashboard.password:
            await websocket.close(code=1008)
            return
    except (asyncio.TimeoutError, json.JSONDecodeError, KeyError, Exception) as e:
        logger.warning(f"WebSocket auth failed or timeout: {e}")
        try:
            await websocket.close(code=1008)
        except Exception:
            pass
        return

    manager.active_connections.append(websocket)
    logger.info(f"WebSocket client connected ({len(manager.active_connections)} total)")

    try:
        while True:
            # Receive messages from client
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                msg_type = message.get("type", "")

                if msg_type == "ping":
                    await manager.send_personal(websocket, {"type": "pong"})
                elif msg_type == "subscribe":
                    channel = message.get("channel", "")
                    logger.debug(f"Client subscribed to: {channel}")
                    await manager.send_personal(
                        websocket,
                        {"type": "subscribed", "channel": channel},
                    )
            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)


async def broadcast_status(status_data: dict) -> None:
    """Broadcast bot status update to all dashboard clients."""
    await manager.broadcast({"type": "status", "data": status_data})


async def broadcast_price(price_data: dict) -> None:
    """Broadcast price update."""
    await manager.broadcast({"type": "price", "data": price_data})


async def broadcast_trade(trade_data: dict) -> None:
    """Broadcast trade execution event."""
    await manager.broadcast({"type": "trade", "data": trade_data})


async def broadcast_grid(grid_data: dict) -> None:
    """Broadcast grid state update."""
    await manager.broadcast({"type": "grid", "data": grid_data})
