# ============================================
# Telegram Sender Pro
# Copyright (c) 2025 Palamarchuk Volodymyr
# Licensed under the MIT License
# See LICENSE file for details
# ============================================

import asyncio
import json
import os
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog, filedialog

from telethon import TelegramClient
from telethon.errors import ChannelPrivateError, ChatAdminRequiredError, ApiIdInvalidError
from telethon.sessions import SQLiteSession
from telethon.tl.functions.channels import GetForumTopicsRequest


# ============================================
# DEDICATED TELETHON WORKER (single client/loop)
# ============================================
class TelethonWorker:
    def __init__(self):
        self.thread = None
        self.loop = None
        self.client = None
        self.lock = None
        self._ready = threading.Event()

    def start(self, app):
        if self.thread is not None:
            return

        self._ready.clear()
        exception_holder = {}

        def runner():
            try:
                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)
                self.lock = asyncio.Lock()

                async def _ensure_client():
                    self.client = await init_client(app, app.config["api_id"], app.config["api_hash"],
                                                    app.config["phone"])

                self.loop.run_until_complete(_ensure_client())
            except Exception as exc:
                exception_holder['exc'] = exc
                self._ready.set()
                return

            self._ready.set()
            try:
                self.loop.run_forever()
            finally:
                try:
                    pending = asyncio.all_tasks(loop=self.loop)
                    for t in list(pending):
                        t.cancel()
                    if pending:
                        self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
                try:
                    if self.client and self.loop.run_until_complete(self.client.is_connected()):
                        self.loop.run_until_complete(self.client.disconnect())
                except Exception:
                    pass
                self.loop.stop()
                self.loop.close()

        self.thread = threading.Thread(target=runner, name="TelethonWorker", daemon=True)
        self.thread.start()
        self._ready.wait()

        if 'exc' in exception_holder:
            try:
                self.thread.join(timeout=0.1)
            except Exception:
                pass
            self.thread = None
            self.loop = None
            self.client = None
            self.lock = None
            raise exception_holder['exc']

    def call(self, coro_factory):
        if self.thread is None:
            raise RuntimeError("TelethonWorker не запущен. Вызовите start(app).")

        async def _run_serialized():
            async with self.lock:
                return await coro_factory(self.client)

        fut = asyncio.run_coroutine_threadsafe(_run_serialized(), self.loop)
        return fut.result()


TG_WORKER = TelethonWorker()

# ============================================
# ЛОГИРОВАНИЕ
# ============================================
import logging
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "app.log")

_logger = logging.getLogger("app")
_logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
if not _logger.handlers:
    _logger.addHandler(_handler)

# ============================================
# КОНФИГУРАЦИЯ
# ============================================
USER_CONFIG = "config.json"
APP_DATA_FILE = "app_data.json"


def load_config():
    defaults = {"api_id": "", "api_hash": "", "phone": "", "rate_delay": 10.0}
    if os.path.exists(USER_CONFIG):
        try:
            with open(USER_CONFIG, 'r', encoding='utf-8') as f:
                data = json.load(f)
                defaults.update(data)
                return defaults
        except (json.JSONDecodeError, IOError):
            pass
    return defaults


