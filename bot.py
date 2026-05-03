import asyncio
import html
import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("tutor_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден. Добавь его в .env файл")
BOT_TOKEN = BOT_TOKEN.strip()
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "tutor_bot.sqlite3"))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
DEFAULT_REMINDER_MINUTES = int(os.getenv("DEFAULT_REMINDER_MINUTES", "60"))
PROXY_URL = os.getenv("PROXY_URL", "").strip()

ADD_STUDENT_BUTTON = "Новый ученик"
ADD_LESSON_BUTTON = "Новое занятие"
QUICK_ADD_BUTTON = "Быстрый ввод"
EDIT_SCHEDULE_BUTTON = "Изменить расписание"
OLD_ADD_STUDENT_BUTTON = "Добавить ученика"
OLD_ADD_LESSON_BUTTON = "Добавить занятие"
TODAY_BUTTON = "Сегодня"
WEEK_BUTTON = "Неделя"
LESSONS_BUTTON = "Ближайшие"
CANCEL_BUTTON = "Отмена"
SKIP_BUTTON = "Пропустить"

WEEKDAY_NAMES = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]

WEEKDAY_ALIASES = {
    "пн": 0,
    "понедельник": 0,
    "вт": 1,
    "вторник": 1,
    "ср": 2,
    "среда": 2,
    "чт": 3,
    "четверг": 3,
    "пт": 4,
    "пятница": 4,
    "сб": 5,
    "суббота": 5,
    "вс": 6,
    "воскресенье": 6,
}

QUICK_PAIR_PATTERN = re.compile(
    r"\b(?P<day>пн|понедельник|вт|вторник|ср|среда|чт|четверг|пт|пятница|сб|суббота|вс|воскресенье)\.?\s+"
    r"(?P<time>[0-2]?\d:[0-5]\d)"
    r"(?:\s+(?P<duration>\d{2,3}))?\b",
    re.IGNORECASE,
)


class AddStudentFlow(StatesGroup):
    name = State()
    subject = State()
    notes = State()


class AddLessonFlow(StatesGroup):
    student_id = State()
    starts_at = State()
    duration = State()
    recurrence = State()
    note = State()


class QuickAddFlow(StatesGroup):
    text = State()


class EditScheduleFlow(StatesGroup):
    student_id = State()
    lesson_id = State()
    new_start = State()
    new_duration = State()
    new_note = State()


