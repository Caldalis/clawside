from __future__ import annotations

import asyncio
import json
import uuid
from base64 import b64encode
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional, TYPE_CHECKING

from src.channels.adapter import (
    ChannelAdapter,
    InboundEvent,
    InboundMessage,
    OutboundFile,
    get_action_handler,
)
from src.log import log

if TYPE_CHECKING:
    from src.config import Config


MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

def _detect_mention(text, entities, bot_username: str, bot_id) -> bool:
    if not text or not entities:
        return False
    for ent in entities:
        if ent.type == "mention":
            raw = text[ent.offset : ent.offset + ent.length].lower()
            if raw == f"@{bot_username}":
                return True
        elif ent.type == "text_mention" and ent.user and ent.user.id == bot_id:
            return True
    return False

async def _download_file(bot, file_id: str) -> Optional[bytes]:
    try:
        f = await bot.get_file(file_id)
        data = await f.download_as_bytearray()
    except Exception as e:
        log.warn("telegram_attachment_download_failed", file_id=file_id, err=str(e))
        return None
    return bytes(data)

async def _collect_attachments(msg, bot) -> tuple[list[dict], list[str]]:
    attachments: list[dict] = []
    errors: list[str] = []

    candidates: list[tuple[str, str, Optional[int], str]] = []
    # (name, type, file_size, file_id)

    doc = getattr(msg, "document", None)
    if doc is not None:
        name = getattr(doc, "file_name", None) or f"document-{doc.file_unique_id}"
        candidates.append(
            (name, "document", getattr(doc, "file_size", None), doc.file_id)
        )

    photos = getattr(msg, "photo", None)
    if photos:
        photo = photos[-1]
        name = f"photo-{photo.file_unique_id}.jpg"
        candidates.append(
            (name, "photo", getattr(photo, "file_size", None), photo.file_id)
        )

    for name, atype, size, file_id in candidates:
        if isinstance(size, int) and size > MAX_ATTACHMENT_BYTES:
            errors.append(
                f"{name}: exceeds {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB "
                "limit, not received"
            )
            continue
        data = await _download_file(bot, file_id)
        if data is None:
            errors.append(f"{name}: download failed")
            continue
        attachments.append(
            {
                "name": name,
                "type": atype,
                "data": b64encode(data).decode("ascii"),
            }
        )
    return attachments, errors