def save_config(api_id, api_hash, phone, rate_delay):
    config = {"api_id": api_id, "api_hash": api_hash, "phone": phone, "rate_delay": rate_delay}
    with open(USER_CONFIG, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def load_app_data():
    defaults = {"groups": [], "themes": [], "tags": [], "templates": []}
    if os.path.exists(APP_DATA_FILE):
        try:
            with open(APP_DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for k, v in defaults.items():
                    if k not in data:
                        data[k] = v
                for item_list in ("groups", "themes"):
                    for item in data.get(item_list, []):
                        if 'client_number' not in item:
                            item['client_number'] = item.pop('cabinet', "")
                        if 'name' not in item:
                            item['name'] = ""
                        if 'custom_templates' not in item or not isinstance(item.get('custom_templates'), dict):
                            item['custom_templates'] = {}
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return defaults


def save_app_data(data):
    with open(APP_DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ============================================
# TELEGRAM
# ============================================
async def init_client(app, api_id, api_hash, phone):
    session_name = f"session_{phone.strip().replace('+', '')}"
    session = SQLiteSession(session_name)
    local_client = TelegramClient(session, api_id, api_hash)

    def code_callback():
        code = app.get_input_from_dialog("Код подтверждения", "Введите код из SMS/Telegram:")
        if not code:
            raise Exception("Код не введен/Отменен")
        return code

    def password_callback():
        password = app.get_input_from_dialog("Двухфакторная авторизация", "Введите пароль 2FA:", show='*')
        if not password:
            raise Exception("Пароль 2FA не введен/Отменен")
        return password

    await local_client.start(phone=phone, code_callback=code_callback, password=password_callback)
    return local_client


async def get_user_groups(client):
    groups = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            groups.append({
                "id": dialog.id,
                "name": dialog.title,
                "username": getattr(dialog.entity, 'username', "")
            })
    return groups


async def get_group_topics(client, group_id):
    try:
        entity = await client.get_entity(group_id)
        if not getattr(entity, 'forum', False):
            return [], "Это не группа-форум, или темы отключены."
        result = await client(
            GetForumTopicsRequest(channel=entity, offset_date=0, offset_id=0, offset_topic=0, limit=100))
        topics = [{"topic_id": t.id, "name": t.title} for t in result.topics if
                  not (t.closed or (t.hidden and t.id != 1))]
        return topics, None
    except (ValueError, TypeError):
        return [], f"Неверный ID группы: {group_id}"
    except (ChannelPrivateError, ChatAdminRequiredError):
        return [], "Ошибка доступа: проверьте, что вы состоите в группе и у вас есть права на просмотр."
    except Exception as e:
        return [], f"Неизвестная ошибка: {e}"


# ============================================
# GUI ПРИЛОЖЕНИЕ
# ============================================
class TelegramSenderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Telegram Sender Pro")
        self.root.geometry("1200x900")

        self.colors = {
            'bg': '#f9fafb',
            'card': '#ffffff',
            'primary': '#2563eb',
            'success': '#16a34a',
            'danger': '#dc2626',
            'warning': '#f59e0b',
            'text': '#1f2937',
            'text_light': '#4b5563',
            'border': '#e5e7eb',
            'input_bg': '#ffffff',
            'input_fg': '#111827',
            'tag_filter_bg': '#f3f4f6',
            'hover': '#e0e7ff'
        }
        self.root.configure(bg=self.colors['bg'])
        self._setup_base_styles()

        self.is_sending = False
        self.config = load_config()
        self.app_data = load_app_data()
        self.fetched_groups = []
        self.fetched_topics = []

        self.create_widgets()
        self.load_saved_config()
        self.refresh_all_lists()

    # ---------- СТИЛИ ----------
    def _setup_base_styles(self):
        style = ttk.Style()
        style.theme_use('clam')
        # Notebook
        style.configure('TNotebook', background=self.colors['bg'], borderwidth=0)
        style.configure('TNotebook.Tab', background=self.colors['card'], foreground=self.colors['text_light'],
                        padding=[20, 12], font=('Segoe UI', 10), borderwidth=0)
        style.map('TNotebook.Tab', background=[('selected', self.colors['primary'])],
                  foreground=[('selected', '#ffffff')])
        # Card
        style.configure('Card.TLabelframe', background=self.colors['card'], bordercolor=self.colors['border'],
                        borderwidth=1, relief='solid')
        style.configure('Card.TLabelframe.Label', background=self.colors['card'], foreground=self.colors['text'],
                        font=('Segoe UI', 11, 'bold'))
        # Combobox
        style.configure('TCombobox', fieldbackground=self.colors['input_bg'], background=self.colors['input_bg'],
                        foreground=self.colors['input_fg'], arrowcolor=self.colors['text'],
                        bordercolor=self.colors['border'], selectbackground=self.colors['primary'],
                        selectforeground='#ffffff')
        style.map('TCombobox', fieldbackground=[('readonly', self.colors['input_bg'])])
        # Кнопки (ttk)
        self._mk_button_styles()

    def _mk_button_styles(self):
        style = ttk.Style()
        base = {'font': ('Segoe UI', 10, 'bold'), 'padding': (16, 10)}
        variants = {
            'Primary': self.colors['primary'],
            'Success': self.colors['success'],
            'Danger': self.colors['danger'],
            'Secondary': '#64748b',
        }
        for name, color in variants.items():
            darker = self._adjust_color(color, 0.9)
            pressed = self._adjust_color(color, 0.8)
            stylename = f'Btn.{name}.TButton'
            style.configure(stylename, background=color, foreground='#ffffff', **base)
            style.map(stylename,
                      background=[('active', darker), ('pressed', pressed), ('disabled', '#cbd5e1')],
                      foreground=[('disabled', '#ffffff')])

    def _adjust_color(self, color, factor=0.9):
        try:
            r16, g16, b16 = self.root.winfo_rgb(color)
            r = max(0, min(255, int((r16 / 65535) * 255 * factor)))
            g = max(0, min(255, int((g16 / 65535) * 255 * factor)))
            b = max(0, min(255, int((b16 / 65535) * 255 * factor)))
            return f'#{r:02x}{g:02x}{b:02x}'
        except Exception:
            return color

    # ---------- ЕДИНЫЕ ФАБРИКИ ВИДЖЕТОВ ----------
    def create_button(self, parent, text, command, variant='primary', **kwargs):
        style_map = {
            'primary': 'Btn.Primary.TButton',
            'success': 'Btn.Success.TButton',
            'danger': 'Btn.Danger.TButton',
            'secondary': 'Btn.Secondary.TButton',
        }
        style_name = style_map.get(str(variant).lower())
        if not style_name:
            # кастомный цвет: сгенерировать стиль
            color = variant if isinstance(variant, str) else self.colors['primary']
            darker = self._adjust_color(color, 0.9)
            pressed = self._adjust_color(color, 0.8)
            style_name = f"Btn.Custom_{color.replace('#', '')}.TButton"
            s = ttk.Style()
            s.configure(style_name, background=color, foreground='#ffffff', font=('Segoe UI', 10, 'bold'),
                        padding=(16, 10))
            s.map(style_name,
                  background=[('active', darker), ('pressed', pressed), ('disabled', '#cbd5e1')],
                  foreground=[('disabled', '#ffffff')])
        return ttk.Button(parent, text=text, command=command, style=style_name, **kwargs)

    def create_card(self, parent, title):
        return ttk.LabelFrame(parent, text=title, style='Card.TLabelframe', padding=20)

    def mk_label(self, parent, text, bold=False, color=None):
        return tk.Label(parent, text=text, bg=self.colors['card'] if parent != self.root else self.colors['bg'],
                        fg=color or self.colors['text'], font=('Segoe UI', 10, 'bold' if bold else 'normal'))

    def _bind_clipboard_shortcuts(self, widget):
        if platform.system() == "Darwin":
            mapping = {
                '<Command-c>': '<<Copy>>', '<Command-C>': '<<Copy>>',
                '<Command-v>': '<<Paste>>', '<Command-V>': '<<Paste>>',
                '<Command-x>': '<<Cut>>', '<Command-X>': '<<Cut>>',
            }
            for seq, ev in mapping.items():
                widget.bind(seq, lambda e, ev=ev: widget.event_generate(ev) or 'break')
        return widget

    def add_context_menu(self, widget):
        menu = tk.Menu(widget, tearoff=0, bg=self.colors['card'], fg=self.colors['text'])
        menu.add_command(label="Вырезать", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="Копировать", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Вставить", command=lambda: widget.event_generate("<<Paste>>"))
        widget.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
        widget.bind("<Control-Button-1>", lambda e: menu.tk_popup(e.x_root, e.y_root))
        self._bind_clipboard_shortcuts(widget)

    def mk_entry(self, parent, **kwargs):
        entry = tk.Entry(parent, font=('Segoe UI', 10), bg=self.colors['input_bg'], fg=self.colors['input_fg'],
                         relief='solid', bd=1, insertbackground=self.colors['text'], **kwargs)
        self.add_context_menu(entry)
        return entry

    def mk_text(self, parent, **kwargs):
        txt = scrolledtext.ScrolledText(parent, wrap=tk.WORD, font=('Segoe UI', 10),
                                        bg=self.colors['input_bg'], fg=self.colors['input_fg'],
                                        relief='solid', bd=1, insertbackground=self.colors['text'], **kwargs)
        self.add_context_menu(txt)
        return txt

    def mk_combobox(self, parent, **kwargs):
        cb = ttk.Combobox(parent, **kwargs)
        return cb

    def mk_checkbutton(self, parent, text, var, bg_card=True):
        return tk.Checkbutton(parent, text=text, variable=var,
                              bg=self.colors['card'] if bg_card else self.colors['tag_filter_bg'],
                              fg=self.colors['text'], selectcolor=self.colors['input_bg'],
                              activebackground=self.colors['card'] if bg_card else self.colors['tag_filter_bg'],
                              activeforeground=self.colors['text'], font=('Segoe UI', 9))

    def mk_listbox(self, parent, height=12):
        frame = tk.Frame(parent, bg=self.colors['card'])
        frame.pack(fill='both', expand=True, pady=(0, 15))
        listbox = tk.Listbox(frame, font=('Segoe UI', 9), bg=self.colors['input_bg'], fg=self.colors['input_fg'],
                             relief='solid', bd=1, selectbackground=self.colors['primary'], selectforeground='#ffffff',
                             height=height, exportselection=False)
        scrollbar = tk.Scrollbar(frame, orient='vertical', command=listbox.yview)
        listbox.config(yscrollcommand=scrollbar.set)
        listbox.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        return listbox

    # ---------- УТИЛИТЫ ----------
    def _on_mousewheel(self, event, canvas):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _bind_mousewheel(self, widget, canvas):
        widget.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", lambda ev: self._on_mousewheel(ev, canvas)))
        widget.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

    # ---------- UI ----------
    def create_widgets(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=0, pady=0)
        tabs = {
            "  ⚙️  Настройки  ": self.create_settings_tab,
            "  📋  Управление списками  ": self.create_manage_tab,
            "  📥  Получить группы  ": self.create_fetch_tab,
            "  🔎  Проверка тем  ": self.create_topic_checker_tab,
            "  📤  Отправка сообщений  ": self.create_sending_tab
        }
        for text, creator in tabs.items():
            tab_frame = tk.Frame(self.notebook, bg=self.colors['bg'])
            self.notebook.add(tab_frame, text=text)
            creator(tab_frame)

    def create_settings_tab(self, parent):
        container = tk.Frame(parent, bg=self.colors['bg']);
        container.place(relx=0.5, rely=0.5, anchor='center')
        card = self.create_card(container, "🔐  Настройки Telegram API");
        card.pack(padx=40, pady=20)
        fields_frame = tk.Frame(card, bg=self.colors['card']);
        fields_frame.pack(fill="x", pady=10)

        def field(label, row):
            self.mk_label(fields_frame, label, bold=True).grid(row=row, column=0, sticky='w', pady=12, padx=(0, 15))
            e = self.mk_entry(fields_frame);
            e.grid(row=row, column=1, sticky='ew', pady=12);
            e.config(width=40)
            return e

        self.api_id_entry = field("API ID:", 0)
        self.api_hash_entry = field("API Hash:", 1)
        self.phone_entry = field("Телефон:", 2)
        self.rate_delay_entry = field("Задержка (сек на отправку):", 3)
        fields_frame.columnconfigure(1, weight=1)

        btn_frame = tk.Frame(card, bg=self.colors['card']);
        btn_frame.pack(pady=20)
        self.create_button(btn_frame, "💾  Сохранить настройки", self.save_settings, variant='success').pack()
        self.settings_status = tk.Label(card, text="", bg=self.colors['card'], fg=self.colors['text'],
                                        font=('Segoe UI', 10))
        self.settings_status.pack(pady=10)

    def create_manage_tab(self, parent):
        main = tk.Frame(parent, bg=self.colors['bg']);
        main.pack(fill="both", expand=True, padx=20, pady=20)
        main.grid_columnconfigure((0, 1, 2), weight=1);
        main.grid_rowconfigure(0, weight=1)
        self.create_tags_manager(main).grid(row=0, column=0, sticky='nsew', padx=(0, 10))
        self.create_groups_manager(main).grid(row=0, column=1, sticky='nsew', padx=(5, 5))
        self.create_themes_manager(main).grid(row=0, column=2, sticky='nsew', padx=(10, 0))

    def create_tags_manager(self, parent):
        card = self.create_card(parent, "🏷️  Управление тегами")
        self.tags_listbox = self.mk_listbox(card)
        form = tk.Frame(card, bg=self.colors['card']);
        form.pack(fill='x', pady=5)
        self.mk_label(form, "Новый тег:", bold=True).pack(side='left')
        self.tag_name_entry = self.mk_entry(form);
        self.tag_name_entry.pack(side='left', fill='x', expand=True, padx=(10, 0))
        btns = tk.Frame(card, bg=self.colors['card']);
        btns.pack(fill='x', pady=10)
        self.create_button(btns, "➕", self.add_tag, variant='success', width=5).pack(side='left', expand=True, fill='x',
                                                                                     padx=2)
        self.create_button(btns, "❌", self.delete_tag, variant='danger', width=5).pack(side='left', expand=True,
                                                                                       fill='x', padx=2)
        return card

    def create_groups_manager(self, parent):
        card = self.create_card(parent, "📁  Управление группами")
        self.groups_listbox = self.mk_listbox(card)
        form = tk.Frame(card, bg=self.colors['card']);
        form.pack(fill='x', pady=10);
        form.columnconfigure(1, weight=1)
        for i, (text, name) in enumerate(
                [("ID:", "group_id_entry"), ("Название:", "group_name_entry"), ("Кабинет:", "group_cabinet_entry")]):
            self.mk_label(form, text, bold=True).grid(row=i, column=0, sticky='w', pady=4)
            setattr(self, name, self.mk_entry(form));
            getattr(self, name).grid(row=i, column=1, sticky='ew', pady=4, padx=(10, 0))
        btns = tk.Frame(card, bg=self.colors['card']);
        btns.pack(fill='x', pady=10)
        self.create_button(btns, "➕", self.add_group, variant='success', width=5).pack(side='left', expand=True,
                                                                                       fill='x', padx=2)
        self.create_button(btns, "✏️", lambda: self.edit_item_tags('group'), variant='primary', width=5).pack(
            side='left', expand=True, fill='x', padx=2)
        self.create_button(btns, "❌", self.delete_group, variant='danger', width=5).pack(side='left', expand=True,
                                                                                         fill='x', padx=2)
        return card

    def create_themes_manager(self, parent):
        card = self.create_card(parent, "🧵  Управление темами")
        self.themes_listbox = self.mk_listbox(card)
        form = tk.Frame(card, bg=self.colors['card']);
        form.pack(fill='x', pady=10);
        form.columnconfigure(1, weight=1)
        for i, (text, name) in enumerate([("ID группы:", "theme_group_id_entry"), ("ID темы:", "theme_topic_id_entry"),
                                          ("Название:", "theme_name_entry"), ("Кабинет:", "theme_cabinet_entry")]):
            self.mk_label(form, text, bold=True).grid(row=i, column=0, sticky='w', pady=4)
            setattr(self, name, self.mk_entry(form));
            getattr(self, name).grid(row=i, column=1, sticky='ew', pady=4, padx=(10, 0))
        btns = tk.Frame(card, bg=self.colors['card']);
        btns.pack(fill='x', pady=10)
        self.create_button(btns, "➕", self.add_theme, variant='success', width=5).pack(side='left', expand=True,
                                                                                       fill='x', padx=2)
        self.create_button(btns, "✏️", lambda: self.edit_item_tags('theme'), variant='primary', width=5).pack(
            side='left', expand=True, fill='x', padx=2)
        self.create_button(btns, "❌", self.delete_theme, variant='danger', width=5).pack(side='left', expand=True,
                                                                                         fill='x', padx=2)
        return card

    def create_fetch_tab(self, parent):
        container = tk.Frame(parent, bg=self.colors['bg']);
        container.place(relx=0.5, rely=0.5, anchor='center')
        card = self.create_card(container, "📥  Получить список всех групп");
        card.pack(padx=40, pady=20)
        self.fetch_btn = self.create_button(card, "🔄  Загрузить мои группы", self.fetch_user_groups, variant='primary')
        self.fetch_btn.pack(pady=20)
        self.fetched_groups_listbox = self.mk_listbox(card);
        self.fetched_groups_listbox.config(selectmode='multiple', width=70)
        self.create_button(card, "➕  Добавить выбранные в список", self.add_fetched_groups, variant='success').pack(
            pady=15)

    def create_topic_checker_tab(self, parent):
        container = tk.Frame(parent, bg=self.colors['bg']);
        container.place(relx=0.5, rely=0.5, anchor='center')
        card = self.create_card(container, "🔎  Получить темы из группы-форума");
        card.pack(padx=40, pady=20, fill='x')
        self.mk_label(card, "Выберите группу-форум:", bold=True).pack(anchor='w', pady=(0, 5))
        self.topic_check_group_combo = self.mk_combobox(card, state="readonly", font=('Segoe UI', 10))
        self.topic_check_group_combo.pack(fill='x', pady=(0, 15))
        self.fetch_topics_btn = self.create_button(card, "🔍  Получить темы", self.fetch_group_topics, variant='primary')
        self.fetch_topics_btn.pack(pady=10)
        self.fetched_topics_listbox = self.mk_listbox(card);
        self.fetched_topics_listbox.config(selectmode='multiple', width=70)
        self.create_button(card, "➕  Добавить выбранные темы в список", self.add_fetched_topics,
                           variant='success').pack(pady=15)

    def create_sending_tab(self, parent):
        main = tk.Frame(parent, bg=self.colors['bg']);
        main.pack(fill='both', expand=True, padx=20, pady=20)
        self.left_col_sending = tk.Frame(main, bg=self.colors['bg']);
        self.left_col_sending.pack(side='left', fill='both', expand=True, padx=(0, 10))
        right = tk.Frame(main, bg=self.colors['bg']);
        right.pack(side='right', fill='both', expand=True, padx=(10, 0))

        templates_card = self.create_card(right, "📝  Шаблоны сообщений");
        templates_card.pack(fill='x', pady=(0, 10))
        self.create_templates_manager(templates_card)

        msg_card = self.create_card(right, "✉️  Текст сообщения");
        msg_card.pack(fill='both', expand=True, pady=(0, 10))
        self.message_text = self.mk_text(msg_card, height=10);
        self.message_text.pack(fill='both', expand=True)

        self.send_btn = self.create_button(right, "📨  Отправить сообщения", self.prepare_send, variant='success')
        self.send_btn.config(width=20)
        self.send_btn.pack(pady=10, fill='x')

        log_card = self.create_card(right, "📊  Лог отправки");
        log_card.pack(fill='both', expand=True)
        self.log_text = scrolledtext.ScrolledText(log_card, height=8, state='disabled', font=('Consolas', 9),
                                                  bg=self.colors['input_bg'], fg=self.colors['text'], relief='solid',
                                                  bd=1)
        self.log_text.pack(fill='both', expand=True)

    def create_templates_manager(self, parent):
        self.templates_listbox = self.mk_listbox(parent, height=5)
        btns = tk.Frame(parent, bg=self.colors['card']);
        btns.pack(fill='x', pady=10)
        self.create_button(btns, "📥", self.use_template, variant='primary', width=5).pack(side='left', expand=True,
                                                                                          fill='x', padx=2)
        self.create_button(btns, "💾", self.save_template, variant='success', width=5).pack(side='left', expand=True,
                                                                                           fill='x', padx=2)
        self.create_button(btns, "❌", self.delete_template, variant='danger', width=5).pack(side='left', expand=True,
                                                                                            fill='x', padx=2)

    def build_sending_lists(self, parent):
        for w in parent.winfo_children(): w.destroy()
        tags_card = self.create_card(parent, "🏷️  Фильтр по тегам");
        tags_card.pack(fill='x', pady=(0, 10))
        tags_outer = tk.Frame(tags_card, bg=self.colors['tag_filter_bg'], bd=1, relief='solid', borderwidth=0)
        tags_outer.pack(fill='x', pady=5, padx=0)
        tags_frame = tk.Frame(tags_outer, bg=self.colors['tag_filter_bg']);
        tags_frame.pack(fill='x', padx=5, pady=5)

        self.tag_filter_vars = []
        self.all_tags_var = tk.BooleanVar(value=True)

        def toggle_all_tags():
            is_checked = self.all_tags_var.get()
            for var, _ in self.tag_filter_vars:
                var.set(is_checked)
            self.filter_sending_lists()

        def update_all_tags_state():
            all_checked = all(var.get() for var, _ in self.tag_filter_vars) if self.tag_filter_vars else True
            self.all_tags_var.set(all_checked)
            self.filter_sending_lists()

        all_cb = tk.Checkbutton(tags_frame, text="Все", variable=self.all_tags_var, command=toggle_all_tags,
                                font=('Segoe UI', 9, 'bold'), bg=self.colors['tag_filter_bg'], fg=self.colors['text'],
                                selectcolor='#e5e7eb', activebackground=self.colors['tag_filter_bg'],
                                activeforeground=self.colors['text'])
        all_cb.pack(side='left', padx=5)

        for tag in self.app_data["tags"]:
            var = tk.BooleanVar(value=True);
            self.tag_filter_vars.append((var, tag))
            cb = self.mk_checkbutton(tags_frame, tag, var, bg_card=False)
            cb.config(command=update_all_tags_state)
            cb.pack(side='left', padx=5)

        self.scrollable_groups_frame_container = self.create_card(parent, "📁  Выбор групп");
        self.scrollable_groups_frame_container.pack(fill='both', expand=True, pady=(0, 10))
        self.scrollable_themes_frame_container = self.create_card(parent, "🧵  Выбор тем");
        self.scrollable_themes_frame_container.pack(fill='both', expand=True)

        btn_select = tk.Frame(parent, bg=self.colors['bg']);
        btn_select.pack(fill='x', pady=10)
        self.create_button(btn_select, "✓", self.select_all, variant='primary', width=10).pack(side='left', padx=5,
                                                                                               expand=True, fill='x')
        self.create_button(btn_select, "✗", self.deselect_all, variant='secondary', width=10).pack(side='left', padx=5,
                                                                                                   expand=True,
                                                                                                   fill='x')

        self.filter_sending_lists()

    def filter_sending_lists(self):
        active_tags = {tag for var, tag in getattr(self, 'tag_filter_vars', []) if var.get()}
        show_all = self.all_tags_var.get() if hasattr(self, 'all_tags_var') else True

        for container in [self.scrollable_groups_frame_container, self.scrollable_themes_frame_container]:
            for widget in container.winfo_children(): widget.destroy()

        def populate(container, items, is_group):
            canvas = tk.Canvas(container, bg=self.colors['card'], highlightthickness=0)
            scrollbar = tk.Scrollbar(container, orient='vertical', command=canvas.yview)
            inner = tk.Frame(canvas, bg=self.colors['card'])
            inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=inner, anchor='nw');
            canvas.configure(yscrollcommand=scrollbar.set)
            self._bind_mousewheel(inner, canvas)

            item_vars = []
            count_added = 0
            for item in items:
                item_tags = set(item.get('tags', []))
                if show_all or (active_tags and item_tags.intersection(active_tags)):
                    var = tk.BooleanVar()
                    item_vars.append((var, item))
                    label = f"{item['name']} - Каб. {item.get('cabinet', 'N/A')}"
                    self.mk_checkbutton(inner, label, var, bg_card=True).pack(anchor='w', pady=3, padx=5)
                    count_added += 1

            if count_added == 0:
                self.mk_label(inner, "Нет элементов под выбранные теги", bold=False,
                              color=self.colors['text_light']).pack(pady=8, padx=8, anchor='w')

            canvas.pack(side='left', fill='both', expand=True);
            scrollbar.pack(side='right', fill='y')
            if is_group:
                self.group_vars = item_vars
            else:
                self.theme_vars = item_vars

        populate(self.scrollable_groups_frame_container, self.app_data["groups"], True)
        populate(self.scrollable_themes_frame_container, self.app_data["themes"], False)

    # ---------- ДАННЫЕ/ЛОГИКА ----------
    def refresh_all_lists(self):
        self.tags_listbox.delete(0, tk.END)
        for tag in self.app_data["tags"]: self.tags_listbox.insert(tk.END, tag)
        self.groups_listbox.delete(0, tk.END)
        for g in self.app_data["groups"]: self.groups_listbox.insert(tk.END, f"{g['name']} | ID: {g['id']}")
        self.themes_listbox.delete(0, tk.END)
        for t in self.app_data["themes"]: self.themes_listbox.insert(tk.END, f"{t['name']} | Группа: {t['group_id']}")
        self.templates_listbox.delete(0, tk.END)
        for tpl in self.app_data["templates"]: self.templates_listbox.insert(tk.END, tpl["name"])

        self.group_name_to_id_map = {f"{g['name']} (ID: {g['id']})": g['id'] for g in self.app_data["groups"]}
        self.topic_check_group_combo['values'] = list(self.group_name_to_id_map.keys())

        self.build_sending_lists(self.left_col_sending)

    def add_tag(self):
        tag_name = self.tag_name_entry.get().strip()
        if not tag_name: return messagebox.showwarning("Внимание", "Название тега не может быть пустым!")
        if tag_name in self.app_data["tags"]: return messagebox.showwarning("Внимание", "Такой тег уже существует.")
        self.app_data["tags"].append(tag_name);
        save_app_data(self.app_data);
        self.refresh_all_lists()
        self.tag_name_entry.delete(0, tk.END);
        messagebox.showinfo("Успех", "Тег добавлен!")

    def delete_tag(self):
        sel = self.tags_listbox.curselection()
        if not sel: return messagebox.showwarning("Внимание", "Выберите тег для удаления!")
        tag_to_delete = self.app_data["tags"][sel[0]]
        if messagebox.askyesno("Подтверждение",
                               f"Удалить тег '{tag_to_delete}'? Он также будет удален из всех групп и тем."):
            del self.app_data["tags"][sel[0]]
            for item in self.app_data["groups"] + self.app_data["themes"]:
                if "tags" in item and tag_to_delete in item["tags"]: item["tags"].remove(tag_to_delete)
            save_app_data(self.app_data);
            self.refresh_all_lists();
            messagebox.showinfo("Успех", "Тег удален!")

    def add_group(self):
        try:
            gid = int(self.group_id_entry.get().strip());
            name = self.group_name_entry.get().strip();
            cab = self.group_cabinet_entry.get().strip()
            if not name: return messagebox.showwarning("Внимание", "Укажите название группы!")
            self.app_data["groups"].append({"id": gid, "name": name, "cabinet": cab, "tags": []})
            save_app_data(self.app_data);
            self.refresh_all_lists()
            for e in [self.group_id_entry, self.group_name_entry, self.group_cabinet_entry]: e.delete(0, tk.END)
            messagebox.showinfo("Успех", "Группа добавлена!")
        except ValueError:
            messagebox.showerror("Ошибка", "ID группы должен быть числом!")

    def delete_group(self):
        sel = self.groups_listbox.curselection()
        if not sel: return messagebox.showwarning("Внимание", "Выберите группу для удаления!")
        if messagebox.askyesno("Подтверждение", "Удалить выбранную группу?"):
            del self.app_data["groups"][sel[0]];
            save_app_data(self.app_data);
            self.refresh_all_lists();
            messagebox.showinfo("Успех", "Группа удалена!")

    def add_theme(self):
        try:
            gid = int(self.theme_group_id_entry.get().strip());
            tid = int(self.theme_topic_id_entry.get().strip())
            name = self.theme_name_entry.get().strip();
            cab = self.theme_cabinet_entry.get().strip()
            if not name: return messagebox.showwarning("Внимание", "Укажите название темы!")
            self.app_data["themes"].append({"group_id": gid, "topic_id": tid, "name": name, "cabinet": cab, "tags": []})
            save_app_data(self.app_data);
            self.refresh_all_lists()
            for e in [self.theme_group_id_entry, self.theme_topic_id_entry, self.theme_name_entry,
                      self.theme_cabinet_entry]: e.delete(0, tk.END)
            messagebox.showinfo("Успех", "Тема добавлена!")
        except ValueError:
            messagebox.showerror("Ошибка", "ID группы и темы должны быть числами!")

    def delete_theme(self):
        sel = self.themes_listbox.curselection()
        if not sel: return messagebox.showwarning("Внимание", "Выберите тему для удаления!")
        if messagebox.askyesno("Подтверждение", "Удалить выбранную тему?"):
            del self.app_data["themes"][sel[0]];
            save_app_data(self.app_data);
            self.refresh_all_lists();
            messagebox.showinfo("Успех", "Тема удалена!")

    def edit_item_tags(self, item_type):
        listbox = self.groups_listbox if item_type == 'group' else self.themes_listbox
        data_list = self.app_data["groups"] if item_type == 'group' else self.app_data["themes"]
        sel = listbox.curselection()
        if not sel: return messagebox.showwarning("Внимание",
                                                  f"Выберите {'группу' if item_type == 'group' else 'тему'} для редактирования!")
        item = data_list[sel[0]]

        dialog = tk.Toplevel(self.root);
        dialog.title("Редактировать теги");
        dialog.configure(bg=self.colors['bg']);
        dialog.transient(self.root);
        dialog.grab_set()
        self.mk_label(dialog, f"Теги для '{item['name']}'", bold=True, color=self.colors['text']).pack(pady=15)

        vars_map = {};
        current = set(item.get("tags", []))
        frame = tk.Frame(dialog, bg=self.colors['card']);
        frame.pack(padx=20, pady=10, fill='x')
        for tag in self.app_data["tags"]:
            var = tk.BooleanVar(value=(tag in current));
            vars_map[tag] = var
            self.mk_checkbutton(frame, tag, var).pack(anchor='w')

        def save_tags():
            item["tags"] = [t for t, v in vars_map.items() if v.get()]
            save_app_data(self.app_data);
            self.refresh_all_lists();
            dialog.destroy()

        self.create_button(dialog, "Сохранить", save_tags, variant='success').pack(pady=20)

    def use_template(self):
        sel = self.templates_listbox.curselection()
        if not sel:
            return messagebox.showwarning("Внимание", "Выберите шаблон!")
        if not hasattr(self, 'message_text'):
            return messagebox.showwarning("Внимание", "Поле сообщения ещё не готово")
        try:
            text = self.app_data["templates"][sel[0]]["text"]
        except Exception:
            return messagebox.showerror("Ошибка", "Не удалось прочитать выбранный шаблон")
        self.message_text.delete("1.0", tk.END)
        self.message_text.insert("1.0", text)

    def save_template(self):
        if not hasattr(self, 'message_text'):
            return messagebox.showwarning("Внимание", "Поле сообщения ещё не готово")
        text = self.message_text.get("1.0", tk.END).strip()
        if not text:
            return messagebox.showwarning("Внимание", "Текст сообщения пуст!")
        name = simpledialog.askstring("Сохранить шаблон", "Название шаблона:", parent=self.root)
        if not name:
            return
        # перезапись по имени с подтверждением
        if any(t.get("name") == name for t in self.app_data.get("templates", [])):
            if not messagebox.askyesno("Внимание", "Шаблон с таким именем уже существует. Перезаписать?"):
                return
            self.app_data["templates"] = [t for t in self.app_data["templates"] if t.get("name") != name]
        self.app_data.setdefault("templates", []).append({"name": name, "text": text})
        save_app_data(self.app_data)
        self.refresh_all_lists()
        messagebox.showinfo("Успех", "Шаблон сохранен!")

    def delete_template(self):
        sel = self.templates_listbox.curselection()
        if not sel:
            return messagebox.showwarning("Внимание", "Выберите шаблон для удаления!")
        name = self.app_data["templates"][sel[0]]["name"]
        if messagebox.askyesno("Подтверждение", f"Удалить шаблон '{name}'?"):
            del self.app_data["templates"][sel[0]]
            save_app_data(self.app_data)
            self.refresh_all_lists()
            messagebox.showinfo("Успех", "Шаблон удален!")

    # ---------- ВСПОМОГАТЕЛЬНОЕ ----------
    def get_input_from_dialog(self, title, prompt, show=None, timeout=120):
        """
        Безопасный запрос строки у пользователя из фонового потока.
        Диалог создаётся в главном потоке через .after(). Возвращает None по таймауту.
        """
        result, event = [], threading.Event()

        def ask():
            try:
                ans = simpledialog.askstring(title, prompt, show=show, parent=self.root)
                result.append(ans)
            finally:
                event.set()

        self.root.after(0, ask)
        event.wait(timeout=timeout)
        return result[0] if result else None

    def show_thread_safe_message(self, msg_type, title, message):
        if msg_type == "error":
            messagebox.showerror(title, message)
        elif msg_type == "info":
            messagebox.showinfo(title, message)

    # ---------- ДЕЙСТВИЯ ----------
    def fetch_user_groups(self):
        if not all(self.config.get(k) for k in ["api_id", "api_hash", "phone"]):
            messagebox.showwarning("Внимание", "Настройте API ключи!");
            return self.notebook.select(0)
        try:
            self.fetch_btn.state(['disabled'])
            self.fetch_btn.config(text="⏳ Загрузка...")
        except Exception:
            pass
        threading.Thread(target=self.fetch_in_thread,
                         args=(self._fetch_groups_async, self.update_fetched_groups_list_ui, self.fetch_btn,
                               "🔄  Загрузить мои группы"), daemon=True).start()

    async def _fetch_groups_async(self, client):
        return await get_user_groups(client)

    def update_fetched_groups_list_ui(self, groups):
        self.fetched_groups_listbox.delete(0, tk.END);
        self.fetched_groups = groups
        for g in groups: self.fetched_groups_listbox.insert(tk.END, f"{g['name']} | ID: {g['id']}")
        messagebox.showinfo("Успех", f"Загружено {len(groups)} групп!");
        self.notebook.select(2)

    def add_fetched_groups(self):
        sel = self.fetched_groups_listbox.curselection()
        if not sel: return messagebox.showwarning("Внимание", "Выберите группы для добавления!")
        added = 0
        for idx in sel:
            g = self.fetched_groups[idx]
            if not any(x['id'] == g['id'] for x in self.app_data["groups"]):
                self.app_data["groups"].append({"id": g['id'], "name": g['name'], "cabinet": "", "tags": []});
                added += 1
        if added:
            save_app_data(self.app_data);
            self.refresh_all_lists();
            messagebox.showinfo("Успех", f"Добавлено {added} новых групп!");
            self.notebook.select(1)
        else:
            messagebox.showinfo("Информация", "Все выбранные группы уже есть в списке.")

    def fetch_group_topics(self):
        selected_group_name = self.topic_check_group_combo.get()
        if not selected_group_name: return messagebox.showwarning("Внимание", "Выберите группу из списка!")
        gid = self.group_name_to_id_map[selected_group_name]
        if not all(self.config.get(k) for k in ["api_id", "api_hash", "phone"]):
            messagebox.showwarning("Внимание", "Настройте API ключи!");
            return self.notebook.select(0)
        try:
            self.fetch_topics_btn.state(['disabled'])
            self.fetch_topics_btn.config(text="⏳  Поиск...")
        except Exception:
            pass
        threading.Thread(target=self.fetch_in_thread,
                         args=(lambda c: get_group_topics(c, gid), self.update_fetched_topics_list_ui,
                               self.fetch_topics_btn, "🔍  Получить темы"), daemon=True).start()

    def update_fetched_topics_list_ui(self, result):
        topics, error = result;
        self.fetched_topics_listbox.delete(0, tk.END)
        if error: return messagebox.showerror("Ошибка", error)
        self.fetched_topics = topics
        for t in topics: self.fetched_topics_listbox.insert(tk.END, f"{t['name']} | ID темы: {t['topic_id']}")
        messagebox.showinfo("Успех", f"Найдено {len(topics)} тем!")

    def add_fetched_topics(self):
        sel = self.fetched_topics_listbox.curselection();
        group_name = self.topic_check_group_combo.get()
        if not group_name: return messagebox.showwarning("Внимание", "Сначала выберите группу!")
        gid = self.group_name_to_id_map[group_name]
        if not sel: return messagebox.showwarning("Внимание", "Выберите темы для добавления!")
        added = 0
        for idx in sel:
            t = self.fetched_topics[idx]
            if not any(x['topic_id'] == t['topic_id'] and x['group_id'] == gid for x in self.app_data["themes"]):
                self.app_data["themes"].append(
                    {"group_id": gid, "topic_id": t['topic_id'], "name": t['name'], "cabinet": "", "tags": []});
                added += 1
        if added:
            save_app_data(self.app_data);
            self.refresh_all_lists();
            messagebox.showinfo("Успех", f"Добавлено {added} новых тем!");
            self.notebook.select(1)
        else:
            messagebox.showinfo("Информация", "Все выбранные темы уже есть в списке.")

    def fetch_in_thread(self, async_func, callback, btn, btn_text):
        try:
            # Запускаем singleton-воркер (если ещё не запущен)
            TG_WORKER.start(self)
            # Выполняем задачу последовательно под asyncio.Lock
            result = TG_WORKER.call(async_func)
            self.root.after(0, callback, result)
        except Exception as e:
            _logger.exception("Ошибка в фоне (fetch)")
            self.root.after(0, messagebox.showerror, "Ошибка", str(e))
        finally:
            self.root.after(0, self._restore_button, btn, btn_text)

    def _restore_button(self, btn, text):
        try:
            btn.state(['!disabled'])
            btn.config(text=text)
        except Exception:
            pass

    def load_saved_config(self):
        for key, entry in [("api_id", self.api_id_entry), ("api_hash", self.api_hash_entry),
                           ("phone", self.phone_entry), ("rate_delay", self.rate_delay_entry)]:
            if entry is None:
                continue
            value = self.config.get(key)
            if value is not None:
                entry.delete(0, tk.END)
                entry.insert(0, str(value))

    def save_settings(self):
        api_id = self.api_id_entry.get().strip();
        api_hash = self.api_hash_entry.get().strip();
        phone = self.phone_entry.get().strip()
        rate_txt = self.rate_delay_entry.get().strip() if hasattr(self, "rate_delay_entry") else "10"
        if not all([api_id, api_hash, phone]): return messagebox.showwarning("Внимание", "Заполните все поля!")
        try:
            int(api_id)
        except ValueError:
            return messagebox.showerror("Ошибка", "API ID должен быть числом!")
        if not phone.startswith("+"): return messagebox.showwarning("Внимание", "Номер телефона должен начинаться с +")
        try:
            rate_delay = float(rate_txt)
            if rate_delay < 0: raise ValueError
        except ValueError:
            return messagebox.showerror("Ошибка", "Задержка должна быть неотрицательным числом (в секундах).")
        self.config = {"api_id": api_id, "api_hash": api_hash, "phone": phone, "rate_delay": rate_delay}
        save_config(api_id, api_hash, phone, rate_delay)
        self.settings_status.config(text="✓ Настройки сохранены!", fg=self.colors['success'])

    def select_all(self):
        for var, _ in getattr(self, 'group_vars', []) + getattr(self, 'theme_vars', []): var.set(True)

    def deselect_all(self):
        for var, _ in getattr(self, 'group_vars', []) + getattr(self, 'theme_vars', []): var.set(False)

    def log(self, message):
        # Пишем в файл и в UI
        try:
            _logger.info(message)
        except Exception:
            pass
        self.root.after(0, self._log_threadsafe, message)

    def _log_threadsafe(self, message):
        self.log_text.configure(state='normal');
        self.log_text.insert(tk.END, message + "\n");
        self.log_text.see(tk.END);
        self.log_text.configure(state='disabled')

    def prepare_send(self):
        if self.is_sending: return messagebox.showwarning("Внимание", "Отправка уже выполняется!")
        if not all(self.config.get(k) for k in ["api_id", "api_hash", "phone"]):
            messagebox.showwarning("Внимание", "Настройте API ключи!");
            return self.notebook.select(0)
        selected_groups = [g for var, g in getattr(self, 'group_vars', []) if var.get()]
        selected_themes = [t for var, t in getattr(self, 'theme_vars', []) if var.get()]
        if not selected_groups and not selected_themes: return messagebox.showwarning("Внимание",
                                                                                      "Выберите получателей!")
        message = self.message_text.get("1.0", tk.END).strip()
        if not message: return messagebox.showwarning("Внимание", "Введите текст сообщения!")
        self.show_confirmation_dialog(selected_groups, selected_themes, message)

    def show_confirmation_dialog(self, selected_groups, selected_themes, message):
        dialog = tk.Toplevel(self.root);
        dialog.title("Подтверждение");
        dialog.configure(bg=self.colors['bg']);
        dialog.transient(self.root);
        dialog.grab_set()
        content = tk.Frame(dialog, bg=self.colors['bg']);
        content.pack(fill='both', expand=True, padx=20, pady=20)
        recipients_card = self.create_card(content, f"👥  Получатели ({len(selected_groups) + len(selected_themes)})");
        recipients_card.pack(fill='both', expand=True, pady=(0, 15))
        recipients_text = scrolledtext.ScrolledText(recipients_card, height=10, bg=self.colors['input_bg'],
                                                    fg=self.colors['input_fg']);
        recipients_text.pack(fill='both', expand=True)
        if selected_groups:
            recipients_text.insert(tk.END, "ГРУППЫ:\n", 'bold');
            for g in selected_groups: recipients_text.insert(tk.END, f"  • {g['name']}\n")
        if selected_themes:
            recipients_text.insert(tk.END, "\nТЕМЫ:\n", 'bold');
            for t in selected_themes: recipients_text.insert(tk.END, f"  • {t['name']}\n")
        recipients_text.tag_config('bold', font=('Segoe UI', 9, 'bold'), foreground=self.colors['primary']);
        recipients_text.config(state='disabled')
        btn_frame = tk.Frame(dialog, bg=self.colors['bg']);
        btn_frame.pack(pady=10)
        self.create_button(btn_frame, "✓  Отправить",
                           lambda: self.confirm_and_send(dialog, selected_groups, selected_themes, message),
                           variant='success').pack(side='left', padx=10)
        self.create_button(btn_frame, "✗  Отмена", dialog.destroy, variant='danger').pack(side='left', padx=10)

    def confirm_and_send(self, dialog, selected_groups, selected_themes, message):
        dialog.destroy();
        self.is_sending = True
        try:
            self.send_btn.state(['disabled'])
            self.send_btn.config(text="⏳ Идет отправка...")
        except Exception:
            pass
        self.log("🚀 Начинаю отправку...\n")
        threading.Thread(target=self.send_in_thread, args=(selected_groups, selected_themes, message),
                         daemon=True).start()

    def send_in_thread(self, selected_groups, selected_themes, message):
        try:
            self.log("🔐 Подключение к Telegram...")
            TG_WORKER.start(self)
            self.log("✓ Успешно подключено!")

            rate_delay = float(self.config.get("rate_delay", 10))

            def _send(client):
                return send_messages(client, selected_groups, selected_themes, message, self.log, rate_delay)

            success, failed = TG_WORKER.call(_send)
            self.log(f" {'=' * 30} 📊 ИТОГО: ✓ {success} | ✗ {failed} {'=' * 30} ")
        except Exception as e:
            _logger.exception("Ошибка при отправке")
            self.root.after(0, messagebox.showerror, "Ошибка", str(e))
        finally:
            self.is_sending = False
            self.root.after(0, self._restore_button, self.send_btn, "📨  Отправить сообщения")


# ============================================
# ЗАПУСК ПРИЛОЖЕНИЯ
# ============================================
if __name__ == "__main__":
    root = tk.Tk()
    app = TelegramSenderApp(root)
    root.mainloop()