@dataclass
class Lesson:
    id: int
    student_name: str
    subject: str
    starts_at: datetime
    duration_minutes: int
    recurrence: str
    note: str


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(connect()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                starts_at TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL,
                recurrence TEXT NOT NULL CHECK (recurrence IN ('once', 'weekly')),
                note TEXT NOT NULL DEFAULT '',
                reminder_minutes INTEGER NOT NULL DEFAULT 60,
                reminded_for_start TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()


def now_local() -> datetime:
    return datetime.now(TIMEZONE).replace(second=0, microsecond=0)


def parse_local_datetime(value: str) -> datetime:
    parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    return parsed.replace(tzinfo=TIMEZONE)


def dt_to_db(value: datetime) -> str:
    return value.astimezone(TIMEZONE).isoformat(timespec="minutes")


def dt_from_db(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(TIMEZONE)


def set_setting(key: str, value: str) -> None:
    with closing(connect()) as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()


def get_setting(key: str) -> Optional[str]:
    with closing(connect()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def add_student(name: str, subject: str, notes: str) -> int:
    with closing(connect()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO students(name, subject, notes, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (name.strip(), subject.strip(), notes.strip(), dt_to_db(now_local())),
        )
        conn.commit()
        return int(cursor.lastrowid)


def list_students() -> list[sqlite3.Row]:
    with closing(connect()) as conn:
        return list(conn.execute("SELECT * FROM students ORDER BY name COLLATE NOCASE"))


def student_exists(student_id: int) -> bool:
    with closing(connect()) as conn:
        row = conn.execute("SELECT id FROM students WHERE id = ?", (student_id,)).fetchone()
        return row is not None


def weekly_lesson_exists(student_id: int, starts_at: datetime) -> bool:
    with closing(connect()) as conn:
        row = conn.execute(
            """
            SELECT id
            FROM lessons
            WHERE student_id = ?
              AND starts_at = ?
              AND recurrence = 'weekly'
            """,
            (student_id, dt_to_db(starts_at)),
        ).fetchone()
        return row is not None


def add_lesson(
    student_id: int,
    starts_at: datetime,
    duration_minutes: int,
    recurrence: str,
    note: str,
) -> int:
    with closing(connect()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO lessons(
                student_id, starts_at, duration_minutes, recurrence,
                note, reminder_minutes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                student_id,
                dt_to_db(starts_at),
                duration_minutes,
                recurrence,
                note.strip(),
                DEFAULT_REMINDER_MINUTES,
                dt_to_db(now_local()),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def delete_lesson(lesson_id: int) -> bool:
    with closing(connect()) as conn:
        cursor = conn.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))
        conn.commit()
        return cursor.rowcount > 0


def lessons_between(start: datetime, end: datetime) -> list[Lesson]:
    with closing(connect()) as conn:
        rows = conn.execute(
            """
            SELECT l.*, s.name AS student_name, s.subject
            FROM lessons l
            JOIN students s ON s.id = l.student_id
            WHERE l.starts_at >= ? AND l.starts_at < ?
            ORDER BY l.starts_at
            """,
            (dt_to_db(start), dt_to_db(end)),
        ).fetchall()
        return [row_to_lesson(row) for row in rows]


def upcoming_lessons(limit: int = 20) -> list[Lesson]:
    with closing(connect()) as conn:
        rows = conn.execute(
            """
            SELECT l.*, s.name AS student_name, s.subject
            FROM lessons l
            JOIN students s ON s.id = l.student_id
            WHERE l.starts_at >= ?
            ORDER BY l.starts_at
            LIMIT ?
            """,
            (dt_to_db(now_local() - timedelta(minutes=1)), limit),
        ).fetchall()
        return [row_to_lesson(row) for row in rows]


def lessons_for_student(student_id: int) -> list[Lesson]:
    with closing(connect()) as conn:
        rows = conn.execute(
            """
            SELECT l.*, s.name AS student_name, s.subject
            FROM lessons l
            JOIN students s ON s.id = l.student_id
            WHERE l.student_id = ?
              AND l.starts_at >= ?
            ORDER BY l.starts_at
            """,
            (student_id, dt_to_db(now_local() - timedelta(minutes=1))),
        ).fetchall()
        return [row_to_lesson(row) for row in rows]


def get_lesson(lesson_id: int) -> Optional[Lesson]:
    with closing(connect()) as conn:
        row = conn.execute(
            """
            SELECT l.*, s.name AS student_name, s.subject
            FROM lessons l
            JOIN students s ON s.id = l.student_id
            WHERE l.id = ?
            """,
            (lesson_id,),
        ).fetchone()
        return row_to_lesson(row) if row else None


def update_lesson_start(lesson_id: int, starts_at: datetime) -> bool:
    with closing(connect()) as conn:
        cursor = conn.execute(
            """
            UPDATE lessons
            SET starts_at = ?, reminded_for_start = NULL
            WHERE id = ?
            """,
            (dt_to_db(starts_at), lesson_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def update_lesson_duration(lesson_id: int, duration_minutes: int) -> bool:
    with closing(connect()) as conn:
        cursor = conn.execute(
            "UPDATE lessons SET duration_minutes = ? WHERE id = ?",
            (duration_minutes, lesson_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def update_lesson_recurrence(lesson_id: int, recurrence: str) -> bool:
    with closing(connect()) as conn:
        cursor = conn.execute(
            """
            UPDATE lessons
            SET recurrence = ?, reminded_for_start = NULL
            WHERE id = ?
            """,
            (recurrence, lesson_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def update_lesson_note(lesson_id: int, note: str) -> bool:
    with closing(connect()) as conn:
        cursor = conn.execute(
            "UPDATE lessons SET note = ? WHERE id = ?",
            (note.strip(), lesson_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def due_reminders() -> list[Lesson]:
    current = now_local()
    with closing(connect()) as conn:
        rows = conn.execute(
            """
            SELECT l.*, s.name AS student_name, s.subject
            FROM lessons l
            JOIN students s ON s.id = l.student_id
            WHERE l.starts_at > ?
              AND (l.reminded_for_start IS NULL OR l.reminded_for_start != l.starts_at)
            ORDER BY l.starts_at
            """,
            (dt_to_db(current),),
        ).fetchall()
        lessons = [row_to_lesson(row) for row in rows]
        return [
            lesson
            for lesson in lessons
            if lesson.starts_at <= current + timedelta(minutes=DEFAULT_REMINDER_MINUTES)
        ]


def mark_reminded(lesson: Lesson) -> None:
    with closing(connect()) as conn:
        conn.execute(
            "UPDATE lessons SET reminded_for_start = ? WHERE id = ?",
            (dt_to_db(lesson.starts_at), lesson.id),
        )
        conn.commit()


def advance_finished_weekly_lessons() -> None:
    current = now_local()
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id, starts_at FROM lessons WHERE recurrence = 'weekly' AND starts_at <= ?",
            (dt_to_db(current),),
        ).fetchall()
        for row in rows:
            next_start = dt_from_db(row["starts_at"])
            while next_start <= current:
                next_start += timedelta(days=7)
            conn.execute(
                """
                UPDATE lessons
                SET starts_at = ?, reminded_for_start = NULL
                WHERE id = ?
                """,
                (dt_to_db(next_start), row["id"]),
            )
        conn.commit()


def row_to_lesson(row: sqlite3.Row) -> Lesson:
    return Lesson(
        id=row["id"],
        student_name=row["student_name"],
        subject=row["subject"],
        starts_at=dt_from_db(row["starts_at"]),
        duration_minutes=row["duration_minutes"],
        recurrence=row["recurrence"],
        note=row["note"],
    )


def split_args(text: str, expected_min: int) -> list[str]:
    command_parts = text.split(maxsplit=1)
    if len(command_parts) < 2:
        raise ValueError
    parts = [part.strip() for part in command_parts[1].split("|")]
    if len(parts) < expected_min or any(part == "" for part in parts[:expected_min]):
        raise ValueError
    return parts


def h(value: object) -> str:
    return html.escape(str(value), quote=False)


def find_student_in_text(text: str) -> tuple[sqlite3.Row, str]:
    normalized = text.strip()
    lowered = normalized.lower()
    students = sorted(list_students(), key=lambda row: len(row["name"]), reverse=True)
    for student in students:
        name = student["name"].strip()
        name_lower = name.lower()
        if lowered == name_lower:
            return student, ""
        if lowered.startswith(name_lower + " "):
            return student, normalized[len(name):].strip()
    raise ValueError("Не нашел ученика. Проверь имя или добавь ученика через кнопку <b>Новый ученик</b>.")


def next_weekday_datetime(weekday: int, time_text: str) -> datetime:
    hour_raw, minute_raw = time_text.split(":", 1)
    hour = int(hour_raw)
    minute = int(minute_raw)
    if hour > 23:
        raise ValueError("Время должно быть в формате <code>20:30</code>.")

    current = now_local()
    days_ahead = (weekday - current.weekday()) % 7
    candidate = (current + timedelta(days=days_ahead)).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    if candidate <= current:
        candidate += timedelta(days=7)
    return candidate


def parse_quick_lessons(text: str) -> tuple[sqlite3.Row, list[tuple[datetime, int]]]:
    student, rest = find_student_in_text(text)
    if not rest:
        raise ValueError(
            "После имени укажи день и время.\n\n"
            "Пример: <code>Захар ср 20:30</code>"
        )

    lessons = []
    for match in QUICK_PAIR_PATTERN.finditer(rest):
        day_text = match.group("day").lower().rstrip(".")
        starts_at = next_weekday_datetime(WEEKDAY_ALIASES[day_text], match.group("time"))
        duration = int(match.group("duration") or 60)
        lessons.append((starts_at, duration))

    if not lessons:
        raise ValueError(
            "Не понял день и время.\n\n"
            "Примеры:\n"
            "<code>Захар ср 20:30</code>\n"
            "<code>Никита ср 19:30, чт 19:00</code>\n"
            "<code>София среда 17:00 90</code>"
        )
    return student, lessons


def quick_add_weekly_lessons(text: str) -> tuple[str, list[Lesson]]:
    student, lesson_specs = parse_quick_lessons(text)
    added_lessons = []
    skipped = 0
    for starts_at, duration in lesson_specs:
        if weekly_lesson_exists(student["id"], starts_at):
            skipped += 1
            continue
        lesson_id = add_lesson(student["id"], starts_at, duration, "weekly", "")
        lesson = get_lesson(lesson_id)
        if lesson:
            added_lessons.append(lesson)

    if not added_lessons and skipped:
        return "Такие еженедельные занятия уже есть.", []
    return "", added_lessons


def format_lesson(lesson: Lesson) -> str:
    starts = lesson.starts_at.strftime("%d.%m.%Y %H:%M")
    date_part, time_part = starts.split(" ", 1)
    recurrence = "еженедельно" if lesson.recurrence == "weekly" else "разово"
    subject = f"\nПредмет: {h(lesson.subject)}" if lesson.subject else ""
    note = f"\nЗаметка: {h(lesson.note)}" if lesson.note else ""
    return (
        f"<b>{time_part}</b>  {h(lesson.student_name)}\n"
        f"Дата: {date_part}\n"
        f"Длительность: {lesson.duration_minutes} мин\n"
        f"Тип: {recurrence}"
        f"{subject}{note}\n"
        f"<code>#{lesson.id}</code>"
    )


def format_lesson_row(lesson: Lesson) -> str:
    time_part = lesson.starts_at.strftime("%H:%M")
    recurrence = "еженед." if lesson.recurrence == "weekly" else "разово"
    subject = lesson.subject if lesson.subject else "предмет не указан"
    note = f" ({lesson.note})" if lesson.note else ""
    return f"{time_part}  {lesson.student_name} - {subject}, {lesson.duration_minutes} мин, {recurrence}{note}"


def format_lessons(lessons: Iterable[Lesson], empty_text: str, title: str = "Расписание") -> str:
    items = list(lessons)
    if not items:
        return f"<b>{h(title)}</b>\n\n{h(empty_text)}"

    blocks = [f"<b>{h(title)}</b>"]
    current_date = None
    current_rows: list[str] = []

    for lesson in items:
        lesson_date = lesson.starts_at.date()
        if current_date is not None and lesson_date != current_date:
            blocks.append(format_day_block(current_date, current_rows))
            current_rows = []
        current_date = lesson_date
        current_rows.append(format_lesson_row(lesson))

    if current_date is not None:
        blocks.append(format_day_block(current_date, current_rows))

    return "\n\n".join(blocks)


def format_day_block(day, rows: list[str]) -> str:
    weekday = WEEKDAY_NAMES[day.weekday()]
    date_part = day.strftime("%d.%m")
    width = max((len(row) for row in rows), default=0)
    border = "-" * min(max(width, 24), 46)
    body = h("\n".join(rows))
    return f"<b>{weekday}, {date_part}</b>\n<pre>{border}\n{body}\n{border}</pre>"


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ADD_LESSON_BUTTON), KeyboardButton(text=ADD_STUDENT_BUTTON)],
            [KeyboardButton(text=QUICK_ADD_BUTTON), KeyboardButton(text=EDIT_SCHEDULE_BUTTON)],
            [KeyboardButton(text=TODAY_BUTTON), KeyboardButton(text=WEEK_BUTTON)],
            [KeyboardButton(text=LESSONS_BUTTON)],
        ],
        resize_keyboard=True,
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CANCEL_BUTTON)]],
        resize_keyboard=True,
    )


def skip_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=SKIP_BUTTON)], [KeyboardButton(text=CANCEL_BUTTON)]],
        resize_keyboard=True,
    )


