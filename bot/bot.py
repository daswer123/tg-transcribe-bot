import os
import asyncio
import logging
import ffmpeg
from pathlib import Path
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters.command import Command
from dotenv import load_dotenv
from transfer_sh_client.manager import TransferManager
import fal_client
import openai
import tempfile
from collections import defaultdict
from typing import Dict, Set

# === ЗАГРУЗКА ОКРУЖЕНИЯ ===
load_dotenv()

# === ЛОГГИРОВАНИЕ ===
logging.basicConfig(level=logging.INFO)

# === ИНИЦИАЛИЗАЦИЯ БОТА ===
bot = Bot(token=os.getenv('BOT_TOKEN'))
dp = Dispatcher()

# === ТАБЛИЦА ЗАДАЧ ДЛЯ КАЖДОГО ЮЗЕРА ===
user_tasks: Dict[int, Set[asyncio.Task]] = defaultdict(set)

# === КЛИЕНТЫ ДЛЯ transfer.sh и OpenRouter ===
transfer_manager = TransferManager(
    os.getenv('TRANSFER_SH_HOST'),
    os.getenv('TRANSFER_SH_LOGIN'),
    os.getenv('TRANSFER_SH_PASSWORD')
)

openai_client = openai.OpenAI(
    base_url=os.getenv('OPENROUTER_BASE_URL'),
    api_key=os.getenv('OPENROUTER_API_KEY'),
)

# === КОНСТАНТЫ ===
MAX_MESSAGE_LENGTH = 4096
TOTAL_STEPS = 4

HELP_MESSAGE = """
🎙 Я помогаю преобразовывать голосовые и аудио сообщения в текст с умным форматированием.

Как использовать:
1. Отправьте мне голосовое или аудио сообщение
2. Я обработаю его и верну отформатированный текст

Поддерживаемые форматы:
- Голосовые сообщения
- Аудио файлы (mp3, wav, ogg, m4a)

Команды:
/start - Начать работу с ботом
/help - Показать это сообщение помощи
"""

# =======================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =======================================================================

async def cleanup_task(user_id: int, task: asyncio.Task):
    """Удалить задачу из списка активных после завершения (успешного или с ошибкой)."""
    try:
        await task
    except Exception as e:
        logging.error(f"Task error for user {user_id}: {str(e)}")
    finally:
        user_tasks[user_id].discard(task)
        if not user_tasks[user_id]:
            del user_tasks[user_id]


def chunk_text(text: str, max_size: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """
    Безопасное разбиение текста на части, чтобы не превышать лимит Telegram (4096 символов).
    Разбивает «жёстко» по max_size. Если хочется по предложениям/абзацам – можно добавить доп.логику.
    """
    chunks = []
    start = 0
    while start < len(text):
        end = start + max_size
        chunks.append(text[start:end])
        start = end
    return chunks


async def send_long_text_as_messages(
    message: types.Message,
    text: str,
    initial_text: str = None
):
    """
    Отправить длинный текст по частям. 
    - Сначала редактируем progress_message (message) на первую часть.
    - Если частей несколько, добавляем «Часть X из N» в начало каждого куска.
    """
    # Соединяем "initial_text + сам текст"
    if initial_text:
        combined_text = initial_text.strip() + "\n" + text
    else:
        combined_text = text

    chunks = chunk_text(combined_text, MAX_MESSAGE_LENGTH)
    total_chunks = len(chunks)

    for i, chunk in enumerate(chunks):
        # Если всего несколько частей, добавляем в начало "Часть X из N"
        if total_chunks > 1:
            # Пронумеруем отдельно, чтобы initial_text не "дублировалось"
            chunk_to_send = f"Часть {i+1} из {total_chunks}:\n{chunk}"
        else:
            chunk_to_send = chunk

        # Первую часть пытаемся сделать edit_text — чтобы обновить progress_message
        if i == 0:
            await message.edit_text(chunk_to_send)
        else:
            # Остальные части отправляем новыми сообщениями
            await message.answer(chunk_to_send)


async def download_telegram_file(file: types.File, destination: str):
    """Скачать файл из Telegram в локальный путь."""
    await bot.download_file(file.file_path, destination)


async def get_file_extension(file_path: str) -> str:
    """Получить расширение аудио-файла через ffmpeg.probe (асинхронно через to_thread)."""
    try:
        probe = await asyncio.to_thread(ffmpeg.probe, file_path)
        # format_name может быть списком через запятую, берём первый
        return probe['format']['format_name'].split(',')[0].lower()
    except ffmpeg.Error:
        return None


async def try_format_with_openai(text: str) -> str:
    """
    Пытаемся «пробить» форматирование текста через OpenRouter на двух моделях:
      1) google/gemini-2.0-flash-exp:free
      2) при ошибке -> google/gemini-flash-1.5
    Если обе попытки не сработали — пробрасываем исключение.
    """
    messages = [
        {
            "role": "user",
            "content": (
                "Пожалуйста, разбей следующий текст на логические параграфы "
                "для улучшения читаемости. Сохрани исходное содержание полностью, "
                "добавь только переносы строк. "
                "Не добавляй никаких дополнительных слов или пунктуации.\n\n"
                f"Текст: {text}"
            )
        }
    ]

    # Модель 1
    try:
        completion = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model="google/gemini-2.0-flash-exp:free",
            messages=messages,
            temperature=0.2,
            max_tokens=8000,
            stream=False
        )
        return completion.choices[0].message.content
    except Exception as e:
        # raise e
        logging.warning("Первая модель упала с ошибкой: %s", str(e))

    # Модель 2
    try:
        completion = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model="google/gemini-flash-1.5",
            messages=messages,
            temperature=0.2,
            max_tokens=8000,
            stream=False
        )
        return completion.choices[0].message.content
    except Exception as e:
        logging.error("Вторая модель тоже упала с ошибкой: %s", str(e))
        raise e