class TelegramAdapter(ChannelAdapter):

    channel_type = "telegram"
    supports_threads = True

    def __init__(self, cfg: "Config") -> None:

        from telegram import Update  # noqa: F401  （在 handler 中使用）
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            MessageHandler,
            filters,
        )

        if not cfg.telegram_bot_token:
            raise RuntimeError("TelegramAdapter requires TELEGRAM_BOT_TOKEN")

        self._cfg = cfg
        self._app = Application.builder().token(cfg.telegram_bot_token).build()


        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.Document.ALL | filters.PHOTO)
                & ~filters.COMMAND,
                self._on_message,
            )
        )
        self._app.add_handler(CallbackQueryHandler(self._on_callback))

        self._runner_task: Optional[asyncio.Task] = None


    async def start(self) -> None:

        if self._runner_task is not None and not self._runner_task.done():
            return
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(allowed_updates=None)
        self._runner_task = asyncio.create_task(self._runner_idle(), name="telegram-runner")
        log.info("telegram_adapter_started")

    async def _runner_idle(self) -> None:

        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    async def shutdown(self) -> None:
        if self._runner_task is not None and not self._runner_task.done():
            self._runner_task.cancel()
        self._runner_task = None
        try:
            if self._app.updater is not None and self._app.updater.running:
                await self._app.updater.stop()
            if self._app.running:
                await self._app.stop()
            await self._app.shutdown()
        except Exception as e:
            log.warn("telegram_shutdown_error", err=str(e))



    async def _on_message(self, update, context) -> None:  # type: ignore[no-untyped-def]
        msg = update.effective_message
        if msg is None:
            return
        chat = msg.chat
        user = msg.from_user
        if chat is None or user is None:
            return

        text = msg.text or msg.caption or ""
        has_media = bool(getattr(msg, "document", None) or getattr(msg, "photo", None))
        if not text and not has_media:
            return

        platform_id = str(chat.id)
        thread_id = (
            str(msg.message_thread_id)
            if getattr(msg, "is_topic_message", False) and msg.message_thread_id is not None
            else None
        )


        handle = user.username or str(user.id)
        sender_id = f"telegram:{handle}"
        sender_name = user.full_name or user.username or str(user.id)

        bot_username = (context.bot.username or "").lower()
        is_mention = _detect_mention(
            msg.text, msg.entities, bot_username, context.bot.id
        ) or _detect_mention(
            msg.caption,
            getattr(msg, "caption_entities", None),
            bot_username,
            context.bot.id,
        )

        is_group = chat.type in ("group", "supergroup", "channel")

        attachments: list[dict] = []
        if has_media:
            attachments, errors = await _collect_attachments(msg, context.bot)
            for err in errors:
                try:
                    await msg.reply_text(f"⚠️ {err}")
                except Exception as e:
                    log.warn("telegram_attachment_notice_failed", err=str(e))
            if not attachments and not text:
                return

        content = {
            "text": text,
            "sender": sender_name,
            "sender_id": sender_id,
        }
        if attachments:
            content["attachments"] = attachments
        message = InboundMessage(
            id=f"tg-{chat.id}-{msg.message_id}",
            kind="chat",
            content=json.dumps(content),
            timestamp=datetime.now(timezone.utc).isoformat(),
            is_mention=is_mention,
            is_group=is_group,
        )
        event = InboundEvent(
            channel_type=self.channel_type,
            platform_id=platform_id,
            thread_id=thread_id,
            message=message,
        )

        from src.router import route_inbound
        try:
            await route_inbound(event)
        except Exception as e:
            log.error("telegram_route_inbound_failed", err=str(e))

    async def _on_callback(self, update, context) -> None:  # type: ignore[no-untyped-def]
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()

        if not query.data.startswith("q:"):
            return
        rest = query.data[2:]
        sep = rest.find(":")
        if sep == -1:
            return
        question_id = rest[:sep]
        choice = rest[sep + 1 :]

        user = query.from_user
        handle = (user.username or str(user.id)) if user else None
        user_id = f"telegram:{handle}" if handle else None

        handler = get_action_handler()
        if handler is None:
            log.warn("telegram_action_handler_missing", question_id=question_id)
            return
        try:
            await handler(question_id, choice, user_id)
        except Exception as e:
            log.error("telegram_action_handler_failed", question_id=question_id, err=str(e))

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


    async def deliver(
        self,
        *,
        platform_id: str,
        thread_id: Optional[str],
        kind: str,
        content: dict,
        files: Optional[list[OutboundFile]] = None,
    ) -> Optional[str]:
        try:
            chat_id = int(platform_id)
        except (TypeError, ValueError):
            log.warn("telegram_deliver_bad_chat_id", platform_id=platform_id)
            return None

        thread_kwargs: dict = {}
        if thread_id is not None:
            try:
                thread_kwargs["message_thread_id"] = int(thread_id)
            except ValueError:
                # 并非所有 thread_id 都是整数（其他渠道），但 telegram 总是 静默拒绝并发到 topic 之外
                pass

        # ask_user_question —— inline keyboard 按钮
        if kind == "chat-sdk" and content.get("type") == "ask_question":
            return await self._deliver_question(chat_id, thread_kwargs, content)

        text = (content.get("text") if isinstance(content, dict) else None) or ""
        sent_id: Optional[int] = None

        if files:
            # 把每个文件以 document 形式发送 首个附带 caption
            for i, f in enumerate(files):
                buf = BytesIO(f.data)
                buf.name = f.filename
                msg = await self._app.bot.send_document(
                    chat_id=chat_id,
                    document=buf,
                    filename=f.filename,
                    caption=text if i == 0 and text else None,
                    **thread_kwargs,
                )
                sent_id = msg.message_id
            return str(sent_id) if sent_id is not None else None

        if not text:
            return None
        msg = await self._app.bot.send_message(
            chat_id=chat_id,
            text=text,
            **thread_kwargs,
        )
        return str(msg.message_id)

    async def _deliver_question(
        self,
        chat_id: int,
        thread_kwargs: dict,
        content: dict,
    ) -> Optional[str]:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        question_id = content.get("question_id") or content.get("questionId") or uuid.uuid4().hex
        title = content.get("title") or ""
        question = content.get("question") or ""
        options = content.get("options") or []
        if not isinstance(options, list) or not options:
            text = f"*{title}*\n{question}" if title else question
            msg = await self._app.bot.send_message(chat_id=chat_id, text=text, **thread_kwargs)
            return str(msg.message_id)

        keyboard = []
        for i, opt in enumerate(options):
            label = opt.get("label") if isinstance(opt, dict) else str(opt)
            data = f"q:{question_id}:{i}"
            keyboard.append([InlineKeyboardButton(text=str(label), callback_data=data)])

        body_parts = []
        if title:
            body_parts.append(f"*{title}*")
        if question:
            body_parts.append(question)
        text = "\n".join(body_parts) if body_parts else "Please pick:"

        msg = await self._app.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            **thread_kwargs,
        )
        return str(msg.message_id)

    async def set_typing(self, platform_id: str, thread_id: Optional[str]) -> None:
        try:
            chat_id = int(platform_id)
        except (TypeError, ValueError):
            return
        kwargs: dict = {}
        if thread_id is not None:
            try:
                kwargs["message_thread_id"] = int(thread_id)
            except ValueError:
                pass
        try:
            await self._app.bot.send_chat_action(chat_id=chat_id, action="typing", **kwargs)
        except Exception:
            # typing 是尽力而为
            pass