def duration_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="45"),
                KeyboardButton(text="60"),
                KeyboardButton(text="90"),
                KeyboardButton(text="120"),
            ],
            [KeyboardButton(text=CANCEL_BUTTON)],
        ],
        resize_keyboard=True,
    )


def students_keyboard(students: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{student['name']} - {student['subject']}",
                    callback_data=f"student:{student['id']}",
                )
            ]
            for student in students
        ]
    )


def edit_students_keyboard(students: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{student['name']} - {student['subject']}",
                    callback_data=f"edit_student:{student['id']}",
                )
            ]
            for student in students
        ]
    )


def lesson_picker_keyboard(lessons: list[Lesson]) -> InlineKeyboardMarkup:
    buttons = []
    for lesson in lessons:
        day = lesson.starts_at.strftime("%d.%m")
        time_part = lesson.starts_at.strftime("%H:%M")
        recurrence = "еженед." if lesson.recurrence == "weekly" else "разово"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{day} {time_part} - {recurrence}",
                    callback_data=f"edit_lesson:{lesson.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def edit_lesson_keyboard(lesson_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Дата/время", callback_data=f"edit_start:{lesson_id}"),
                InlineKeyboardButton(text="Длительность", callback_data=f"edit_duration:{lesson_id}"),
            ],
            [
                InlineKeyboardButton(text="Повтор", callback_data=f"edit_recurrence_menu:{lesson_id}"),
                InlineKeyboardButton(text="Заметка", callback_data=f"edit_note:{lesson_id}"),
            ],
            [
                InlineKeyboardButton(text="Удалить", callback_data=f"edit_delete:{lesson_id}"),
                InlineKeyboardButton(text="К ученикам", callback_data="edit_back_students"),
            ],
        ]
    )


