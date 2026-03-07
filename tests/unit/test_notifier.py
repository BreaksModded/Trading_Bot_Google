import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update

from services.notifier import TelegramNotifier


@pytest.fixture
def db_mock():
    mock = MagicMock()
    mock.get_bot_state.return_value = {"status": "running", "updated_at": "now", "message": "all good"}
    mock.get_latest_config_snapshot.return_value = {"values_json": '{"some": "config"}'}
    return mock


@pytest.fixture
def mock_app_builder():
    with patch("telegram.ext.ApplicationBuilder") as builder_cls, \
         patch("telegram.ext.CommandHandler") as cmd_handler_cls, \
         patch("telegram.ext.ContextTypes"):
            
        app_mock = MagicMock()
        # Explicitly make async methods AsyncMock, while add_handler remains sync (MagicMock default)
        app_mock.initialize = AsyncMock()
        app_mock.start = AsyncMock()
        app_mock.stop = AsyncMock()
        app_mock.shutdown = AsyncMock()
        app_mock.updater = MagicMock()
        app_mock.updater.start_polling = AsyncMock()
        app_mock.updater.stop = AsyncMock()

        handlers = {}
        
        # When CommandHandler("status", status_cmd) is called, it returns a mock
        # We can mock its instantiation to capture the command and callback
        def mock_cmd_handler(command, callback, *args, **kwargs):
            handlers[command] = callback
            return MagicMock()
            
        cmd_handler_cls.side_effect = mock_cmd_handler
        
        builder_mock = MagicMock()
        builder_mock.token.return_value = builder_mock
        builder_mock.build.return_value = app_mock
        builder_cls.return_value = builder_mock
        
        yield app_mock, handlers


@pytest.mark.asyncio
async def test_telegram_rejects_unknown_chat_id(db_mock, mock_app_builder):
    app_mock, handlers = mock_app_builder
    notifier = TelegramNotifier(enabled=True, token="fake_token", chat_id="12345")
    
    with patch("asyncio.Event.wait", new_callable=AsyncMock) as wait_mock:
        wait_mock.side_effect = asyncio.CancelledError
        await notifier.run_command_bot(db=db_mock)
    
    for cmd, callback in handlers.items():
        update = MagicMock(spec=Update)
        update.effective_chat.id = "67890"  # Unauthorized
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        
        await callback(update, context)
        
        update.message.reply_text.assert_called_once_with("⛔ Unauthorized")


@pytest.mark.asyncio
async def test_telegram_accepts_authorized_chat_id(db_mock, mock_app_builder):
    app_mock, handlers = mock_app_builder
    notifier = TelegramNotifier(enabled=True, token="fake_token", chat_id="12345")
    
    with patch("asyncio.Event.wait", new_callable=AsyncMock) as wait_mock:
        wait_mock.side_effect = asyncio.CancelledError
        await notifier.run_command_bot(db=db_mock)
    
    update = MagicMock(spec=Update)
    update.effective_chat.id = "12345"  # Authorized
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    
    status_callback = handlers["status"]
    await status_callback(update, context)
    
    update.message.reply_text.assert_called_once()
    args, _ = update.message.reply_text.call_args
    assert "⛔ Unauthorized" not in args[0]
    
    stop_callback = handlers["stop"]
    update.message.reply_text.reset_mock()
    await stop_callback(update, context)
    db_mock.enqueue_command.assert_called_once_with("stop")
    update.message.reply_text.assert_called_once_with("Comando STOP encolado.")
