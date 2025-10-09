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

        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        window_width = int(screen_width * 0.5)
        window_height = int(screen_height * 0.5)
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")
        self.root.minsize(int(screen_width * 0.2), int(screen_height * 0.2))

        self.colors = {
            'bg': '#f8fafc', 'card': '#ffffff', 'primary': '#3b82f6', 'primary_hover': '#2563eb',
            'success': '#10b981', 'success_hover': '#059669', 'danger': '#ef4444', 'danger_hover': '#dc2626',
            'warning': '#f59e0b', 'secondary': '#6366f1', 'secondary_hover': '#4f46e5', 'text': '#0f172a',
            'text_light': '#64748b', 'text_muted': '#94a3b8', 'border': '#e2e8f0', 'border_focus': '#3b82f6',
            'input_bg': '#ffffff', 'input_fg': '#0f172a', 'tag_filter_bg': '#f1f5f9', 'hover': '#f1f5f9'
        }
        self.root.configure(bg=self.colors['bg'])
        self._setup_base_styles()

        self.is_sending = False
        self.config = load_config()
        self.app_data = load_app_data()
        self.fetched_groups = []
        self.fetched_topics = []

        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        self.create_widgets()
        self.load_saved_config()
        self.refresh_all_lists()

    def _setup_base_styles(self):
        style = ttk.Style()
        style.theme_use('clam')

        style.configure('TNotebook', background=self.colors['bg'], borderwidth=0, tabmargins=[0, 0, 0, 0])
        style.configure('TNotebook.Tab', background=self.colors['card'], foreground=self.colors['text_light'],
                        padding=[24, 14], font=('Segoe UI', 10), borderwidth=0, relief='flat')
        style.map('TNotebook.Tab', background=[('selected', self.colors['primary'])],
                  foreground=[('selected', '#ffffff')])

        style.configure('Card.TLabelframe', background=self.colors['card'], bordercolor=self.colors['border'],
                        borderwidth=1, relief='solid', padding=16)
        style.configure('Card.TLabelframe.Label', background=self.colors['card'], foreground=self.colors['text'],
                        font=('Segoe UI', 11, 'bold'))

        self._mk_button_styles()

    def _mk_button_styles(self):
        style = ttk.Style()
        base = {'font': ('Segoe UI', 10, 'bold'), 'padding': (18, 12), 'borderwidth': 0, 'relief': 'flat'}
        variants = {
            'Primary': (self.colors['primary'], self.colors['primary_hover']),
            'Success': (self.colors['success'], self.colors['success_hover']),
            'Danger': (self.colors['danger'], self.colors['danger_hover']),
            'Secondary': (self.colors['secondary'], self.colors['secondary_hover']),
        }

        for name, (color, hover) in variants.items():
            pressed = self._adjust_color(hover, 0.9)
            stylename = f'Btn.{name}.TButton'
            style.configure(stylename, background=color, foreground='#ffffff', focuscolor='none', **base)
            style.map(stylename,
                      background=[('active', hover), ('pressed', pressed), ('disabled', '#cbd5e1')],
                      foreground=[('disabled', '#ffffff')])

    def _adjust_color(self, color, factor=0.9):
        try:
            r, g, b = [int(x * factor) for x in self.root.winfo_rgb(color)[0:3:1]]
            return f'#{r // 256:02x}{g // 256:02x}{b // 256:02x}'
        except Exception:
            return color

    # UI helper: Button creation with styling
    def create_button(self, parent, text, command, variant='primary', **kwargs):
        style_map = {
            'primary': 'Btn.Primary.TButton', 'success': 'Btn.Success.TButton',
            'danger': 'Btn.Danger.TButton', 'secondary': 'Btn.Secondary.TButton',
        }
        style_name = style_map.get(str(variant).lower(), 'Btn.Primary.TButton')
        btn = ttk.Button(parent, text=text, command=command, style=style_name, **kwargs)
        btn.bind('<Enter>', lambda e: btn.configure(cursor='hand2'))
        return btn

    # UI helper: Card (LabelFrame) creation
    def create_card(self, parent, title):
        return ttk.LabelFrame(parent, text=title, style='Card.TLabelframe', padding=20)

    # UI helper: Label creation
    def mk_label(self, parent, text, bold=False, color=None):
        """
        Создает обычный текстовый ярлык, наследуя фон от родителя. Некоторые ttk-виджеты
        (например, LabelFrame) не поддерживают опцию 'bg', поэтому безопасно обрабатываем эту ситуацию.
        """
        try:
            bg = parent.cget('bg')
        except tk.TclError:
            # ttk.Frame/LabelFrame может не иметь опции bg; используем цвет карточки
            bg = self.colors.get('card', self.colors.get('bg', '#ffffff'))
        return tk.Label(
            parent,
            text=text,
            bg=bg,
            fg=color or self.colors['text'],
            font=('Segoe UI', 10, 'bold' if bold else 'normal')
        )

    def add_context_menu(self, widget):
        menu = tk.Menu(widget, tearoff=0, bg=self.colors['card'], fg=self.colors['text'],
                       activebackground=self.colors['primary'], activeforeground='#ffffff',
                       relief='flat', borderwidth=1)
        menu.add_command(label="Вырезать", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="Копировать", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Вставить", command=lambda: widget.event_generate("<<Paste>>"))

        def show_menu(e):
            menu.tk_popup(e.x_root, e.y_root)

        widget.bind("<Button-3>", show_menu)
        widget.bind("<Control-Button-1>", show_menu)

    # UI helper: Entry creation
    def mk_entry(self, parent, **kwargs):
        entry = tk.Entry(parent, font=('Segoe UI', 10), bg=self.colors['input_bg'], fg=self.colors['input_fg'],
                         relief='solid', bd=1, insertbackground=self.colors['text'], highlightthickness=2,
                         highlightbackground=self.colors['border'], highlightcolor=self.colors['border_focus'],
                         **kwargs)
        entry.bind('<FocusIn>', lambda e: entry.config(highlightbackground=self.colors['border_focus']))
        entry.bind('<FocusOut>', lambda e: entry.config(highlightbackground=self.colors['border']))
        self.add_context_menu(entry)
        return entry

    # UI helper: Text widget creation
    def mk_text(self, parent, **kwargs):
        frame = tk.Frame(parent, bg=self.colors['border'], bd=1, relief='solid')
        txt = scrolledtext.ScrolledText(frame, wrap=tk.WORD, font=('Segoe UI', 10),
                                        bg=self.colors['input_bg'], fg=self.colors['input_fg'], relief='flat', bd=0,
                                        insertbackground=self.colors['text'], highlightthickness=0, **kwargs)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        txt.grid(row=0, column=0, sticky="nsew")
        self.add_context_menu(txt)
        return frame, txt

    def mk_checkbutton(self, parent, text, var):
        bg = parent.cget('bg')
        cb = tk.Checkbutton(parent, text=text, variable=var, bg=bg, fg=self.colors['text'],
                            selectcolor=self.colors['input_bg'], activebackground=bg,
                            activeforeground=self.colors['text'], font=('Segoe UI', 9),
                            relief='flat', borderwidth=0, highlightthickness=0, cursor='hand2')
        cb.bind('<Enter>', lambda e: cb.config(fg=self.colors['primary']))
        cb.bind('<Leave>', lambda e: cb.config(fg=self.colors['text']))
        return cb

    def mk_listbox(self, parent):
        frame = tk.Frame(parent, bg=self.colors['card'])
        listbox = tk.Listbox(frame, font=('Segoe UI', 9), bg=self.colors['input_bg'], fg=self.colors['input_fg'],
                             relief='solid', bd=1, selectbackground=self.colors['primary'],
                             selectforeground='#ffffff', activestyle='none', exportselection=False,
                             highlightthickness=1, highlightbackground=self.colors['border'],
                             highlightcolor=self.colors['border_focus'])
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=listbox.yview)
        listbox.config(yscrollcommand=scrollbar.set)

        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        listbox.grid(row=0, column=0, sticky='nsew')
        scrollbar.grid(row=0, column=1, sticky='ns')
        return frame, listbox

    def _create_scrollable_area(self, parent):
        container = tk.Frame(parent, bg=self.colors['bg'])
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(container, bg=self.colors['bg'], highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=self.colors['bg'])


        canvas_window = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        def on_canvas_configure(event):
            canvas.itemconfig(canvas_window, width=event.width)
        canvas.bind('<Configure>', on_canvas_configure)

        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky='nsew')
        scrollbar.grid(row=0, column=1, sticky='nsew')

        return container, scrollable_frame

    def create_widgets(self):
        main_container = tk.Frame(self.root, bg=self.colors['bg'])
        main_container.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        main_container.grid_rowconfigure(0, weight=1)
        main_container.grid_columnconfigure(0, weight=1)

        self.notebook = ttk.Notebook(main_container)
        self.notebook.grid(row=0, column=0, sticky="nsew")

        tabs = {
            "  ⚙️  Настройки  ": self.create_settings_tab,
            "  📋  Управление списками  ": self.create_manage_tab,
            "  📥  Получить группы и темы  ": self.create_fetch_tab,
            "  📤  Отправка сообщений  ": self.create_sending_tab
        }

        for text, creator in tabs.items():
            tab_frame = tk.Frame(self.notebook, bg=self.colors['bg'])
            self.notebook.add(tab_frame, text=text)
            creator(tab_frame)

    # ============================================
    # UI FUNCTIONS
    # ============================================
    # -- Settings Page --
    def create_settings_tab(self, parent):

        # Настройка контейнера и создание прокручиваемой области

        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        container, scrollable_frame = self._create_scrollable_area(parent)
        container.grid(row=0, column=0, sticky="nsew")

        scrollable_frame.grid_columnconfigure(0, weight=1)
        scrollable_frame.grid_columnconfigure(1, weight=8)
        scrollable_frame.grid_columnconfigure(2, weight=1)
        scrollable_frame.grid_rowconfigure(0, weight=1)

        # Карточка настроек Telegram API располагается в центральной колонке (80% ширины)
        card = self.create_card(scrollable_frame, "🔐  Настройки Telegram API")
        card.grid(row=0, column=1, sticky='nsew', pady=(20, 12))
        card.columnconfigure(1, weight=1)

        def field(label, row, show=None):
            """Создать подпись и поле ввода."""
            self.mk_label(card, label, bold=True).grid(row=row, column=0, sticky='nsew', pady=6, padx=(0, 10))
            entry = self.mk_entry(card, show=show)
            entry.grid(row=row, column=1, sticky='nsew', pady=6)
            return entry

        # Поля ввода
        self.api_id_entry = field("API ID:", 0)
        self.api_hash_entry = field("API Hash:", 1)
        self.phone_entry = field("Телефон:", 2)
        self.rate_delay_entry = field("Задержка (сек):", 3)

        # Подсказка
        tk.Label(card, text="💡 Получите API ключи на my.telegram.org/apps",
                 bg=self.colors['card'], fg=self.colors['text_muted'],
                 font=('Segoe UI', 9, 'italic')).grid(row=4, column=0, columnspan=2, pady=(8, 12), sticky='nsew')

        # Кнопка сохранения
        self.create_button(card, "💾  Сохранить настройки", self.save_settings,
                           variant='success').grid(row=5, column=0, columnspan=2, pady=10, sticky='nsew')

        # Строка статуса
        self.settings_status = tk.Label(card, text="", bg=self.colors['card'], fg=self.colors['success'],
                                        font=('Segoe UI', 10))
        self.settings_status.grid(row=6, column=0, columnspan=2, pady=(6, 10), sticky='nsew')

    # -- Manage Page: UI --
    def create_manage_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_columnconfigure(1, weight=2)
        parent.grid_columnconfigure(2, weight=2)
        parent.grid_rowconfigure(0, weight=1)

        self.create_tags_manager(parent).grid(row=0, column=0, sticky='nsew', padx=(0, 8), pady=16)
        self.create_groups_manager(parent).grid(row=0, column=1, sticky='nsew', padx=8, pady=16)
        self.create_themes_manager(parent).grid(row=0, column=2, sticky='nsew', padx=(8, 0), pady=16)

    # Manage: UI component for tags
    def create_tags_manager(self, parent):
        card = self.create_card(parent, "🏷️  Управление тегами")
        card.rowconfigure(0, weight=1)
        card.columnconfigure(0, weight=1)

        list_frame, self.tags_listbox = self.mk_listbox(card)
        list_frame.grid(row=0, column=0, sticky='nsew', pady=(0, 15))

        form = tk.Frame(card, bg=self.colors['card'])
        form.grid(row=1, column=0, sticky='ew', pady=8)
        form.columnconfigure(1, weight=1)
        self.mk_label(form, "Новый тег:", bold=True).grid(row=0, column=0, padx=(0, 10))
        self.tag_name_entry = self.mk_entry(form)
        self.tag_name_entry.grid(row=0, column=1, sticky='ew')

        btns = tk.Frame(card, bg=self.colors['card'])
        btns.grid(row=2, column=0, sticky='ew', pady=12)
        btns.columnconfigure((0, 1), weight=1)
        self.create_button(btns, "➕", self.add_tag, variant='success').grid(row=0, column=0, sticky='ew', padx=(0, 3))
        self.create_button(btns, "✖", self.delete_tag, variant='danger').grid(row=0, column=1, sticky='ew', padx=(3, 0))
        return card

    # Manage: UI component for groups
    def create_groups_manager(self, parent):
        card = self.create_card(parent, "📁  Управление группами")
        card.rowconfigure(0, weight=1)
        card.columnconfigure(0, weight=1)

        list_frame, self.groups_listbox = self.mk_listbox(card)
        self.groups_listbox.config(selectmode='extended')
        list_frame.grid(row=0, column=0, sticky='nsew', pady=(0, 15))

        form = tk.Frame(card, bg=self.colors['card'])
        form.grid(row=1, column=0, sticky='ew', pady=12)
        form.columnconfigure(1, weight=1)

        fields = [("ID:", "group_id_entry"), ("Название TG:", "group_name_entry"),
                  ("Номер клиента:", "group_client_entry")]
        for i, (text, name) in enumerate(fields):
            self.mk_label(form, text, bold=True).grid(row=i, column=0, sticky='w', pady=6, padx=(0, 10))
            entry = self.mk_entry(form)
            entry.grid(row=i, column=1, sticky='ew', pady=6)
            setattr(self, name, entry)

        btns = tk.Frame(card, bg=self.colors['card'])
        btns.grid(row=2, column=0, sticky='ew', pady=12)
        btns.columnconfigure((0, 1, 2, 3), weight=1)
        self.create_button(btns, "➕", self.add_group, variant='success').grid(row=0, column=0, sticky='ew', padx=2)
        self.create_button(btns, "✏️", lambda: self.edit_item('group'), variant='primary').grid(row=0, column=1,
                                                                                                sticky='ew', padx=2)
        self.create_button(btns, "🛠", lambda: self._edit_item_template_dialog('group'), variant='secondary').grid(row=0,
                                                                                                                  column=2,
                                                                                                                  sticky='ew',
                                                                                                                  padx=2)
        self.create_button(btns, "✖", self.delete_group, variant='danger').grid(row=0, column=3, sticky='ew', padx=2)
        return card

    # Manage: UI component for themes
    def create_themes_manager(self, parent):
        card = self.create_card(parent, "🧵  Управление темами")
        card.rowconfigure(0, weight=1)
        card.columnconfigure(0, weight=1)

        list_frame, self.themes_listbox = self.mk_listbox(card)
        self.themes_listbox.config(selectmode='extended')
        list_frame.grid(row=0, column=0, sticky='nsew', pady=(0, 15))

        form = tk.Frame(card, bg=self.colors['card'])
        form.grid(row=1, column=0, sticky='ew', pady=12)
        form.columnconfigure(1, weight=1)

        fields = [("ID группы:", "theme_group_id_entry"), ("ID темы:", "theme_topic_id_entry"),
                  ("Название:", "theme_name_entry"), ("Номер клиента:", "theme_client_entry")]
        for i, (text, name) in enumerate(fields):
            self.mk_label(form, text, bold=True).grid(row=i, column=0, sticky='w', pady=6, padx=(0, 10))
            entry = self.mk_entry(form)
            entry.grid(row=i, column=1, sticky='ew', pady=6)
            setattr(self, name, entry)

        btns = tk.Frame(card, bg=self.colors['card'])
        btns.grid(row=2, column=0, sticky='ew', pady=12)
        btns.columnconfigure((0, 1, 2, 3), weight=1)
        self.create_button(btns, "➕", self.add_theme, variant='success').grid(row=0, column=0, sticky='ew', padx=2)
        self.create_button(btns, "✏️", lambda: self.edit_item('theme'), variant='primary').grid(row=0, column=1,
                                                                                                sticky='ew', padx=2)
        self.create_button(btns, "🛠", lambda: self._edit_item_template_dialog('theme'), variant='secondary').grid(row=0,
                                                                                                                  column=2,
                                                                                                                  sticky='ew',
                                                                                                                  padx=2)
        self.create_button(btns, "✖", self.delete_theme, variant='danger').grid(row=0, column=3, sticky='ew', padx=2)
        return card

    # -- Fetch Page: UI --
    def create_fetch_tab(self, parent):
        """
        Создает вкладку для загрузки групп и тем. Элементы располагаются горизонтально и занимают по 50% ширины
        экрана, обеспечивая удобное сравнение и доступ к обеим карточкам одновременно.
        """
        # базовая конфигурация и прокручиваемая область
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        container, scrollable_frame = self._create_scrollable_area(parent)
        container.grid(row=0, column=0, sticky='nsew')

        # контейнер для карточек, две колонки по 50%
        content_container = tk.Frame(scrollable_frame, bg=self.colors['bg'])
        content_container.grid(row=0, column=0, sticky='nsew', padx=20, pady=20)
        content_container.grid_columnconfigure(0, weight=1)
        content_container.grid_columnconfigure(1, weight=1)
        content_container.grid_rowconfigure(0, weight=1)

        # --- Карточка: получение списка групп ---
        groups_card = self.create_card(content_container, "📥  Получить список всех групп")
        groups_card.grid(row=0, column=0, sticky='nsew', padx=(0, 10))
        groups_card.columnconfigure(0, weight=1)
        groups_card.rowconfigure(2, weight=1)

        tk.Label(groups_card, text="Загрузите список ваших Telegram групп и каналов",
                 bg=self.colors['card'], fg=self.colors['text_muted'], font=('Segoe UI', 9, 'italic')).grid(
            row=0, column=0, pady=(0, 15), sticky='nsew')
        self.fetch_btn = self.create_button(groups_card, "🔄  Загрузить мои группы", self.fetch_user_groups,
                                            variant='primary')
        self.fetch_btn.grid(row=1, column=0, pady=20, sticky='nsew')

        list_frame_g, self.fetched_groups_listbox = self.mk_listbox(groups_card)
        self.fetched_groups_listbox.config(selectmode='extended')
        list_frame_g.grid(row=2, column=0, sticky='nsew')

        tk.Label(groups_card, text="💡 Выберите несколько групп (Shift/Ctrl)",
                 bg=self.colors['card'], fg=self.colors['text_muted'], font=('Segoe UI', 9)).grid(
            row=3, column=0, pady=(5, 15), sticky='nsew')
        self.create_button(groups_card, "➕  Добавить выбранные", self.add_fetched_groups,
                           variant='success').grid(row=4, column=0, pady=15, sticky='nsew')

        # --- Карточка: поиск тем в группах ---
        topics_card = self.create_card(content_container, "🔎  Получить темы из групп-форумов")
        topics_card.grid(row=0, column=1, sticky='nsew', padx=(10, 0))
        topics_card.columnconfigure(0, weight=1)
        topics_card.rowconfigure(2, weight=1)

        tk.Label(topics_card, text="Найдите доступные темы во всех группах-форумах",
                 bg=self.colors['card'], fg=self.colors['text_muted'], font=('Segoe UI', 9, 'italic')).grid(
            row=0, column=0, pady=(0, 15), sticky='nsew')
        self.fetch_topics_btn = self.create_button(topics_card, "🔍  Найти темы", self.fetch_all_group_topics,
                                                   variant='primary')
        self.fetch_topics_btn.grid(row=1, column=0, pady=15, sticky='nsew')

        list_frame_t, self.fetched_topics_listbox = self.mk_listbox(topics_card)
        self.fetched_topics_listbox.config(selectmode='extended')
        list_frame_t.grid(row=2, column=0, sticky='nsew')

        tk.Label(topics_card, text="💡 Выберите темы для добавления",
                 bg=self.colors['card'], fg=self.colors['text_muted'], font=('Segoe UI', 9)).grid(
            row=3, column=0, pady=(5, 15), sticky='nsew')
        self.create_button(topics_card, "➕  Добавить выбранные темы", self.add_fetched_topics,
                           variant='success').grid(row=4, column=0, pady=15, sticky='nsew')

    # -- Sending Page: UI --
    def create_sending_tab(self, parent):
        """
        Создает вкладку отправки сообщений. Размещает все элементы в одной вертикальной колонке с прокруткой,
        чтобы содержимое адаптировалось к изменению размеров окна и занимало всю доступную ширину.
        """
        # базовая конфигурация и прокручиваемая область
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        container, scrollable_frame = self._create_scrollable_area(parent)
        container.grid(row=0, column=0, sticky='nsew')

        # Для корректного растяжения по вертикали задаем вес строке 0 в scrollable_frame
        scrollable_frame.grid_rowconfigure(0, weight=1)
        scrollable_frame.grid_columnconfigure(0, weight=1)

        # --- Двухколоночный макет: слева выбор получателей, справа подготовка сообщения ---
        split_container = tk.Frame(scrollable_frame, bg=self.colors['bg'])
        split_container.grid(row=0, column=0, sticky='nsew', padx=0, pady=0)
        # соотношение 3:7 соответствует ~30% и ~70%
        split_container.grid_columnconfigure(0, weight=3)
        split_container.grid_columnconfigure(1, weight=7)
        split_container.grid_rowconfigure(0, weight=1)

        # === Левая колонка: выбор получателей (теги, группы, темы) ===
        left_frame = tk.Frame(split_container, bg=self.colors['bg'])
        # не задаем вес строке, чтобы высота зависела от содержимого (глобальная прокрутка решает переполнение)
        left_frame.grid(row=0, column=0, sticky='nsew', padx=(0, 8), pady=16)
        left_frame.grid_columnconfigure(0, weight=1)

        # карточка выбора получателей
        self.lists_card_sending = self.create_card(left_frame, "📋  Выбор получателей")
        self.lists_card_sending.grid(row=0, column=0, sticky='nsew')
        # строим теги/группы/темы внутри карточки
        self.build_sending_lists(self.lists_card_sending)

        # === Правая колонка: подготовка сообщения ===
        right_frame = tk.Frame(split_container, bg=self.colors['bg'])
        right_frame.grid(row=0, column=1, sticky='nsew', padx=(8, 0), pady=16)
        right_frame.grid_columnconfigure(0, weight=1)
        # строки для текста сообщения и лога растягиваются
        right_frame.grid_rowconfigure(1, weight=1)
        right_frame.grid_rowconfigure(5, weight=1)

        # --- Шаблоны сообщений ---
        templates_card = self.create_card(right_frame, "📝  Шаблоны сообщений")
        templates_card.grid(row=0, column=0, sticky='ew', pady=(0, 12))
        self.create_templates_manager(templates_card)

        # --- Текст сообщения ---
        msg_card = self.create_card(right_frame, "✉️  Текст сообщения")
        msg_card.grid(row=1, column=0, sticky='nsew', pady=(0, 12))
        msg_card.columnconfigure(0, weight=1)
        msg_card.rowconfigure(2, weight=1)

        char_counter_frame = tk.Frame(msg_card, bg=self.colors['card'])
        char_counter_frame.grid(row=0, column=0, sticky='ew', pady=(0, 5))
        self.char_counter = tk.Label(char_counter_frame, text="Символов: 0",
                                     bg=self.colors['card'], fg=self.colors['text_muted'],
                                     font=('Segoe UI', 9))
        self.char_counter.grid(row=0, column=0, sticky='e')

        self.var_buttons_frame = tk.Frame(msg_card, bg=self.colors['card'])
        self.var_buttons_frame.grid(row=1, column=0, sticky='w', pady=(0, 5))

        text_frame, self.message_text = self.mk_text(msg_card)
        text_frame.grid(row=2, column=0, sticky='nsew')
        self.message_text.bind('<KeyRelease>', self.update_char_counter)

        # --- Параметры ---
        params_card = self.create_card(right_frame, "🔧  Параметры")
        params_card.grid(row=2, column=0, sticky='ew', pady=(0, 12))
        self.parameters = [{'name_var': tk.StringVar(value=str(i)), 'value_var': tk.StringVar()}
                           for i in range(1, 5)]
        self.param_frame = params_card
        self.build_params_section(self.param_frame)

        # --- Вложения ---
        attachments_card = self.create_card(right_frame, "📎  Вложения")
        attachments_card.grid(row=3, column=0, sticky='ew', pady=(0, 12))
        attachments_card.columnconfigure(0, weight=1)
        attachments_card.rowconfigure(1, weight=1)

        attach_btn_frame = tk.Frame(attachments_card, bg=self.colors['card'])
        attach_btn_frame.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        attach_btn_frame.columnconfigure((0, 1), weight=1)
        self.create_button(attach_btn_frame, "📂  Добавить файлы", self.add_attachments,
                           variant='primary').grid(row=0, column=0, sticky='ew', padx=(0, 4))
        self.create_button(attach_btn_frame, "✖  Удалить выбранные", self.remove_attachments,
                           variant='danger').grid(row=0, column=1, sticky='ew', padx=(4, 0))

        list_frame, self.attachments_listbox = self.mk_listbox(attachments_card)
        list_frame.grid(row=1, column=0, sticky='nsew')
        self.attachments_listbox.config(selectmode='extended')
        self.attachments = []

        # --- Кнопка отправки ---
        self.send_btn = self.create_button(right_frame, "📨  Отправить сообщения", self.prepare_send,
                                           variant='success')
        self.send_btn.grid(row=4, column=0, pady=12, sticky='ew')

        # --- Лог отправки ---
        progress_card = self.create_card(right_frame, "📊  Лог отправки")
        progress_card.grid(row=5, column=0, sticky='nsew', pady=(0, 16))
        progress_card.columnconfigure(0, weight=1)
        progress_card.rowconfigure(0, weight=1)

        log_frame, self.log_text = self.mk_text(progress_card)
        self.log_text.config(state='disabled', font=('Consolas', 9))
        log_frame.grid(row=0, column=0, sticky='nsew')

    def update_char_counter(self, event=None):
        count = len(self.message_text.get("1.0", tk.END).strip())
        self.char_counter.config(text=f"Символов: {count}")

    def insert_message_var(self, var):
        self.message_text.insert(tk.INSERT, var)
        self.message_text.focus_set()
        self.update_char_counter()

    # Sending: Builds the parameters section UI
    def build_params_section(self, parent):
        for w in parent.winfo_children():
            if w.winfo_class() != 'Labelframe': w.destroy()

        rows_frame = tk.Frame(parent, bg=self.colors['card'])
        rows_frame.grid(row=0, column=0, sticky='ew')
        parent.columnconfigure(0, weight=1)

        for idx, param in enumerate(self.parameters):
            row = tk.Frame(rows_frame, bg=self.colors['card'])
            row.grid(row=idx, column=0, sticky='ew', pady=4, padx=4)
            row.columnconfigure(1, weight=1)

            name_entry = self.mk_entry(row, textvariable=param['name_var'])
            name_entry.grid(row=0, column=0, padx=(0, 4))

            value_entry = self.mk_entry(row, textvariable=param['value_var'])
            value_entry.grid(row=0, column=1, padx=(0, 4), sticky='ew')

            placeholder_button = self.create_button(row, f"[{param['name_var'].get()}]",
                                                    lambda p=param: self.insert_message_var(f"[{p['name_var'].get()}]"),
                                                    variant='secondary')
            placeholder_button.grid(row=0, column=2, padx=(0, 4))

            if len(self.parameters) > 1:
                remove_btn = self.create_button(row, "✖", lambda i=idx: self.remove_parameter(i), variant='danger')
                remove_btn.grid(row=0, column=3)

            def on_name_change(*_args, var=param['name_var'], btn=placeholder_button):
                new_text = f"[{var.get()}]"
                btn.config(text=new_text)
                btn.configure(command=lambda v=var: self.insert_message_var(f"[{v.get()}]"))
                self.refresh_var_buttons()

            param['name_var'].trace_add('write', on_name_change)

        add_btn = self.create_button(parent, "➕ Добавить параметр", self.add_parameter, variant='secondary')
        add_btn.grid(row=1, column=0, pady=8, padx=4, sticky='w')
        self.refresh_var_buttons()

    def add_parameter(self):
        existing = {p['name_var'].get() for p in self.parameters}
        name = f"param{len(existing) + 1}"
        while name in existing:
            name = f"{name}_"
        self.parameters.append({'name_var': tk.StringVar(value=name), 'value_var': tk.StringVar()})
        self.build_params_section(self.param_frame)

    def remove_parameter(self, index):
        if 0 <= index < len(self.parameters):
            del self.parameters[index]
            self.build_params_section(self.param_frame)

    def refresh_var_buttons(self):
        for widget in self.var_buttons_frame.winfo_children():
            widget.destroy()
        for i, param in enumerate(self.parameters):
            name = param['name_var'].get()
            placeholder = f"[{name}]"
            btn = self.create_button(self.var_buttons_frame, placeholder,
                                     lambda p=placeholder: self.insert_message_var(p), variant='secondary')
            btn.grid(row=0, column=i, padx=4)

    def replace_vars(self, text: str) -> str:
        for param in self.parameters:
            name = param['name_var'].get()
            value = param['value_var'].get()
            text = text.replace(f"[{name}]", value)
        return text

    def add_attachments(self):
        paths = filedialog.askopenfilenames(title="Выберите файлы для прикрепления")
        if not paths: return
        for p in paths:
            if p and p not in self.attachments:
                self.attachments.append(p)
                self.attachments_listbox.insert(tk.END, os.path.basename(p))

    def remove_attachments(self):
        for idx in sorted(self.attachments_listbox.curselection(), reverse=True):
            self.attachments_listbox.delete(idx)
            self.attachments.pop(idx)

    # Sending: UI component for managing templates
    def create_templates_manager(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

        list_frame, self.templates_listbox = self.mk_listbox(parent)
        list_frame.grid(row=0, column=0, sticky='nsew', pady=(0, 15))

        btns = tk.Frame(parent, bg=self.colors['card'])
        btns.grid(row=1, column=0, sticky='ew', pady=12)
        btns.columnconfigure((0, 1, 2), weight=1)

        self.create_button(btns, "📥", self.use_template, variant='primary').grid(row=0, column=0, sticky='ew', padx=3)
        self.create_button(btns, "💾", self.save_template, variant='success').grid(row=0, column=1, sticky='ew', padx=3)
        self.create_button(btns, "✖", self.delete_template, variant='danger').grid(row=0, column=2, sticky='ew', padx=3)

    # Sending: Builds the tag filter and group/theme selection UI
    def build_sending_lists(self, parent):
        for w in parent.winfo_children(): w.destroy()
        parent.grid_rowconfigure(1, weight=0)
        parent.columnconfigure(0, weight=1)

        tags_card = self.create_card(parent, "🏷️  Фильтр по тегам")
        tags_card.grid(row=0, column=0, sticky='ew', pady=(0, 12))
        tags_card.columnconfigure(0, weight=1)
        tags_outer = tk.Frame(tags_card, bg=self.colors['tag_filter_bg'], relief='flat')
        tags_outer.grid(row=0, column=0, sticky='ew', pady=8, padx=0)
        tags_outer.columnconfigure(0, weight=1)
        tags_frame = tk.Frame(tags_outer, bg=self.colors['tag_filter_bg'])
        tags_frame.grid(row=0, column=0, sticky='w', padx=12, pady=12)

        self.tag_filter_vars = []
        self.all_tags_var = tk.BooleanVar(value=True)

        def toggle_all_tags():
            for var, _ in self.tag_filter_vars: var.set(self.all_tags_var.get())
            self.filter_sending_lists()

        def update_all_tags_state():
            self.all_tags_var.set(all(var.get() for var, _ in self.tag_filter_vars))
            self.filter_sending_lists()

        all_cb = self.mk_checkbutton(tags_frame, "Все", self.all_tags_var)
        all_cb.config(command=toggle_all_tags, font=('Segoe UI', 10, 'bold'), fg=self.colors['primary'],
                      activeforeground=self.colors['primary'])
        all_cb.grid(row=0, column=0, padx=8)

        for i, tag in enumerate(self.app_data["tags"]):
            var = tk.BooleanVar(value=True)
            self.tag_filter_vars.append((var, tag))
            cb = self.mk_checkbutton(tags_frame, tag, var)
            cb.config(command=update_all_tags_state)
            cb.grid(row=0, column=i + 1, padx=8)

        lists_container = tk.Frame(parent, bg=self.colors['bg'])
        lists_container.grid(row=1, column=0, sticky='nsew', pady=(0, 12))
        lists_container.grid_columnconfigure((0, 1), weight=1)
        lists_container.grid_rowconfigure(0, weight=0)

        self.groups_card_sending = self.create_card(lists_container, "📁  Выбор групп")
        self.themes_card_sending = self.create_card(lists_container, "🧵  Выбор тем")
        self.groups_card_sending.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        self.themes_card_sending.grid(row=0, column=1, sticky='nsew', padx=(8, 0))

        btn_select = tk.Frame(parent, bg=self.colors['bg'])
        btn_select.grid(row=2, column=0, sticky='ew', pady=12)
        btn_select.columnconfigure((0, 1), weight=1)
        self.create_button(btn_select, "✓  Выбрать все", self.select_all, variant='primary').grid(row=0, column=0,
                                                                                                  sticky='ew',
                                                                                                  padx=(0, 5))
        self.create_button(btn_select, "✗  Снять выбор", self.deselect_all, variant='secondary').grid(row=0, column=1,
                                                                                                      sticky='ew',
                                                                                                      padx=(5, 0))

        self.filter_sending_lists()

    def filter_sending_lists(self):
        active_tags = {tag for var, tag in getattr(self, 'tag_filter_vars', []) if var.get()}

        def populate(card, items, is_group):
            for widget in card.winfo_children():
                widget.destroy()

            card.grid_rowconfigure(0, weight=0)
            card.grid_columnconfigure(0, weight=1)
            container, scrollable_area = self._create_scrollable_area(card)
            container.grid(row=0, column=0, sticky='nsew')

            item_vars = []
            for i, item in enumerate(items):
                if not active_tags or set(item.get('tags', [])).intersection(active_tags):
                    var = tk.BooleanVar()
                    item_vars.append((var, item))
                    label = f"{item['name']} - Клиент: {item.get('client_number', 'N/A')}"
                    cb = self.mk_checkbutton(scrollable_area, label, var)
                    cb.grid(row=i, column=0, sticky='w', pady=2, padx=8)

            if not item_vars:
                self.mk_label(scrollable_area, "Нет элементов", color=self.colors['text_muted']).grid(row=0, column=0,
                                                                                                      pady=20, padx=20)

            if is_group:
                self.group_vars = item_vars
            else:
                self.theme_vars = item_vars

        populate(self.groups_card_sending, self.app_data["groups"], True)
        populate(self.themes_card_sending, self.app_data["themes"], False)

    def refresh_all_lists(self):
        for listbox, items, formatter in [
            (self.tags_listbox, self.app_data["tags"], lambda t: t),
            (self.groups_listbox, self.app_data["groups"], lambda g: f"{g['name']} | ID: {g['id']}"),
            (self.themes_listbox, self.app_data["themes"], lambda t: f"{t['name']} | Группа: {t['group_id']}"),
            (self.templates_listbox, self.app_data["templates"], lambda t: t["name"])
        ]:
            listbox.delete(0, tk.END)
            for item in items:
                listbox.insert(tk.END, formatter(item))

        # если уже создан раздел с получателями, обновляем его
        if hasattr(self, 'lists_card_sending'):
            self.build_sending_lists(self.lists_card_sending)

    # -- Manage Page Logic --
    def add_tag(self):
        tag_name = self.tag_name_entry.get().strip()
        if not tag_name: return messagebox.showwarning("Внимание", "Название тега не может быть пустым!")
        if tag_name in self.app_data["tags"]: return messagebox.showwarning("Внимание", "Такой тег уже существует.")
        self.app_data["tags"].append(tag_name)
        save_app_data(self.app_data)
        self.refresh_all_lists()
        self.tag_name_entry.delete(0, tk.END)
        messagebox.showinfo("Успех", "Тег добавлен!")

    def delete_tag(self):
        sel = self.tags_listbox.curselection()
        if not sel: return messagebox.showwarning("Внимание", "Выберите тег для удаления!")
        tag_to_delete = self.app_data["tags"][sel[0]]
        if messagebox.askyesno("Подтверждение",
                               f"Удалить тег '{tag_to_delete}'? Он также будет удален из всех групп и тем."):
            del self.app_data["tags"][sel[0]]
            for item in self.app_data["groups"] + self.app_data["themes"]:
                if "tags" in item and tag_to_delete in item["tags"]:
                    item["tags"].remove(tag_to_delete)
            save_app_data(self.app_data)
            self.refresh_all_lists()
            messagebox.showinfo("Успех", "Тег удален!")

    def add_group(self):
        try:
            gid, name = int(self.group_id_entry.get()), self.group_name_entry.get().strip()
            client_num = self.group_client_entry.get().strip()
            if not name: return messagebox.showwarning("Внимание", "Укажите название группы!")

            tag_input = simpledialog.askstring("Назначить теги", f"Введите теги для группы '{name}' через запятую:",
                                               parent=self.root)
            tags = [t.strip() for t in tag_input.split(',') if t.strip()] if tag_input else []
            for t in tags:
                if t not in self.app_data["tags"]: self.app_data["tags"].append(t)

            self.app_data["groups"].append(
                {"id": gid, "name": name, "client_number": client_num, "tags": tags, "custom_templates": {}})
            save_app_data(self.app_data)
            self.refresh_all_lists()
            for e in [self.group_id_entry, self.group_name_entry, self.group_client_entry]: e.delete(0, tk.END)
            messagebox.showinfo("Успех", "Группа добавлена!")
        except ValueError:
            messagebox.showerror("Ошибка", "ID группы должен быть числом!")

    def delete_group(self):
        sel = self.groups_listbox.curselection()
        if not sel: return messagebox.showwarning("Внимание", "Выберите группу для удаления!")
        if messagebox.askyesno("Подтверждение", "Удалить выбранную группу?"):
            del self.app_data["groups"][sel[0]]
            save_app_data(self.app_data)
            self.refresh_all_lists()
            messagebox.showinfo("Успех", "Группа удалена!")

    def add_theme(self):
        try:
            gid, tid = int(self.theme_group_id_entry.get()), int(self.theme_topic_id_entry.get())
            name, client_num = self.theme_name_entry.get().strip(), self.theme_client_entry.get().strip()
            if not name: return messagebox.showwarning("Внимание", "Укажите название темы!")

            tag_input = simpledialog.askstring("Назначить теги", f"Введите теги для темы '{name}' через запятую:",
                                               parent=self.root)
            tags = [t.strip() for t in tag_input.split(',') if t.strip()] if tag_input else []
            for t in tags:
                if t not in self.app_data["tags"]: self.app_data["tags"].append(t)

            self.app_data["themes"].append(
                {"group_id": gid, "topic_id": tid, "name": name, "client_number": client_num, "tags": tags,
                 "custom_templates": {}})
            save_app_data(self.app_data)
            self.refresh_all_lists()
            for e in [self.theme_group_id_entry, self.theme_topic_id_entry, self.theme_name_entry,
                      self.theme_client_entry]: e.delete(0, tk.END)
            messagebox.showinfo("Успех", "Тема добавлена!")
        except ValueError:
            messagebox.showerror("Ошибка", "ID группы и темы должны быть числами!")

    def delete_theme(self):
        sel = self.themes_listbox.curselection()
        if not sel: return messagebox.showwarning("Внимание", "Выберите тему для удаления!")
        if messagebox.askyesno("Подтверждение", "Удалить выбранную тему?"):
            del self.app_data["themes"][sel[0]]
            save_app_data(self.app_data)
            self.refresh_all_lists()
            messagebox.showinfo("Успех", "Тема удалена!")

    def edit_item(self, item_type):
        listbox = self.groups_listbox if item_type == 'group' else self.themes_listbox
        data_list = self.app_data["groups"] if item_type == 'group' else self.app_data["themes"]
        sel = listbox.curselection()
        if not sel: return messagebox.showwarning("Внимание",
                                                  f"Выберите {'группу' if item_type == 'group' else 'тему'} для редактирования!")
        item = data_list[sel[0]]

        dialog = tk.Toplevel(self.root)
        dialog.title(f"Редактирование {item_type}")
        dialog.configure(bg=self.colors['bg'])
        dialog.transient(self.root);
        dialog.grab_set()
        width, height = 480, 600
        x, y = (self.root.winfo_screenwidth() - width) // 2, (self.root.winfo_screenheight() - height) // 2
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(3, weight=1)

        tk.Label(dialog, text=f"Редактирование: {item['name']}", bg=self.colors['bg'],
                 font=('Segoe UI', 12, 'bold')).grid(row=0, column=0, pady=(20, 10))

        form_frame = tk.Frame(dialog, bg=self.colors['card'], relief='solid', bd=1, highlightthickness=1)
        form_frame.grid(row=1, column=0, sticky='ew', padx=20, pady=(0, 10))
        form_frame.columnconfigure(1, weight=1)

        def create_field(parent, label_text, initial, row_num):
            self.mk_label(parent, label_text, bold=True).grid(row=row_num, column=0, sticky='w', padx=10, pady=6)
            ent = self.mk_entry(parent);
            ent.insert(0, initial)
            ent.grid(row=row_num, column=1, sticky='ew', padx=10, pady=6)
            return ent

        name_entry = create_field(form_frame, "Название:", item['name'], 0)
        client_entry = create_field(form_frame, "Номер клиента:", item['client_number'], 1)

        tk.Label(dialog, text="Теги:", bg=self.colors['bg'], font=('Segoe UI', 10, 'bold')).grid(row=2, column=0,
                                                                                                 sticky='w', padx=20,
                                                                                                 pady=(10, 0))

        container, tag_scrollable_area = self._create_scrollable_area(dialog)
        container.grid(row=3, column=0, sticky='nsew', padx=20, pady=10)

        vars_map, current_tags = {}, set(item.get('tags', []))
        for i, tag in enumerate(self.app_data["tags"]):
            var = tk.BooleanVar(value=(tag in current_tags))
            vars_map[tag] = var
            self.mk_checkbutton(tag_scrollable_area, tag, var).grid(row=i, column=0, sticky='w', padx=10, pady=5)

        def save_changes():
            if not name_entry.get().strip(): return messagebox.showwarning("Внимание", "Название не может быть пустым!")
            item['name'] = name_entry.get().strip()
            item['client_number'] = client_entry.get().strip()
            item['tags'] = [t for t, v in vars_map.items() if v.get()]
            save_app_data(self.app_data)
            self.refresh_all_lists()
            dialog.destroy()

        self.create_button(dialog, "Сохранить", save_changes, variant='success').grid(row=4, column=0, pady=20)

    def _edit_item_template_dialog(self, item_type):
        listbox = self.groups_listbox if item_type == 'group' else self.themes_listbox
        data_list = self.app_data["groups"] if item_type == 'group' else self.app_data["themes"]
        item_type_str = "группы" if item_type == 'group' else "темы"

        sel = listbox.curselection()
        if not sel: return messagebox.showwarning("Внимание", f"Выберите {item_type_str} для настройки шаблона!")
        item = data_list[sel[0]]

        if not self.app_data["templates"]: return messagebox.showinfo("Информация",
                                                                      "Создайте шаблоны сообщений перед настройкой.")

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Настройка шаблона для {item_type_str} {item['name']}")
        dlg.configure(bg=self.colors['bg'])
        dlg.transient(self.root);
        dlg.grab_set()
        width, height = 500, 550
        x, y = (self.root.winfo_screenwidth() - width) // 2, (self.root.winfo_screenheight() - height) // 2
        dlg.geometry(f"{width}x{height}+{x}+{y}")
        dlg.grid_columnconfigure(0, weight=1)
        dlg.grid_rowconfigure(2, weight=1)

        tk.Label(dlg, text="Выберите шаблон и настройте текст", bg=self.colors['bg'],
                 font=('Segoe UI', 12, 'bold')).grid(row=0, column=0, pady=(20, 10))

        combo_frame = tk.Frame(dlg, bg=self.colors['bg'])
        combo_frame.grid(row=1, column=0, sticky='ew', padx=20, pady=(0, 10))
        combo_frame.columnconfigure(1, weight=1)
        self.mk_label(combo_frame, "Шаблон:").grid(row=0, column=0, padx=(0, 10))
        template_names = [tpl["name"] for tpl in self.app_data["templates"]]
        template_var = tk.StringVar(value=template_names[0])
        template_combo = ttk.Combobox(combo_frame, textvariable=template_var, values=template_names, state='readonly')
        template_combo.grid(row=0, column=1, sticky='ew')

        text_frame, custom_text = self.mk_text(dlg)
        text_frame.grid(row=2, column=0, sticky='nsew', padx=20, pady=(0, 10))

        var_frame = tk.Frame(dlg, bg=self.colors['bg'])
        var_frame.grid(row=3, column=0, sticky='w', padx=20, pady=(4, 8))
        for i, param in enumerate(self.parameters):
            name = param['name_var'].get()
            placeholder = f"[{name}]"
            btn = self.create_button(var_frame, placeholder, lambda p=placeholder: custom_text.insert(tk.INSERT, p),
                                     variant='secondary')
            btn.grid(row=0, column=i, padx=4)

        def load_template_text(event=None):
            name = template_var.get()
            base_text = next((tpl.get("text", "") for tpl in self.app_data["templates"] if tpl["name"] == name), "")
            override_text = item.get('custom_templates', {}).get(name, base_text)
            custom_text.delete('1.0', tk.END);
            custom_text.insert('1.0', override_text)

        template_combo.bind('<<ComboboxSelected>>', load_template_text)
        load_template_text()

        def save_override():
            name = template_var.get()
            text = custom_text.get('1.0', tk.END).rstrip()
            item.setdefault('custom_templates', {})[name] = text
            save_app_data(self.app_data)
            messagebox.showinfo("Успех", f"Шаблон '{name}' обновлен для {item_type_str} {item['name']}")
            dlg.destroy()

        btn_frame = tk.Frame(dlg, bg=self.colors['bg'])
        btn_frame.grid(row=4, column=0, pady=15)
        self.create_button(btn_frame, "💾  Сохранить", save_override, variant='success').grid(row=0, column=0, padx=10)
        self.create_button(btn_frame, "✗  Отмена", dlg.destroy, variant='secondary').grid(row=0, column=1, padx=10)

    def use_template(self):
        sel = self.templates_listbox.curselection()
        if not sel: return messagebox.showwarning("Внимание", "Выберите шаблон!")

        tpl = self.app_data["templates"][sel[0]]
        self.message_text.delete("1.0", tk.END)
        self.message_text.insert("1.0", tpl.get("text", ""))
        self.current_template_name = tpl.get("name")

        if "params" in tpl and isinstance(tpl["params"], list):
            self.parameters = [{'name_var': tk.StringVar(value=p), 'value_var': tk.StringVar()} for p in tpl["params"]]
            self.build_params_section(self.param_frame)

        self.update_char_counter()

    def save_template(self):
        text = self.message_text.get("1.0", tk.END).strip()
        if not text: return messagebox.showwarning("Внимание", "Текст сообщения пуст!")
        name = simpledialog.askstring("Сохранить шаблон", "Название шаблона:", parent=self.root)
        if not name: return

        param_names = [p['name_var'].get() for p in self.parameters if p['name_var'].get()]

        if any(t["name"] == name for t in self.app_data["templates"]):
            if not messagebox.askyesno("Внимание", "Шаблон с таким именем уже существует. Перезаписать?"): return
            self.app_data["templates"] = [t for t in self.app_data["templates"] if t["name"] != name]

        self.app_data["templates"].append({"name": name, "text": text, "params": param_names})
        save_app_data(self.app_data)
        self.refresh_all_lists()
        messagebox.showinfo("Успех", "Шаблон сохранен!")

    def delete_template(self):
        sel = self.templates_listbox.curselection()
        if not sel: return messagebox.showwarning("Внимание", "Выберите шаблон для удаления!")
        name = self.app_data["templates"][sel[0]]["name"]
        if messagebox.askyesno("Подтверждение", f"Удалить шаблон '{name}'?"):
            del self.app_data["templates"][sel[0]]
            save_app_data(self.app_data)
            self.refresh_all_lists()
            messagebox.showinfo("Успех", "Шаблон удален!")

    def get_input_from_dialog(self, title, prompt, show=None, timeout=120):
        result, event = [], threading.Event()

        def ask():
            try:
                result.append(simpledialog.askstring(title, prompt, show=show, parent=self.root))
            finally:
                event.set()

        self.root.after(0, ask)
        event.wait(timeout=timeout)
        return result[0] if result else None

    # -- Fetch Page Logic --
    def fetch_user_groups(self):
        if not all(self.config.get(k) for k in ["api_id", "api_hash", "phone"]):
            messagebox.showwarning("Внимание", "Настройте API ключи!");
            return self.notebook.select(0)
        self.fetch_btn.state(['disabled']);
        self.fetch_btn.config(text="⏳ Загрузка...")
        threading.Thread(target=self.fetch_in_thread, daemon=True,
                         args=(lambda c: get_user_groups(c), self.update_fetched_groups_list_ui,
                               self.fetch_btn, "🔄  Загрузить мои группы")).start()

    def update_fetched_groups_list_ui(self, groups):
        self.fetched_groups_listbox.delete(0, tk.END)
        self.fetched_groups = groups
        for g in groups: self.fetched_groups_listbox.insert(tk.END, f"{g['name']} | ID: {g['id']}")
        messagebox.showinfo("Успех", f"Загружено {len(groups)} групп!")
        self.notebook.select(2)

    def add_fetched_groups(self):
        sel = self.fetched_groups_listbox.curselection()
        if not sel: return messagebox.showwarning("Внимание", "Выберите группы для добавления!")
        added = 0
        for idx in sel:
            g = self.fetched_groups[idx]
            if any(x['id'] == g['id'] for x in self.app_data["groups"]): continue

            result = self._ask_new_group_info(g)
            if result is None: continue

            client_num, selected_tag = result
            record = {"id": g['id'], "name": g['name'], "client_number": client_num, "tags": [], "custom_templates": {}}
            if selected_tag:
                record["tags"] = [selected_tag]
                if selected_tag not in self.app_data["tags"]: self.app_data["tags"].append(selected_tag)
            self.app_data["groups"].append(record)
            added += 1

        if added:
            save_app_data(self.app_data)
            self.refresh_all_lists()
            messagebox.showinfo("Успех", f"Добавлено {added} новых групп!")
        else:
            messagebox.showinfo("Информация", "Все выбранные группы уже есть в списке или добавление отменено.")

    def _ask_new_group_info(self, group):
        result = {'value': None}
        dialog = tk.Toplevel(self.root)
        dialog.title("Новый клиент");
        dialog.configure(bg=self.colors['bg'])
        dialog.transient(self.root);
        dialog.grab_set()
        width, height = 440, 300
        x, y = (self.root.winfo_screenwidth() - width) // 2, (self.root.winfo_screenheight() - height) // 2
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.grid_columnconfigure(0, weight=1)

        card = self.create_card(dialog, f"Добавление группы '{group['name']}'")
        card.grid(row=0, column=0, sticky='nsew', padx=20, pady=20)
        card.columnconfigure(0, weight=1)

        self.mk_label(card, "Укажите номер/название клиента:", bold=True).grid(row=0, column=0, sticky='w')
        entry = self.mk_entry(card);
        entry.grid(row=1, column=0, sticky='ew', pady=(4, 12))

        tags = self.app_data.get('tags', [])
        tag_var = tk.StringVar()
        if tags:
            options = ["— Без тега —"] + tags
            self.mk_label(card, "Выберите тег (необязательно):", bold=True).grid(row=2, column=0, sticky='w')
            combo = ttk.Combobox(card, state='readonly', values=options, textvariable=tag_var)
            combo.grid(row=3, column=0, sticky='ew', pady=(4, 12));
            combo.current(0)

        btn_frame = tk.Frame(card, bg=self.colors['card'])
        btn_frame.grid(row=4, column=0, sticky='ew', pady=(8, 4))
        btn_frame.columnconfigure((0, 1), weight=1)

        def on_ok():
            tag = tag_var.get() if tags and tag_var.get() != "— Без тега —" else None
            result['value'] = (entry.get().strip(), tag)
            dialog.destroy()

        self.create_button(btn_frame, "Добавить", on_ok, variant='success').grid(row=0, column=0, sticky='ew',
                                                                                 padx=(0, 4))
        self.create_button(btn_frame, "Отмена", dialog.destroy, variant='secondary').grid(row=0, column=1, sticky='ew',
                                                                                          padx=(4, 0))

        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.root.wait_window(dialog)
        return result['value']

    def fetch_all_group_topics(self):
        if not self.fetched_groups: return messagebox.showwarning("Внимание", "Сначала загрузите список групп!")
        self.fetch_topics_btn.state(['disabled']);
        self.fetch_topics_btn.config(text="⏳  Поиск тем...")
        threading.Thread(target=self.fetch_in_thread, daemon=True,
                         args=(self._fetch_topics_for_groups_async, self.update_fetched_topics_list_ui_multi,
                               self.fetch_topics_btn, "🔍  Найти темы")).start()

    async def _fetch_topics_for_groups_async(self, client):
        all_topics = []
        for g in self.fetched_groups:
            try:
                topics, error = await get_group_topics(client, g['id'])
                if topics:
                    all_topics.extend(
                        [{'group_id': g['id'], 'group_name': g['name'], 'topic_id': t['topic_id'], 'name': t['name']}
                         for t in topics])
            except Exception:
                continue
        return all_topics

    def update_fetched_topics_list_ui_multi(self, topics):
        self.fetched_topics_listbox.delete(0, tk.END)
        self.fetched_topics = topics
        if not topics: return messagebox.showinfo("Информация", "Темы не найдены для загруженных групп.")
        for t in topics: self.fetched_topics_listbox.insert(tk.END,
                                                            f"{t['group_name']} → {t['name']} | ID: {t['topic_id']}")
        messagebox.showinfo("Успех", f"Найдено {len(topics)} тем!")

    def add_fetched_topics(self):
        sel = self.fetched_topics_listbox.curselection()
        if not sel: return messagebox.showwarning("Внимание", "Выберите темы для добавления!")
        added = 0
        for idx in sel:
            t = self.fetched_topics[idx]
            if not any(
                    x['topic_id'] == t['topic_id'] and x['group_id'] == t['group_id'] for x in self.app_data["themes"]):
                self.app_data["themes"].append(
                    {"group_id": t['group_id'], "topic_id": t['topic_id'], "name": t['name'], "client_number": "",
                     "tags": [], "custom_templates": {}})
                added += 1
        if added:
            save_app_data(self.app_data)
            self.refresh_all_lists()
            messagebox.showinfo("Успех", f"Добавлено {added} новых тем!")
        else:
            messagebox.showinfo("Информация", "Все выбранные темы уже есть в списке.")

    def fetch_in_thread(self, async_func, callback, btn, btn_text):
        global TG_WORKER
        try:
            TG_WORKER.start(self)
            result = TG_WORKER.call(async_func)
            self.root.after(0, callback, result)
        except Exception as e:
            _logger.exception("Ошибка в фоне (fetch)")
            TG_WORKER = TelethonWorker()
            err_msg = str(e)
            if isinstance(e, ApiIdInvalidError): err_msg = "Некорректные API ID или API Hash."
            self.root.after(0, messagebox.showerror, "Ошибка", err_msg)
        finally:
            self.root.after(0, self._restore_button, btn, btn_text)

    def _restore_button(self, btn, text):
        try:
            btn.state(['!disabled']);
            btn.config(text=text)
        except tk.TclError:
            pass

    def load_saved_config(self):
        for key, entry in [("api_id", self.api_id_entry), ("api_hash", self.api_hash_entry),
                           ("phone", self.phone_entry), ("rate_delay", self.rate_delay_entry)]:
            if (value := self.config.get(key)) is not None:
                entry.delete(0, tk.END);
                entry.insert(0, str(value))

    # ============================================
    # LOGIC HANDLERS
    # ============================================
    # -- Settings Page Logic --
    def save_settings(self):
        api_id, api_hash, phone = self.api_id_entry.get(), self.api_hash_entry.get(), self.phone_entry.get()
        if not all([api_id, api_hash, phone]): return messagebox.showwarning("Внимание", "Заполните все поля!")
        if not phone.startswith("+"): return messagebox.showwarning("Внимание", "Номер телефона должен начинаться с +")
        try:
            rate_delay = float(self.rate_delay_entry.get())
            if rate_delay < 0: raise ValueError
        except ValueError:
            return messagebox.showerror("Ошибка", "Задержка должна быть неотрицательным числом.")

        self.config = {"api_id": api_id, "api_hash": api_hash, "phone": phone, "rate_delay": rate_delay}
        save_config(api_id, api_hash, phone, rate_delay)
        self.settings_status.config(text="✓ Настройки сохранены!")

        global TG_WORKER;
        TG_WORKER = TelethonWorker()
        self.root.after(3000, lambda: self.settings_status.config(text=""))

    # -- Sending Page Logic --
    def select_all(self):
        for var, _ in getattr(self, 'group_vars', []) + getattr(self, 'theme_vars', []): var.set(True)

    def deselect_all(self):
        for var, _ in getattr(self, 'group_vars', []) + getattr(self, 'theme_vars', []): var.set(False)

    def log(self, message):
        _logger.info(message)
        self.root.after(0, self._log_threadsafe, message)

    def _log_threadsafe(self, message):
        self.log_text.configure(state='normal')
        self.log_text.insert(tk.END, message + "\n");
        self.log_text.see(tk.END)
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

        replaced_message = self.replace_vars(self.message_text.get("1.0", tk.END).strip())
        if not replaced_message and not self.attachments: return messagebox.showwarning("Внимание",
                                                                                        "Введите текст или добавьте вложения!")

        self.show_confirmation_dialog(selected_groups, selected_themes, replaced_message)

    def show_confirmation_dialog(self, selected_groups, selected_themes, message):
        dialog = tk.Toplevel(self.root)
        dialog.title("Подтверждение");
        dialog.configure(bg=self.colors['bg'])
        dialog.transient(self.root);
        dialog.grab_set()
        width, height = 700, 600
        x, y = (self.root.winfo_screenwidth() - width) // 2, (self.root.winfo_screenheight() - height) // 2
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.grid_columnconfigure(0, weight=1)

        tk.Label(dialog, text="Подтверждение отправки", bg=self.colors['bg'], font=('Segoe UI', 14, 'bold')).grid(row=0,
                                                                                                                  column=0,
                                                                                                                  pady=(
                                                                                                                      20,
                                                                                                                      10))

        container, scrollable_area = self._create_scrollable_area(dialog)
        container.grid(row=1, column=0, sticky='nsew', padx=20, pady=(0, 10))
        dialog.grid_rowconfigure(1, weight=1)

        current_tpl_name = getattr(self, 'current_template_name', None)
        recipient_entries, entry_widgets = [], []

        all_recipients = [('group', g) for g in selected_groups] + [('theme', t) for t in selected_themes]

        for i, (type, data) in enumerate(all_recipients):
            override = data.get('custom_templates', {}).get(current_tpl_name) if current_tpl_name else None
            msg_text = self.replace_vars(override if override is not None else message)
            recipient_entries.append({'type': type, 'data': data, 'message': msg_text})

            row = tk.Frame(scrollable_area, bg=self.colors['card'], relief='solid', bd=1)
            row.grid(row=i, column=0, sticky='ew', pady=4)
            row.columnconfigure(0, weight=1)
            tk.Label(row, text=data['name'], bg=self.colors['card'], font=('Segoe UI', 10, 'bold')).grid(row=0,
                                                                                                         column=0,
                                                                                                         sticky='ew',
                                                                                                         padx=6,
                                                                                                         pady=(4, 2))

            txt_frame, txt = self.mk_text(row)
            txt_frame.grid(row=1, column=0, sticky='nsew', padx=6, pady=(0, 6))
            row.rowconfigure(1, weight=1)
            txt.insert('1.0', msg_text);
            entry_widgets.append(txt)

        if self.attachments:
            attach_card = self.create_card(dialog, f"📎  Вложения ({len(self.attachments)})")
            attach_card.grid(row=2, column=0, sticky='ew', padx=20, pady=(0, 10))
            attach_card.rowconfigure(0, weight=1)
            attach_card.columnconfigure(0, weight=1)
            list_frame, attach_list = self.mk_listbox(attach_card)
            list_frame.grid(row=0, column=0, sticky='nsew')
            for path in self.attachments: attach_list.insert(tk.END, os.path.basename(path))
            attach_list.config(state='disabled')

        btn_frame = tk.Frame(dialog, bg=self.colors['bg']);
        btn_frame.grid(row=3, column=0, pady=10)

        def confirm():
            custom_msgs = []
            for i, rec in enumerate(recipient_entries):
                msg_txt = entry_widgets[i].get('1.0', tk.END).rstrip()
                if not msg_txt and not self.attachments:
                    return messagebox.showwarning("Внимание",
                                                  f"Сообщение для '{rec['data']['name']}' не может быть пустым!")
                custom_msgs.append({'type': rec['type'], 'data': rec['data'], 'message': msg_txt})
            self.confirm_and_send(dialog, custom_msgs)

        self.create_button(btn_frame, "✓  Отправить", confirm, variant='success').grid(row=0, column=0, padx=10)
        self.create_button(btn_frame, "✗  Отмена", dialog.destroy, variant='secondary').grid(row=0, column=1, padx=10)

    def confirm_and_send(self, dialog, custom_messages):
        dialog.destroy()
        self.is_sending = True
        self.send_btn.state(['disabled']);
        self.send_btn.config(text="⏳ Идет отправка...")
        self.log("🚀 Начинаю отправку...\n")
        threading.Thread(target=self.send_in_thread, daemon=True,
                         args=(list(self.attachments), custom_messages)).start()

    def send_in_thread(self, attachments, custom_messages):
        global TG_WORKER
        try:
            self.log("🔌 Подключение к Telegram...")
            TG_WORKER.start(self)
            self.log("✓ Успешно подключено!")
            rate_delay = self.config.get("rate_delay", 10)

            async def _send_all(client):
                success, failed = 0, 0
                for entry in custom_messages:
                    try:
                        data, msg_text = entry['data'], entry['message']
                        recipient_id = data['id'] if entry['type'] == 'group' else data['group_id']
                        reply_to = data.get('topic_id') if entry['type'] == 'theme' else None
                        name = f"{data['name']} (клиент: {data.get('client_number', 'N/A')})"

                        if attachments:
                            await client.send_file(recipient_id, file=attachments,
                                                   caption=msg_text if msg_text else None, reply_to=reply_to)
                        elif msg_text:
                            await client.send_message(recipient_id, message=msg_text, reply_to=reply_to)

                        self.log(f"✓ Отправлено: {name}")
                        success += 1
                        await asyncio.sleep(rate_delay)
                    except Exception as e:
                        self.log(f"✗ Ошибка {name}: {e}")
                        _logger.exception("Ошибка отправки")
                        failed += 1
                return success, failed

            success, failed = TG_WORKER.call(_send_all)

            self.log(f"\n{'=' * 30} 📊 ИТОГО: ✓ {success} | ✗ {failed} {'=' * 30}\n")

            if failed == 0:
                self.root.after(0, messagebox.showinfo, "Успех",
                                f"Все сообщения успешно отправлены!\nОтправлено: {success}")
            else:
                self.root.after(0, messagebox.showwarning, "Завершено с ошибками",
                                f"Отправлено: {success}\nОшибок: {failed}")

        except Exception as e:
            _logger.exception("Ошибка при отправке")
            TG_WORKER = TelethonWorker()
            err_msg = str(e)
            if isinstance(e, ApiIdInvalidError): err_msg = "Некорректные API ID или API Hash."
            self.root.after(0, messagebox.showerror, "Ошибка", err_msg)
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