def edit_recurrence_keyboard(lesson_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Разовое", callback_data=f"edit_recurrence:once:{lesson_id}"),
                InlineKeyboardButton(text="Каждую неделю", callback_data=f"edit_recurrence:weekly:{lesson_id}"),
            ],
            [InlineKeyboardButton(text="Назад", callback_data=f"edit_lesson:{lesson_id}")],
        ]
    )


def confirm_delete_keyboard(lesson_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"confirm_delete:{lesson_id}"),
                InlineKeyboardButton(text="Назад", callback_data=f"edit_lesson:{lesson_id}"),
            ]
        ]
    )


def recurrence_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Разовое", callback_data="recurrence:once"),
                InlineKeyboardButton(text="Каждую неделю", callback_data="recurrence:weekly"),
            ]
        ]
    )


HELP_TEXT = """
<b>Команды</b>

<code>/start</code> - открыть меню
<code>/cancel</code> - отменить текущее добавление
<code>/new_lesson</code> - добавить занятие через кнопки
<code>/q Захар ср 20:30</code> - быстрый ввод
<code>/edit_schedule</code> - изменить расписание ученика
<code>/students</code> - список учеников
<code>/today</code> - занятия сегодня
<code>/week</code> - ближайшие 7 дней
<code>/lessons</code> - ближайшие занятия

<b>Быстрый ввод</b>
<code>Захар ср 20:30</code>
<code>Никита ср 19:30, чт 19:00</code>
<code>София среда 17:00 90</code>

По умолчанию быстрый ввод добавляет еженедельное занятие на 60 минут.

<b>Подробный ввод</b>
<code>/add_student Имя | Предмет | Заметки</code>
<code>/add_lesson ID | 2026-04-29 18:30 | 60 | once | заметка</code>

<code>once</code> - разовое занятие
<code>weekly</code> - каждую неделю
""".strip()


dp = Dispatcher()