# =======================================================================
# ОСНОВНАЯ ЛОГИКА ОБРАБОТКИ АУДИО
# =======================================================================

async def process_audio_file(file_path: str, progress_message: types.Message):
    """
    Общий конвейер:
    1. Загрузка на transfer.sh
    2. Транскрибация через fal_client
    3. Форматирование (OpenRouter)
    4. Отправка результата
    """
    try:
        # Шаг 1: загрузка на transfer.sh
        await progress_message.edit_text(
            "🔄 Загрузка файла на сервер... (1/4)\n"
            "└ Подготовка файла для обработки"
        )
        download_link = await asyncio.to_thread(
            transfer_manager.upload,
            file_path=file_path,
            max_downloads=2
        )

        # Шаг 2: транскрибация через fal_client
        await progress_message.edit_text(
            "🎯 Транскрибация аудио... (2/4)\n"
            "└ Преобразование речи в текст"
        )
        # Если fal_client.subscribe — обычная (синхронная) функция, используем to_thread:
        result = await asyncio.to_thread(
            fal_client.subscribe,
            "fal-ai/whisper",
            arguments={
                "audio_url": download_link,
                "task": "transcribe",
                "language": "ru"
            }
        )

        if not result or not result.get('text'):
            raise Exception("Не удалось получить текст из аудио.")

        raw_text = result['text']

        # Шаг 3: улучшение форматирования (OpenRouter)
        await progress_message.edit_text(
            "📝 Форматирование текста... (3/4)\n"
            "└ Улучшение читаемости текста"
        )
        try:
            formatted_text = await try_format_with_openai(raw_text)
        except Exception as format_err:
            # Если обе модели упали
            raise Exception(
                "Ошибка при форматировании текста (оба варианта не сработали). "
                f"Детали: {str(format_err)}"
            )

        # Шаг 4: отправляем результат
        await progress_message.edit_text(
            "✨ Завершающий этап... (4/4)\n"
            "└ Подготовка результата"
        )

        await send_long_text_as_messages(
            progress_message,
            formatted_text,
            initial_text="✨ Готово! Вот ваш отформатированный текст:\n\n"
        )

    except Exception as e:
        logging.error(f"Error processing audio: {str(e)}")
        await progress_message.edit_text(
            f"❌ Произошла ошибка при обработке:\n└ {str(e)}"
        )


async def process_audio_message(message: types.Message, progress_message: types.Message):
    """Логика подготовки файла и вызова process_audio_file."""
    temp_file = None
    try:
        temp_file = tempfile.NamedTemporaryFile(delete=False)
        
        # Извлекаем правильный file из объекта message
        if message.voice:
            tg_file = message.voice
        elif message.audio:
            tg_file = message.audio
        elif message.document:
            tg_file = message.document
        else:
            raise ValueError("Неподдерживаемый тип вложения.")

        # Получаем file info
        file_info = await bot.get_file(tg_file.file_id)

        # Скачиваем
        await download_telegram_file(file_info, temp_file.name)

        # Проверяем формат (mp3, wav, ogg, m4a)
        extension = await get_file_extension(temp_file.name)
        if not extension or extension not in ['mp3', 'wav', 'ogg', 'm4a']:
            raise ValueError("Файл должен быть аудио форматом (mp3, wav, ogg, m4a).")

        # Запускаем конвейер обработки
        await process_audio_file(temp_file.name, progress_message)

    except Exception as e:
        logging.error(f"Error handling audio message: {str(e)}")
        await progress_message.edit_text(f"❌ Ошибка:\n└ {str(e)}")

    finally:
        # Удаляем временный файл
        if temp_file:
            try:
                os.unlink(temp_file.name)
            except:
                pass


# =======================================================================
# ХЕНДЛЕРЫ /start /help
# =======================================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот для транскрибации аудио в текст.\n\n"
        "Просто отправьте мне голосовое сообщение или аудио файл, "
        "и я преобразую его в отформатированный текст.\n\n"
        "Используйте /help для получения дополнительной информации."
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(HELP_MESSAGE)


# =======================================================================
# ХЕНДЛЕР ОБРАБОТКИ АУДИО (voice, audio, document)
# =======================================================================

@dp.message(F.voice | F.audio | F.document)
async def handle_audio(message: types.Message):
    user_id = message.from_user.id
    
    # Ограничим кол-во параллельных задач (не более 3)
    if len(user_tasks.get(user_id, set())) >= 3:
        await message.answer(
            "⚠️ У вас уже есть 3 активных задачи обработки.\n"
            "Пожалуйста, дождитесь их завершения."
        )
        return

    # Создаём сообщение-«reply» к исходному аудио
    progress_message = await message.reply("⏳ Обработка вашего аудио...")

    # Создаём асинхронную задачу
    task = asyncio.create_task(process_audio_message(message, progress_message))
    user_tasks[user_id].add(task)

    # Подключаем cleanup, чтобы после завершения убрать задачу из user_tasks
    asyncio.create_task(cleanup_task(user_id, task))


# =======================================================================
# ЗАПУСК БОТА
# =======================================================================

def main():
    # Запускаем бота
    asyncio.run(dp.start_polling(bot, skip_updates=True))

if __name__ == "__main__":
    main()
