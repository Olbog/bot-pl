"""Внутренние константы, общие для модулей бота (не настройки)."""
import logging
import re

log = logging.getLogger("bot-pl")

WEEKDAYS = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
TEXT_STEPS = ("topic", "own", "add", "dict_own", "rule", "new_topic")   # шаги, где ждём текст, а не реплику разговора
QUESTION_RE = re.compile(r"^\s*(\d{1,2})\s*[:.)\-]\s*(.+)$", re.S)   # «5: почему…» — вопрос по пункту упражнения

AUTO = object()                       # show(): предыдущий шаг определить автоматически
SET_MENU = {"step": "set_menu"}       # «Назад» → меню /set
DICT_MENU = {"step": "dict_menu"}     # «Назад» → словарь /dict