@dp.message(Command("start"))
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    set_setting("teacher_chat_id", str(message.chat.id))
    await message.answer(
        "<b>Расписание репетитора</b>\n\n"
        "Я запомнил этот чат для напоминаний. Выбирай действие в меню ниже.",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(HELP_TEXT)


@dp.message(Command("cancel"))
@dp.message(F.text == CANCEL_BUTTON)
async def cancel_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, отменил текущее действие.", reply_markup=main_keyboard())


async def ask_quick_add(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(QuickAddFlow.text)
    await message.answer(
        "<b>Быстрый ввод</b>\n\n"
        "Напиши одной строкой:\n"
        "<code>Захар ср 20:30</code>\n"
        "<code>Никита ср 19:30, чт 19:00</code>\n"
        "<code>София среда 17:00 90</code>\n\n"
        "Если длительность не указана, поставлю 60 минут. Занятия будут еженедельными.",
        reply_markup=cancel_keyboard(),
    )


async def handle_quick_add_text(message: Message, state: FSMContext, text: str) -> None:
    try:
        info_message, added_lessons = quick_add_weekly_lessons(text)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    await state.clear()
    if info_message:
        await message.answer(info_message, reply_markup=main_keyboard())
        return

    await message.answer(
        format_lessons(added_lessons, "Ничего не добавлено.", "Добавлено"),
        reply_markup=main_keyboard(),
    )


@dp.message(Command("quick"))
@dp.message(Command("q"))
async def quick_add_command(message: Message, state: FSMContext) -> None:
    command_parts = (message.text or "").split(maxsplit=1)
    if len(command_parts) == 1:
        await ask_quick_add(message, state)
        return
    await handle_quick_add_text(message, state, command_parts[1])


@dp.message(F.text == QUICK_ADD_BUTTON)
async def quick_add_button(message: Message, state: FSMContext) -> None:
    await ask_quick_add(message, state)


@dp.message(QuickAddFlow.text)
async def quick_add_flow_text(message: Message, state: FSMContext) -> None:
    await handle_quick_add_text(message, state, message.text or "")


async def show_edit_students(message: Message, state: FSMContext) -> None:
    students = list_students()
    if not students:
        await message.answer(
            "<b>Изменить расписание</b>\n\nПока нет учеников.",
            reply_markup=main_keyboard(),
        )
        return

    await state.clear()
    await state.set_state(EditScheduleFlow.student_id)
    await message.answer(
        "<b>Изменить расписание</b>\n\nВыбери ученика:",
        reply_markup=edit_students_keyboard(students),
    )


async def show_student_lessons(target, state: FSMContext, student_id: int) -> None:
    lessons = lessons_for_student(student_id)
    await state.update_data(student_id=student_id)
    await state.set_state(EditScheduleFlow.lesson_id)

    if not lessons:
        text = (
            "<b>Изменить расписание</b>\n\n"
            "У этого ученика пока нет будущих занятий."
        )
        if isinstance(target, CallbackQuery):
            await target.answer()
            if target.message:
                await target.message.answer(text, reply_markup=main_keyboard())
        else:
            await target.answer(text, reply_markup=main_keyboard())
        return

    student_name = lessons[0].student_name
    text = f"<b>{h(student_name)}</b>\n\nВыбери занятие, которое нужно изменить:"
    if isinstance(target, CallbackQuery):
        await target.answer()
        if target.message:
            await target.message.answer(text, reply_markup=lesson_picker_keyboard(lessons))
    else:
        await target.answer(text, reply_markup=lesson_picker_keyboard(lessons))


async def show_lesson_editor(target, state: FSMContext, lesson_id: int) -> None:
    lesson = get_lesson(lesson_id)
    if not lesson:
        text = "Не нашел это занятие. Возможно, оно уже удалено."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await target.answer(text, reply_markup=main_keyboard())
        return

    await state.update_data(lesson_id=lesson_id)
    await state.set_state(EditScheduleFlow.lesson_id)
    text = "<b>Что изменить?</b>\n\n" + format_lesson(lesson)
    if isinstance(target, CallbackQuery):
        await target.answer()
        if target.message:
            await target.message.answer(text, reply_markup=edit_lesson_keyboard(lesson_id))
    else:
        await target.answer(text, reply_markup=edit_lesson_keyboard(lesson_id))


@dp.message(Command("edit_schedule"))
@dp.message(F.text == EDIT_SCHEDULE_BUTTON)
async def edit_schedule_command(message: Message, state: FSMContext) -> None:
    await show_edit_students(message, state)


@dp.callback_query(EditScheduleFlow.student_id, F.data.startswith("edit_student:"))
async def choose_edit_student(callback: CallbackQuery, state: FSMContext) -> None:
    student_id = int((callback.data or "").split(":", 1)[1])
    if not student_exists(student_id):
        await callback.answer("Ученика уже нет в базе.", show_alert=True)
        return
    await show_student_lessons(callback, state, student_id)


@dp.callback_query(F.data == "edit_back_students")
async def back_to_edit_students(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message:
        await callback.answer()
        await show_edit_students(callback.message, state)


@dp.callback_query(F.data.startswith("edit_lesson:"))
async def choose_lesson_to_edit(callback: CallbackQuery, state: FSMContext) -> None:
    lesson_id = int((callback.data or "").split(":", 1)[1])
    await show_lesson_editor(callback, state, lesson_id)


@dp.callback_query(F.data.startswith("edit_start:"))
async def request_new_start(callback: CallbackQuery, state: FSMContext) -> None:
    lesson_id = int((callback.data or "").split(":", 1)[1])
    await state.update_data(lesson_id=lesson_id)
    await state.set_state(EditScheduleFlow.new_start)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "<b>Новая дата и время</b>\n\n"
            "Введи в формате <code>YYYY-MM-DD HH:MM</code>\n"
            "Например: <code>2026-05-06 20:30</code>",
            reply_markup=cancel_keyboard(),
        )


@dp.message(EditScheduleFlow.new_start)
async def save_new_start(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        starts_at = parse_local_datetime(message.text or "")
    except ValueError:
        await message.answer(
            "Не понял дату.\n\nПример правильного формата: <code>2026-05-06 20:30</code>"
        )
        return

    lesson_id = int(data["lesson_id"])
    update_lesson_start(lesson_id, starts_at)
    await message.answer("<b>Дата и время обновлены</b>", reply_markup=main_keyboard())
    await show_lesson_editor(message, state, lesson_id)


@dp.callback_query(F.data.startswith("edit_duration:"))
async def request_new_duration(callback: CallbackQuery, state: FSMContext) -> None:
    lesson_id = int((callback.data or "").split(":", 1)[1])
    await state.update_data(lesson_id=lesson_id)
    await state.set_state(EditScheduleFlow.new_duration)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "<b>Новая длительность</b>\n\nВыбери вариант или введи число минут:",
            reply_markup=duration_keyboard(),
        )


@dp.message(EditScheduleFlow.new_duration)
async def save_new_duration(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Длительность должна быть числом минут, например <code>60</code>.")
        return

    data = await state.get_data()
    lesson_id = int(data["lesson_id"])
    update_lesson_duration(lesson_id, int(text))
    await message.answer("<b>Длительность обновлена</b>", reply_markup=main_keyboard())
    await show_lesson_editor(message, state, lesson_id)


@dp.callback_query(F.data.startswith("edit_recurrence_menu:"))
async def show_edit_recurrence(callback: CallbackQuery, state: FSMContext) -> None:
    lesson_id = int((callback.data or "").split(":", 1)[1])
    await state.update_data(lesson_id=lesson_id)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "<b>Тип занятия</b>\n\nВыбери новый тип:",
            reply_markup=edit_recurrence_keyboard(lesson_id),
        )


@dp.callback_query(F.data.startswith("edit_recurrence:"))
async def save_new_recurrence(callback: CallbackQuery, state: FSMContext) -> None:
    _, recurrence, lesson_id_raw = (callback.data or "").split(":", 2)
    if recurrence not in {"once", "weekly"}:
        await callback.answer("Неизвестный тип занятия.", show_alert=True)
        return

    lesson_id = int(lesson_id_raw)
    update_lesson_recurrence(lesson_id, recurrence)
    await callback.answer("Тип обновлен")
    await show_lesson_editor(callback, state, lesson_id)


@dp.callback_query(F.data.startswith("edit_note:"))
async def request_new_note(callback: CallbackQuery, state: FSMContext) -> None:
    lesson_id = int((callback.data or "").split(":", 1)[1])
    await state.update_data(lesson_id=lesson_id)
    await state.set_state(EditScheduleFlow.new_note)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "<b>Новая заметка</b>\n\n"
            "Введи текст заметки или нажми <b>Пропустить</b>, чтобы очистить ее.",
            reply_markup=skip_keyboard(),
        )


@dp.message(EditScheduleFlow.new_note)
async def save_new_note(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lesson_id = int(data["lesson_id"])
    note = "" if (message.text or "").strip() == SKIP_BUTTON else (message.text or "").strip()
    update_lesson_note(lesson_id, note)
    await message.answer("<b>Заметка обновлена</b>", reply_markup=main_keyboard())
    await show_lesson_editor(message, state, lesson_id)


@dp.callback_query(F.data.startswith("edit_delete:"))
async def request_delete_lesson(callback: CallbackQuery, state: FSMContext) -> None:
    lesson_id = int((callback.data or "").split(":", 1)[1])
    lesson = get_lesson(lesson_id)
    if not lesson:
        await callback.answer("Занятие уже удалено.", show_alert=True)
        return

    await state.update_data(lesson_id=lesson_id)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "<b>Удалить занятие?</b>\n\n" + format_lesson(lesson),
            reply_markup=confirm_delete_keyboard(lesson_id),
        )


@dp.callback_query(F.data.startswith("confirm_delete:"))
async def confirm_delete_lesson(callback: CallbackQuery, state: FSMContext) -> None:
    lesson_id = int((callback.data or "").split(":", 1)[1])
    removed = delete_lesson(lesson_id)
    await state.clear()
    await callback.answer("Удалено" if removed else "Не нашел занятие")
    if callback.message:
        await callback.message.answer(
            "<b>Занятие удалено</b>" if removed else "Не нашел занятие с таким ID.",
            reply_markup=main_keyboard(),
        )


@dp.message(Command("new_lesson"))
@dp.message(F.text == ADD_LESSON_BUTTON)
@dp.message(F.text == OLD_ADD_LESSON_BUTTON)
async def new_lesson_command(message: Message, state: FSMContext) -> None:
    students = list_students()
    if not students:
        await message.answer(
            "Сначала добавь ученика. Нажми кнопку <b>Новый ученик</b>.",
            reply_markup=main_keyboard(),
        )
        return

    await state.clear()
    await state.set_state(AddLessonFlow.student_id)
    await message.answer(
        "<b>Новое занятие</b>\n\nВыбери ученика:",
        reply_markup=students_keyboard(students),
    )


@dp.callback_query(AddLessonFlow.student_id, F.data.startswith("student:"))
async def choose_lesson_student(callback: CallbackQuery, state: FSMContext) -> None:
    student_id = int((callback.data or "").split(":", 1)[1])
    if not student_exists(student_id):
        await callback.answer("Ученика уже нет в базе.", show_alert=True)
        return

    await state.update_data(student_id=student_id)
    await state.set_state(AddLessonFlow.starts_at)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "<b>Дата и время</b>\n\n"
            "Введи в формате <code>YYYY-MM-DD HH:MM</code>\n"
            "Например: <code>2026-04-29 18:30</code>",
            reply_markup=cancel_keyboard(),
        )


@dp.message(AddLessonFlow.starts_at)
async def choose_lesson_start(message: Message, state: FSMContext) -> None:
    try:
        starts_at = parse_local_datetime(message.text or "")
    except ValueError:
        await message.answer(
            "Не понял дату.\n\nПример правильного формата: <code>2026-04-29 18:30</code>"
        )
        return

    await state.update_data(starts_at=dt_to_db(starts_at))
    await state.set_state(AddLessonFlow.duration)
    await message.answer(
        "<b>Длительность</b>\n\nВыбери вариант или введи свое число минут:",
        reply_markup=duration_keyboard(),
    )


@dp.message(AddLessonFlow.duration)
async def choose_lesson_duration(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Длительность должна быть числом минут, например <code>60</code>.")
        return

    await state.update_data(duration_minutes=int(text))
    await state.set_state(AddLessonFlow.recurrence)
    await message.answer(
        "<b>Повтор</b>\n\nЭто разовое занятие или еженедельное?",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer("Выбери тип занятия:", reply_markup=recurrence_keyboard())


@dp.callback_query(AddLessonFlow.recurrence, F.data.startswith("recurrence:"))
async def choose_lesson_recurrence(callback: CallbackQuery, state: FSMContext) -> None:
    recurrence = (callback.data or "").split(":", 1)[1]
    if recurrence not in {"once", "weekly"}:
        await callback.answer("Неизвестный тип занятия.", show_alert=True)
        return

    await state.update_data(recurrence=recurrence)
    await state.set_state(AddLessonFlow.note)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "<b>Заметка</b>\n\nДобавь заметку к занятию или нажми <b>Пропустить</b>.",
            reply_markup=skip_keyboard(),
        )


@dp.message(AddLessonFlow.note)
async def finish_lesson_flow(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    note = "" if (message.text or "").strip() == SKIP_BUTTON else (message.text or "").strip()
    lesson_id = add_lesson(
        student_id=int(data["student_id"]),
        starts_at=dt_from_db(data["starts_at"]),
        duration_minutes=int(data["duration_minutes"]),
        recurrence=str(data["recurrence"]),
        note=note,
    )
    await state.clear()
    await message.answer(
        f"<b>Занятие добавлено</b>\n\nНомер: <code>#{lesson_id}</code>",
        reply_markup=main_keyboard(),
    )


@dp.message(F.text == ADD_STUDENT_BUTTON)
@dp.message(F.text == OLD_ADD_STUDENT_BUTTON)
async def new_student_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddStudentFlow.name)
    await message.answer("<b>Новый ученик</b>\n\nВведи имя:", reply_markup=cancel_keyboard())


@dp.message(AddStudentFlow.name)
async def choose_student_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не должно быть пустым.")
        return

    await state.update_data(name=name)
    await state.set_state(AddStudentFlow.subject)
    await message.answer("<b>Предмет</b>\n\nВведи предмет или направление:", reply_markup=cancel_keyboard())


@dp.message(AddStudentFlow.subject)
async def choose_student_subject(message: Message, state: FSMContext) -> None:
    subject = (message.text or "").strip()
    if not subject:
        await message.answer("Предмет не должен быть пустым.")
        return

    await state.update_data(subject=subject)
    await state.set_state(AddStudentFlow.notes)
    await message.answer(
        "<b>Заметка</b>\n\nДобавь заметку по ученику или нажми <b>Пропустить</b>.",
        reply_markup=skip_keyboard(),
    )


@dp.message(AddStudentFlow.notes)
async def finish_student_flow(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    notes = "" if (message.text or "").strip() == SKIP_BUTTON else (message.text or "").strip()
    student_id = add_student(str(data["name"]), str(data["subject"]), notes)
    await state.clear()
    await message.answer(
        f"<b>Ученик добавлен</b>\n\n"
        f"{h(data['name'])}\n"
        f"Номер: <code>#{student_id}</code>",
        reply_markup=main_keyboard(),
    )


@dp.message(F.text == TODAY_BUTTON)
async def today_button(message: Message) -> None:
    await today_command(message)


@dp.message(F.text == WEEK_BUTTON)
async def week_button(message: Message) -> None:
    await week_command(message)


@dp.message(F.text == LESSONS_BUTTON)
async def lessons_button(message: Message) -> None:
    await lessons_command(message)


@dp.message(Command("add_student"))
async def add_student_command(message: Message) -> None:
    try:
        name, subject, *rest = split_args(message.text or "", 2)
    except ValueError:
        await message.answer("Формат: <code>/add_student Имя | Предмет | Заметки</code>")
        return

    notes = rest[0] if rest else ""
    student_id = add_student(name, subject, notes)
    await message.answer(
        f"<b>Ученик добавлен</b>\n\n{h(name)}\nНомер: <code>#{student_id}</code>",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("students"))
async def students_command(message: Message) -> None:
    students = list_students()
    if not students:
        await message.answer(
            "<b>Ученики</b>\n\nПока нет учеников. Нажми <b>Новый ученик</b>.",
            reply_markup=main_keyboard(),
        )
        return

    lines = ["<b>Ученики</b>"]
    for student in students:
        note = f"\nЗаметка: {h(student['notes'])}" if student["notes"] else ""
        lines.append(
            f"<b>{h(student['name'])}</b>\n"
            f"Предмет: {h(student['subject'])}\n"
            f"Номер: <code>#{student['id']}</code>"
            f"{note}"
        )
    await message.answer("\n\n".join(lines))


@dp.message(Command("add_lesson"))
async def add_lesson_command(message: Message) -> None:
    try:
        student_id_raw, starts_raw, duration_raw, recurrence, *rest = split_args(
            message.text or "", 4
        )
        student_id = int(student_id_raw)
        starts_at = parse_local_datetime(starts_raw)
        duration_minutes = int(duration_raw)
    except ValueError:
        await message.answer(
            "Формат:\n"
            "<code>/add_lesson ID_ученика | YYYY-MM-DD HH:MM | 60 | once/weekly | заметка</code>"
        )
        return

    recurrence = recurrence.lower()
    if recurrence not in {"once", "weekly"}:
        await message.answer("Тип занятия должен быть <code>once</code> или <code>weekly</code>.")
        return
    if duration_minutes <= 0:
        await message.answer("Длительность должна быть больше нуля.")
        return
    if not student_exists(student_id):
        await message.answer("Не нашел ученика с таким ID. Проверь <code>/students</code>.")
        return

    note = rest[0] if rest else ""
    lesson_id = add_lesson(student_id, starts_at, duration_minutes, recurrence, note)
    await message.answer(
        f"<b>Занятие добавлено</b>\n\nНомер: <code>#{lesson_id}</code>",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("today"))
async def today_command(message: Message) -> None:
    start = now_local().replace(hour=0, minute=0)
    end = start + timedelta(days=1)
    await message.answer(
        format_lessons(lessons_between(start, end), "Сегодня занятий нет.", "Сегодня")
    )


@dp.message(Command("week"))
async def week_command(message: Message) -> None:
    start = now_local().replace(hour=0, minute=0)
    end = start + timedelta(days=7)
    await message.answer(
        format_lessons(
            lessons_between(start, end),
            "На ближайшие 7 дней занятий нет.",
            "Ближайшие 7 дней",
        )
    )


@dp.message(Command("lessons"))
async def lessons_command(message: Message) -> None:
    await message.answer(
        format_lessons(upcoming_lessons(), "Будущих занятий пока нет.", "Ближайшие занятия")
    )


@dp.message(Command("delete_lesson"))
async def delete_lesson_command(message: Message) -> None:
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2 or not args[1].strip().isdigit():
        await message.answer("Формат: <code>/delete_lesson ID_занятия</code>")
        return

    removed = delete_lesson(int(args[1]))
    await message.answer(
        "<b>Занятие удалено</b>" if removed else "Не нашел занятие с таким ID.",
        reply_markup=main_keyboard(),
    )


async def reminder_loop(bot: Bot) -> None:
    while True:
        try:
            advance_finished_weekly_lessons()
            chat_id = os.getenv("TEACHER_CHAT_ID") or get_setting("teacher_chat_id")
            if chat_id:
                for lesson in due_reminders():
                    minutes_left = max(1, int((lesson.starts_at - now_local()).total_seconds() // 60))
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"<b>Напоминание</b>\n\nЧерез {minutes_left} мин занятие.\n\n"
                            + format_lesson(lesson)
                        ),
                    )
                    mark_reminded(lesson)
        except Exception:
            logger.exception("Reminder loop failed")
        await asyncio.sleep(30)


async def main() -> None:
    init_db()
    bot_properties = DefaultBotProperties(parse_mode=ParseMode.HTML)
    if PROXY_URL:
        logger.info("Using Telegram proxy from PROXY_URL")
        bot = Bot(BOT_TOKEN, session=AiohttpSession(proxy=PROXY_URL), default=bot_properties)
    else:
        bot = Bot(BOT_TOKEN, default=bot_properties)
    logger.info("Bot started")
    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
