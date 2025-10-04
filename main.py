import asyncio
import json
import os
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog

from telethon import TelegramClient

# ============================================
# КОНФИГУРАЦИЯ
# ============================================
USER_CONFIG = "config.json"
GROUPS_FILE = "groups.json"
client = None


def load_config():
    """Загрузить данные"""
    if os.path.exists(USER_CONFIG):
        try:
            with open(USER_CONFIG, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {"api_id": "", "api_hash": "", "phone": ""}


def save_config(api_id, api_hash, phone):
    """Сохранить настройки"""
    config = {"api_id": api_id, "api_hash": api_hash, "phone": phone}
    with open(USER_CONFIG, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def load_groups_data():
    """Загрузить список групп и тем"""
    if os.path.exists(GROUPS_FILE):
        try:
            with open(GROUPS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get("groups", []), data.get("themes", [])
        except:
            pass
    return [], []


def save_groups_data(groups, themes):
    """Сохранить список групп и тем"""
    data = {"groups": groups, "themes": themes}
    with open(GROUPS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


async def init_client(app, api_id, api_hash, phone):
    global client

    session_name = f"session_{phone.strip().replace('+', '')}"
    client = TelegramClient(session_name, api_id, api_hash)

    await client.connect()

    if not await client.is_user_authorized():
        await client.send_code_request(phone)

        code = app.get_input_from_dialog(
            "Код подтверждения",
            "Введите код из SMS/Telegram:"
        )

        if not code:
            raise Exception("Код не введен")

        try:
            await client.sign_in(phone, code)
        except Exception as e:
            if "password" in str(e).lower() or "SessionPasswordNeededError" in str(type(e).__name__):
                # ИЗМЕНЕНИЕ: Используем новый потокобезопасный метод для запроса пароля.
                password = app.get_input_from_dialog(
                    "Двухфакторная авторизация",
                    "Введите пароль 2FA:",
                    show='*'
                )

                if not password:
                    raise Exception("Пароль 2FA не введен")

                await client.sign_in(password=password)
            else:
                raise e

    return client


async def get_user_groups():
    """Получить все группы пользователя"""
    groups = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            groups.append({
                "id": dialog.id,
                "name": dialog.title,
                "username": dialog.entity.username if hasattr(dialog.entity, 'username') else ""
            })
    return groups


async def send_messages(selected_groups, selected_themes, message_text, log_callback):
    """Отправка сообщений в выбранные группы и темы"""
    success = 0
    failed = 0

    for group in selected_groups:
        try:
            await client.send_message(group["id"], message_text)
            log_callback(f"✓ Отправлено: {group['name']} (каб. {group.get('cabinet', 'N/A')})")
            success += 1
            await asyncio.sleep(10)
        except Exception as e:
            log_callback(f"✗ Ошибка {group['name']}: {str(e)}")
            failed += 1

    for theme in selected_themes:
        try:
            await client.send_message(
                entity=theme["group_id"],
                message=message_text,
                reply_to=theme["topic_id"]
            )
            log_callback(f"✓ Отправлено: {theme['name']} (каб. {theme.get('cabinet', 'N/A')})")
            success += 1
            await asyncio.sleep(10)
        except Exception as e:
            log_callback(f"✗ Ошибка {theme['name']}: {str(e)}")
            failed += 1

    return success, failed


# ============================================
# GUI ПРИЛОЖЕНИЕ
# ============================================
class TelegramSenderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Telegram Sender Pro")
        self.root.geometry("1100x850")

        self.colors = {
            'bg': '#f5f7fa',
            'sidebar': '#2c3e50',
            'card': '#ffffff',
            'primary': '#3498db',
            'success': '#27ae60',
            'danger': '#e74c3c',
            'warning': '#f39c12',
            'text': '#2c3e50',
            'text_light': '#7f8c8d',
            'border': '#dce1e6',
            'input_bg': '#f8f9fa',
            'hover': '#ecf0f1'
        }

        self.root.configure(bg=self.colors['bg'])
        self.setup_styles()

        self.group_vars = []
        self.theme_vars = []
        self.is_sending = False
        self.config = load_config()
        self.groups_data, self.themes_data = load_groups_data()
        self.fetched_groups = []

        self.create_widgets()
        self.load_saved_config()
        self.refresh_sending_lists()

    def setup_styles(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TNotebook', background=self.colors['bg'], borderwidth=0)
        style.configure('TNotebook.Tab',
                        background=self.colors['card'],
                        foreground=self.colors['text'],
                        padding=[20, 12],
                        font=('Segoe UI', 10),
                        borderwidth=0)
        style.map('TNotebook.Tab',
                  background=[('selected', self.colors['primary'])],
                  foreground=[('selected', 'white')])
        style.configure('Card.TLabelframe',
                        background=self.colors['card'],
                        bordercolor=self.colors['border'],
                        borderwidth=1,
                        relief='solid')
        style.configure('Card.TLabelframe.Label',
                        background=self.colors['card'],
                        foreground=self.colors['text'],
                        font=('Segoe UI', 11, 'bold'))

    def _on_mousewheel(self, event, canvas):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _bind_mousewheel(self, widget, canvas):
        widget.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", lambda ev: self._on_mousewheel(ev, canvas)))
        widget.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

    def create_modern_button(self, parent, text, command, color, **kwargs):
        btn = tk.Button(parent, text=text, command=command,
                        bg=color, fg='white',
                        font=('Segoe UI', 10, 'bold'),
                        relief='flat', bd=0,
                        padx=20, pady=10,
                        cursor='hand2',
                        activebackground=color,
                        activeforeground='white',
                        **kwargs)

        def on_enter(e): btn['bg'] = self._darken_color(color)

        def on_leave(e): btn['bg'] = color

        btn.bind('<Enter>', on_enter)
        btn.bind('<Leave>', on_leave)
        return btn

    def _darken_color(self, hex_color, factor=0.8):
        hex_color = hex_color.lstrip('#')
        rgb = tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
        darkened = tuple(int(c * factor) for c in rgb)
        return '#{:02x}{:02x}{:02x}'.format(*darkened)

    def create_modern_entry(self, parent, placeholder="", show=None):
        return tk.Entry(parent, font=('Segoe UI', 10), bg=self.colors['input_bg'], fg=self.colors['text'],
                        relief='solid', bd=1, insertbackground=self.colors['text'], show=show if show else '')

    def create_widgets(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=0, pady=0)
        settings_tab = tk.Frame(self.notebook, bg=self.colors['bg'])
        manage_tab = tk.Frame(self.notebook, bg=self.colors['bg'])
        fetch_tab = tk.Frame(self.notebook, bg=self.colors['bg'])
        sending_tab = tk.Frame(self.notebook, bg=self.colors['bg'])
        self.notebook.add(settings_tab, text="  ⚙️  Настройки  ")
        self.notebook.add(manage_tab, text="  📋  Управление списками  ")
        self.notebook.add(fetch_tab, text="  📥  Получить группы  ")
        self.notebook.add(sending_tab, text="  📤  Отправка сообщений  ")
        self.create_settings_tab(settings_tab)
        self.create_manage_tab(manage_tab)
        self.create_fetch_tab(fetch_tab)
        self.create_sending_tab(sending_tab)

    def create_card(self, parent, title):
        return ttk.LabelFrame(parent, text=title, style='Card.TLabelframe', padding=20)

    def create_settings_tab(self, parent):
        container = tk.Frame(parent, bg=self.colors['bg'])
        container.place(relx=0.5, rely=0.5, anchor='center')
        card = self.create_card(container, "🔐  Настройки Telegram API")
        card.pack(padx=40, pady=20)
        info_frame = tk.Frame(card, bg='#e3f2fd', relief='flat', bd=0)
        info_frame.pack(fill="x", pady=(0, 25), padx=5)
        tk.Label(info_frame, text="ℹ️", bg='#e3f2fd', font=('Segoe UI', 16)).pack(side='left', padx=(10, 5), pady=10)
        info_text = "Для работы необходимы API ключи от Telegram.\nПолучите их на: https://my.telegram.org/apps\nПри первом запуске придет SMS с кодом."
        tk.Label(info_frame, text=info_text, bg='#e3f2fd', fg='#0d47a1', justify='left', font=('Segoe UI', 9)).pack(
            side='left', pady=10, padx=(5, 10))
        fields_frame = tk.Frame(card, bg=self.colors['card'])
        fields_frame.pack(fill="x", pady=10)

        def create_field(label_text, row):
            tk.Label(fields_frame, text=label_text, bg=self.colors['card'], fg=self.colors['text'],
                     font=('Segoe UI', 10, 'bold')).grid(row=row, column=0, sticky='w', pady=12, padx=(0, 15))
            entry = self.create_modern_entry(fields_frame)
            entry.grid(row=row, column=1, sticky='ew', pady=12)
            entry.config(width=40)
            return entry

        self.api_id_entry = create_field("API ID:", 0)
        self.api_hash_entry = create_field("API Hash:", 1)
        self.phone_entry = create_field("Телефон:", 2)
        fields_frame.columnconfigure(1, weight=1)
        tk.Label(fields_frame, text="Формат: +380991234567", bg=self.colors['card'], fg=self.colors['text_light'],
                 font=('Segoe UI', 8)).grid(row=3, column=1, sticky='w', pady=(0, 5))
        btn_frame = tk.Frame(card, bg=self.colors['card'])
        btn_frame.pack(pady=20)
        self.save_btn = self.create_modern_button(btn_frame, "💾  Сохранить настройки", self.save_settings,
                                                  self.colors['success'])
        self.save_btn.pack()
        self.settings_status = tk.Label(card, text="", bg=self.colors['card'], font=('Segoe UI', 10))
        self.settings_status.pack(pady=10)

    def create_manage_tab(self, parent):
        main_frame = tk.Frame(parent, bg=self.colors['bg'])
        main_frame.pack(fill="both", expand=True, padx=20, pady=20)
        left_panel = tk.Frame(main_frame, bg=self.colors['bg'])
        left_panel.pack(side='left', fill='both', expand=True, padx=(0, 10))
        groups_card = self.create_card(left_panel, "📁  Управление группами")
        groups_card.pack(fill='both', expand=True)
        self.create_groups_manager(groups_card)
        right_panel = tk.Frame(main_frame, bg=self.colors['bg'])
        right_panel.pack(side='right', fill='both', expand=True, padx=(10, 0))
        themes_card = self.create_card(right_panel, "🧵  Управление темами")
        themes_card.pack(fill='both', expand=True)
        self.create_themes_manager(themes_card)

    def create_groups_manager(self, parent):
        list_frame = tk.Frame(parent, bg=self.colors['card'])
        list_frame.pack(fill='both', expand=True, pady=(0, 15))
        self.groups_listbox = tk.Listbox(list_frame, font=('Segoe UI', 9), bg=self.colors['input_bg'],
                                         fg=self.colors['text'], relief='solid', bd=1,
                                         selectbackground=self.colors['primary'], selectforeground='white', height=12)
        scrollbar = tk.Scrollbar(list_frame, orient='vertical', command=self.groups_listbox.yview)
        self.groups_listbox.config(yscrollcommand=scrollbar.set)
        self.groups_listbox.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        self.refresh_groups_list()
        form_frame = tk.Frame(parent, bg=self.colors['card'])
        form_frame.pack(fill='x', pady=10)
        tk.Label(form_frame, text="ID группы:", bg=self.colors['card'], fg=self.colors['text'],
                 font=('Segoe UI', 9, 'bold')).grid(row=0, column=0, sticky='w', pady=8)
        self.group_id_entry = self.create_modern_entry(form_frame)
        self.group_id_entry.grid(row=0, column=1, sticky='ew', pady=8, padx=(10, 0))
        tk.Label(form_frame, text="Название:", bg=self.colors['card'], fg=self.colors['text'],
                 font=('Segoe UI', 9, 'bold')).grid(row=1, column=0, sticky='w', pady=8)
        self.group_name_entry = self.create_modern_entry(form_frame)
        self.group_name_entry.grid(row=1, column=1, sticky='ew', pady=8, padx=(10, 0))
        tk.Label(form_frame, text="Кабинет:", bg=self.colors['card'], fg=self.colors['text'],
                 font=('Segoe UI', 9, 'bold')).grid(row=2, column=0, sticky='w', pady=8)
        self.group_cabinet_entry = self.create_modern_entry(form_frame)
        self.group_cabinet_entry.grid(row=2, column=1, sticky='ew', pady=8, padx=(10, 0))
        form_frame.columnconfigure(1, weight=1)
        btn_frame = tk.Frame(parent, bg=self.colors['card'])
        btn_frame.pack(fill='x', pady=10)
        self.create_modern_button(btn_frame, "➕ Добавить", self.add_group, self.colors['success'], width=12).pack(
            side='left', padx=5)
        self.create_modern_button(btn_frame, "❌ Удалить", self.delete_group, self.colors['danger'], width=12).pack(
            side='left', padx=5)

    def create_themes_manager(self, parent):
        list_frame = tk.Frame(parent, bg=self.colors['card'])
        list_frame.pack(fill='both', expand=True, pady=(0, 15))
        self.themes_listbox = tk.Listbox(list_frame, font=('Segoe UI', 9), bg=self.colors['input_bg'],
                                         fg=self.colors['text'], relief='solid', bd=1,
                                         selectbackground=self.colors['primary'], selectforeground='white', height=12)
        scrollbar = tk.Scrollbar(list_frame, orient='vertical', command=self.themes_listbox.yview)
        self.themes_listbox.config(yscrollcommand=scrollbar.set)
        self.themes_listbox.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        self.refresh_themes_list()
        form_frame = tk.Frame(parent, bg=self.colors['card'])
        form_frame.pack(fill='x', pady=10)
        tk.Label(form_frame, text="ID группы:", bg=self.colors['card'], fg=self.colors['text'],
                 font=('Segoe UI', 9, 'bold')).grid(row=0, column=0, sticky='w', pady=8)
        self.theme_group_id_entry = self.create_modern_entry(form_frame)
        self.theme_group_id_entry.grid(row=0, column=1, sticky='ew', pady=8, padx=(10, 0))
        tk.Label(form_frame, text="ID темы:", bg=self.colors['card'], fg=self.colors['text'],
                 font=('Segoe UI', 9, 'bold')).grid(row=1, column=0, sticky='w', pady=8)
        self.theme_topic_id_entry = self.create_modern_entry(form_frame)
        self.theme_topic_id_entry.grid(row=1, column=1, sticky='ew', pady=8, padx=(10, 0))
        tk.Label(form_frame, text="Название:", bg=self.colors['card'], fg=self.colors['text'],
                 font=('Segoe UI', 9, 'bold')).grid(row=2, column=0, sticky='w', pady=8)
        self.theme_name_entry = self.create_modern_entry(form_frame)
        self.theme_name_entry.grid(row=2, column=1, sticky='ew', pady=8, padx=(10, 0))
        tk.Label(form_frame, text="Кабинет:", bg=self.colors['card'], fg=self.colors['text'],
                 font=('Segoe UI', 9, 'bold')).grid(row=3, column=0, sticky='w', pady=8)
        self.theme_cabinet_entry = self.create_modern_entry(form_frame)
        self.theme_cabinet_entry.grid(row=3, column=1, sticky='ew', pady=8, padx=(10, 0))
        form_frame.columnconfigure(1, weight=1)
        btn_frame = tk.Frame(parent, bg=self.colors['card'])
        btn_frame.pack(fill='x', pady=10)
        self.create_modern_button(btn_frame, "➕ Добавить", self.add_theme, self.colors['success'], width=12).pack(
            side='left', padx=5)
        self.create_modern_button(btn_frame, "❌ Удалить", self.delete_theme, self.colors['danger'], width=12).pack(
            side='left', padx=5)

    def create_fetch_tab(self, parent):
        container = tk.Frame(parent, bg=self.colors['bg'])
        container.place(relx=0.5, rely=0.5, anchor='center')
        card = self.create_card(container, "📥  Получить список всех групп")
        card.pack(padx=40, pady=20)
        info_frame = tk.Frame(card, bg='#fff3cd', relief='flat', bd=0)
        info_frame.pack(fill="x", pady=(0, 20), padx=5)
        tk.Label(info_frame, text="⚠️", bg='#fff3cd', font=('Segoe UI', 16)).pack(side='left', padx=(10, 5), pady=10)
        info_text = "Эта функция загрузит все группы вашего Telegram аккаунта.\nВы сможете выбрать нужные и добавить их в список для отправки.\nУбедитесь, что вы настроили API ключи на вкладке \"Настройки\"."
        tk.Label(info_frame, text=info_text, bg='#fff3cd', fg='#856404', justify='left', font=('Segoe UI', 9)).pack(
            side='left', pady=10, padx=(5, 10))
        btn_frame = tk.Frame(card, bg=self.colors['card'])
        btn_frame.pack(pady=20)
        self.fetch_btn = self.create_modern_button(btn_frame, "🔄  Загрузить мои группы", self.fetch_user_groups,
                                                   self.colors['primary'])
        self.fetch_btn.pack()
        tk.Label(card, text="Загруженные группы:", bg=self.colors['card'], fg=self.colors['text'],
                 font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(20, 5))
        list_frame = tk.Frame(card, bg=self.colors['card'])
        list_frame.pack(fill='both', expand=True, pady=(0, 15))
        self.fetched_groups_listbox = tk.Listbox(list_frame, font=('Segoe UI', 9), bg=self.colors['input_bg'],
                                                 fg=self.colors['text'], relief='solid', bd=1, selectmode='multiple',
                                                 selectbackground=self.colors['primary'], selectforeground='white',
                                                 height=15, width=70)
        scrollbar = tk.Scrollbar(list_frame, orient='vertical', command=self.fetched_groups_listbox.yview)
        self.fetched_groups_listbox.config(yscrollcommand=scrollbar.set)
        self.fetched_groups_listbox.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        cabinet_frame = tk.Frame(card, bg=self.colors['card'])
        cabinet_frame.pack(fill='x', pady=10)
        tk.Label(cabinet_frame, text="Номер кабинета для выбранных групп:", bg=self.colors['card'],
                 fg=self.colors['text'], font=('Segoe UI', 9, 'bold')).pack(side='left', padx=(0, 10))
        self.cabinet_for_fetched = self.create_modern_entry(cabinet_frame)
        self.cabinet_for_fetched.pack(side='left', fill='x', expand=True)
        add_frame = tk.Frame(card, bg=self.colors['card'])
        add_frame.pack(pady=15)
        self.create_modern_button(add_frame, "➕  Добавить выбранные группы", self.add_fetched_groups,
                                  self.colors['success']).pack()

    def create_sending_tab(self, parent):
        main_frame = tk.Frame(parent, bg=self.colors['bg'])
        main_frame.pack(fill='both', expand=True, padx=20, pady=20)
        self.left_col_sending = tk.Frame(main_frame, bg=self.colors['bg'])
        self.left_col_sending.pack(side='left', fill='both', expand=True, padx=(0, 10))
        right_col = tk.Frame(main_frame, bg=self.colors['bg'])
        right_col.pack(side='right', fill='both', expand=True, padx=(10, 0))
        msg_card = self.create_card(right_col, "✉️  Текст сообщения")
        msg_card.pack(fill='both', expand=True, pady=(0, 10))
        self.message_text = scrolledtext.ScrolledText(msg_card, height=15, wrap=tk.WORD, font=('Segoe UI', 10),
                                                      bg=self.colors['input_bg'], fg=self.colors['text'],
                                                      relief='solid', bd=1, insertbackground=self.colors['text'])
        self.message_text.pack(fill='both', expand=True)
        send_frame = tk.Frame(right_col, bg=self.colors['bg'])
        send_frame.pack(pady=10)
        self.send_btn = self.create_modern_button(send_frame, "📨  Отправить сообщения", self.prepare_send,
                                                  self.colors['success'])
        self.send_btn.config(font=('Segoe UI', 12, 'bold'), padx=40, pady=15)
        self.send_btn.pack()
        log_card = self.create_card(right_col, "📊  Лог отправки")
        log_card.pack(fill='both', expand=True)
        self.log_text = scrolledtext.ScrolledText(log_card, height=10, state='disabled', font=('Consolas', 9),
                                                  bg=self.colors['input_bg'], fg=self.colors['text'], relief='solid',
                                                  bd=1)
        self.log_text.pack(fill='both', expand=True)

    def build_sending_lists(self, parent):
        for widget in parent.winfo_children():
            widget.destroy()
        groups_card = self.create_card(parent, "📁  Выбор групп")
        groups_card.pack(fill='both', expand=True, pady=(0, 10))
        canvas_groups = tk.Canvas(groups_card, bg=self.colors['card'], highlightthickness=0)
        scrollbar_groups = tk.Scrollbar(groups_card, orient='vertical', command=canvas_groups.yview)
        scrollable_groups = tk.Frame(canvas_groups, bg=self.colors['card'])
        scrollable_groups.bind("<Configure>", lambda e: canvas_groups.configure(scrollregion=canvas_groups.bbox("all")))
        canvas_groups.create_window((0, 0), window=scrollable_groups, anchor='nw')
        canvas_groups.configure(yscrollcommand=scrollbar_groups.set)
        self._bind_mousewheel(scrollable_groups, canvas_groups)
        self.group_vars = []
        for group in self.groups_data:
            var = tk.BooleanVar()
            self.group_vars.append((var, group))
            label = f"{group['name']} - Каб. {group.get('cabinet', 'N/A')}"
            cb = tk.Checkbutton(scrollable_groups, text=label, variable=var, bg=self.colors['card'],
                                fg=self.colors['text'], font=('Segoe UI', 9), selectcolor=self.colors['input_bg'],
                                activebackground=self.colors['card'], activeforeground=self.colors['text'])
            cb.pack(anchor='w', pady=3, padx=5)
        canvas_groups.pack(side='left', fill='both', expand=True)
        scrollbar_groups.pack(side='right', fill='y')
        themes_card = self.create_card(parent, "🧵  Выбор тем")
        themes_card.pack(fill='both', expand=True)
        canvas_themes = tk.Canvas(themes_card, bg=self.colors['card'], highlightthickness=0)
        scrollbar_themes = tk.Scrollbar(themes_card, orient='vertical', command=canvas_themes.yview)
        scrollable_themes = tk.Frame(canvas_themes, bg=self.colors['card'])
        scrollable_themes.bind("<Configure>", lambda e: canvas_themes.configure(scrollregion=canvas_themes.bbox("all")))
        canvas_themes.create_window((0, 0), window=scrollable_themes, anchor='nw')
        canvas_themes.configure(yscrollcommand=scrollbar_themes.set)
        self._bind_mousewheel(scrollable_themes, canvas_themes)
        self.theme_vars = []
        for theme in self.themes_data:
            var = tk.BooleanVar()
            self.theme_vars.append((var, theme))
            label = f"{theme['name']} - Каб. {theme.get('cabinet', 'N/A')}"
            cb = tk.Checkbutton(scrollable_themes, text=label, variable=var, bg=self.colors['card'],
                                fg=self.colors['text'], font=('Segoe UI', 9), selectcolor=self.colors['input_bg'],
                                activebackground=self.colors['card'], activeforeground=self.colors['text'])
            cb.pack(anchor='w', pady=3, padx=5)
        canvas_themes.pack(side='left', fill='both', expand=True)
        scrollbar_themes.pack(side='right', fill='y')
        btn_select_frame = tk.Frame(parent, bg=self.colors['bg'])
        btn_select_frame.pack(fill='x', pady=10)
        self.create_modern_button(btn_select_frame, "✓ Выбрать все", self.select_all, self.colors['primary'],
                                  width=14).pack(side='left', padx=5)
        self.create_modern_button(btn_select_frame, "✗ Снять все", self.deselect_all, '#95a5a6', width=14).pack(
            side='left', padx=5)

    def refresh_groups_list(self):
        self.groups_listbox.delete(0, tk.END)
        for group in self.groups_data:
            self.groups_listbox.insert(tk.END,
                                       f"{group['name']} | ID: {group['id']} | Каб: {group.get('cabinet', 'N/A')}")

    def refresh_themes_list(self):
        self.themes_listbox.delete(0, tk.END)
        for theme in self.themes_data:
            self.themes_listbox.insert(tk.END,
                                       f"{theme['name']} | Группа: {theme['group_id']} | Тема: {theme['topic_id']} | Каб: {theme.get('cabinet', 'N/A')}")

    def add_group(self):
        try:
            group_id, name, cabinet = int(
                self.group_id_entry.get().strip()), self.group_name_entry.get().strip(), self.group_cabinet_entry.get().strip()
            if not name: return messagebox.showwarning("Внимание", "Укажите название группы!")
            self.groups_data.append({"id": group_id, "name": name, "cabinet": cabinet})
            save_groups_data(self.groups_data, self.themes_data)
            self.refresh_groups_list()
            self.refresh_sending_lists()
            self.group_id_entry.delete(0, tk.END);
            self.group_name_entry.delete(0, tk.END);
            self.group_cabinet_entry.delete(0, tk.END)
            messagebox.showinfo("Успех", "Группа добавлена!")
        except ValueError:
            messagebox.showerror("Ошибка", "ID группы должен быть числом!")

    def delete_group(self):
        if not (selection := self.groups_listbox.curselection()): return messagebox.showwarning("Внимание",
                                                                                                "Выберите группу для удаления!")
        if messagebox.askyesno("Подтверждение", "Удалить выбранную группу?"):
            del self.groups_data[selection[0]]
            save_groups_data(self.groups_data, self.themes_data)
            self.refresh_groups_list()
            self.refresh_sending_lists()
            messagebox.showinfo("Успех", "Группа удалена!")

    def add_theme(self):
        try:
            group_id, topic_id, name, cabinet = int(self.theme_group_id_entry.get().strip()), int(
                self.theme_topic_id_entry.get().strip()), self.theme_name_entry.get().strip(), self.theme_cabinet_entry.get().strip()
            if not name: return messagebox.showwarning("Внимание", "Укажите название темы!")
            self.themes_data.append({"group_id": group_id, "topic_id": topic_id, "name": name, "cabinet": cabinet})
            save_groups_data(self.groups_data, self.themes_data)
            self.refresh_themes_list()
            self.refresh_sending_lists()
            self.theme_group_id_entry.delete(0, tk.END);
            self.theme_topic_id_entry.delete(0, tk.END);
            self.theme_name_entry.delete(0, tk.END);
            self.theme_cabinet_entry.delete(0, tk.END)
            messagebox.showinfo("Успех", "Тема добавлена!")
        except ValueError:
            messagebox.showerror("Ошибка", "ID группы и темы должны быть числами!")

    def delete_theme(self):
        if not (selection := self.themes_listbox.curselection()): return messagebox.showwarning("Внимание",
                                                                                                "Выберите тему для удаления!")
        if messagebox.askyesno("Подтверждение", "Удалить выбранную тему?"):
            del self.themes_data[selection[0]]
            save_groups_data(self.groups_data, self.themes_data)
            self.refresh_themes_list()
            self.refresh_sending_lists()
            messagebox.showinfo("Успех", "Тема удалена!")

    # ИЗМЕНЕНИЕ: Новые методы для потокобезопасного взаимодействия с GUI
    def get_input_from_dialog(self, title, prompt, show=None):
        result_container = []
        event = threading.Event()

        def ask_and_set():
            res = simpledialog.askstring(title, prompt, show=show, parent=self.root)
            result_container.append(res)
            event.set()

        self.root.after(0, ask_and_set)
        event.wait()
        return result_container[0] if result_container else None

    def update_fetched_groups_list_ui(self, groups):
        self.fetched_groups_listbox.delete(0, tk.END)
        self.fetched_groups = groups
        for group in groups:
            display = f"{group['name']} | ID: {group['id']}"
            if group['username']: display += f" | @{group['username']}"
            self.fetched_groups_listbox.insert(tk.END, display)
        messagebox.showinfo("Успех", f"Загружено {len(groups)} групп!")

    def show_thread_safe_message(self, msg_type, title, message):
        if msg_type == "error":
            messagebox.showerror(title, message)
        elif msg_type == "info":
            messagebox.showinfo(title, message)

    def fetch_user_groups(self):
        if not all(self.config.get(k) for k in ["api_id", "api_hash", "phone"]):
            messagebox.showwarning("Внимание", "Сначала настройте API ключи на вкладке 'Настройки'!")
            return self.notebook.select(0)
        self.fetch_btn.config(state='disabled', text="⏳  Загрузка...")
        threading.Thread(target=self.fetch_in_thread, daemon=True).start()

    def fetch_in_thread(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                init_client(self, self.config["api_id"], self.config["api_hash"], self.config["phone"]))
            groups = loop.run_until_complete(get_user_groups())
            self.root.after(0, self.update_fetched_groups_list_ui, groups)
        except Exception as e:
            self.root.after(0, self.show_thread_safe_message, "error", "Ошибка", f"Не удалось загрузить группы:\n{e}")
        finally:
            if client and client.is_connected(): loop.run_until_complete(client.disconnect())
            loop.close()
            self.root.after(0, lambda: self.fetch_btn.config(state='normal', text="🔄  Загрузить мои группы"))

    def add_fetched_groups(self):
        selections = self.fetched_groups_listbox.curselection()
        if not selections:
            return messagebox.showwarning("Внимание", "Выберите группы для добавления!")

        cabinet = self.cabinet_for_fetched.get().strip()
        if not cabinet:
            if not messagebox.askyesno("Подтверждение", "Номер кабинета не указан. Продолжить без номера?"):
                return

        added_count = 0
        for idx in selections:
            group_to_add = self.fetched_groups[idx]

            # Проверка на дубликаты
            is_duplicate = any(g['id'] == group_to_add['id'] for g in self.groups_data)
            if not is_duplicate:
                self.groups_data.append({
                    "id": group_to_add['id'],
                    "name": group_to_add['name'],
                    "cabinet": cabinet
                })
                added_count += 1

        if added_count > 0:
            save_groups_data(self.groups_data, self.themes_data)
            self.refresh_groups_list()
            self.refresh_sending_lists()
            messagebox.showinfo("Успех", f"Добавлено {added_count} новых групп!")
        else:
            messagebox.showinfo("Информация", "Все выбранные группы уже есть в списке.")

    def refresh_sending_lists(self):
        self.build_sending_lists(self.left_col_sending)

    def load_saved_config(self):
        for key, entry in [("api_id", self.api_id_entry), ("api_hash", self.api_hash_entry),
                           ("phone", self.phone_entry)]:
            if value := self.config.get(key):
                entry.delete(0, tk.END)
                entry.insert(0, value)
                entry.config(fg=self.colors['text'])

    def save_settings(self):
        api_id, api_hash, phone = self.api_id_entry.get().strip(), self.api_hash_entry.get().strip(), self.phone_entry.get().strip()
        if not all([api_id, api_hash, phone]): return messagebox.showwarning("Внимание", "Заполните все поля!")
        try:
            int(api_id)
        except ValueError:
            return messagebox.showerror("Ошибка", "API ID должен быть числом!")
        if not phone.startswith("+"): return messagebox.showwarning("Внимание", "Номер телефона должен начинаться с +")

        self.config = {"api_id": api_id, "api_hash": api_hash, "phone": phone}
        save_config(api_id, api_hash, phone)
        self.settings_status.config(text="✓ Настройки сохранены!", fg=self.colors['success'])
        messagebox.showinfo("Успех",
                            "Настройки сохранены! При следующем подключении будет создана новая сессия, если данные изменились.")

    def select_all(self):
        for var, _ in self.group_vars + self.theme_vars: var.set(True)

    def deselect_all(self):
        for var, _ in self.group_vars + self.theme_vars: var.set(False)

    def log(self, message):
        self.root.after(0, self._log_threadsafe, message)

    def _log_threadsafe(self, message):
        self.log_text.configure(state='normal')
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state='disabled')

    def prepare_send(self):
        if self.is_sending: return messagebox.showwarning("Внимание", "Отправка уже выполняется!")
        if not all(self.config.get(k) for k in ["api_id", "api_hash", "phone"]):
            messagebox.showwarning("Внимание", "Сначала настройте API ключи на вкладке 'Настройки'!")
            return self.notebook.select(0)
        selected_groups = [g for var, g in self.group_vars if var.get()]
        selected_themes = [t for var, t in self.theme_vars if var.get()]
        if not selected_groups and not selected_themes: return messagebox.showwarning("Внимание",
                                                                                      "Выберите хотя бы одну группу или тему!")
        if not (message := self.message_text.get("1.0", tk.END).strip()): return messagebox.showwarning("Внимание",
                                                                                                        "Введите текст сообщения!")
        self.show_confirmation_dialog(selected_groups, selected_themes, message)

    def show_confirmation_dialog(self, selected_groups, selected_themes, message):
        dialog = tk.Toplevel(self.root)
        dialog.title("Подтверждение отправки")
        dialog.geometry("700x600")
        dialog.configure(bg=self.colors['bg'])
        dialog.transient(self.root)
        dialog.grab_set()
        header = tk.Frame(dialog, bg=self.colors['warning'], height=60);
        header.pack(fill='x');
        header.pack_propagate(False)
        tk.Label(header, text="⚠️  Подтверждение отправки", bg=self.colors['warning'], fg='white',
                 font=('Segoe UI', 14, 'bold')).pack(pady=15)
        content = tk.Frame(dialog, bg=self.colors['bg']);
        content.pack(fill='both', expand=True, padx=20, pady=20)
        recipients_card = self.create_card(content, "👥  Получатели");
        recipients_card.pack(fill='both', expand=True, pady=(0, 15))
        recipients_text = scrolledtext.ScrolledText(recipients_card, height=10, font=('Segoe UI', 9),
                                                    bg=self.colors['input_bg'], fg=self.colors['text'], relief='solid',
                                                    bd=1)
        recipients_text.pack(fill='both', expand=True)
        recipients_text.insert(tk.END, "ГРУППЫ:\n", 'bold')
        for group in selected_groups: recipients_text.insert(tk.END,
                                                             f"  • {group['name']} (Каб. {group.get('cabinet', 'N/A')})\n")
        if selected_themes:
            recipients_text.insert(tk.END, "\nТЕМЫ:\n", 'bold')
            for theme in selected_themes: recipients_text.insert(tk.END,
                                                                 f"  • {theme['name']} (Каб. {theme.get('cabinet', 'N/A')})\n")
        recipients_text.insert(tk.END, f"\n📊 Всего получателей: {len(selected_groups) + len(selected_themes)}\n",
                               'bold')
        recipients_text.tag_config('bold', font=('Segoe UI', 9, 'bold'), foreground=self.colors['primary'])
        recipients_text.config(state='disabled')
        msg_card = self.create_card(content, "✉️  Текст сообщения");
        msg_card.pack(fill='both', expand=True, pady=(0, 15))
        msg_preview = scrolledtext.ScrolledText(msg_card, height=8, font=('Segoe UI', 9), bg=self.colors['input_bg'],
                                                fg=self.colors['text'], relief='solid', bd=1)
        msg_preview.pack(fill='both', expand=True);
        msg_preview.insert(tk.END, message);
        msg_preview.config(state='disabled')
        btn_frame = tk.Frame(dialog, bg=self.colors['bg']);
        btn_frame.pack(pady=20)
        self.create_modern_button(btn_frame, "✓  Подтверждаю, отправить",
                                  lambda: self.confirm_and_send(dialog, selected_groups, selected_themes, message),
                                  self.colors['success']).pack(side='left', padx=10)
        self.create_modern_button(btn_frame, "✗  Отмена", dialog.destroy, self.colors['danger']).pack(side='left',
                                                                                                      padx=10)
        dialog.update_idletasks()
        x = (self.root.winfo_x() + (self.root.winfo_width() // 2)) - (dialog.winfo_width() // 2)
        y = (self.root.winfo_y() + (self.root.winfo_height() // 2)) - (dialog.winfo_height() // 2)
        dialog.geometry(f"+{x}+{y}")

    def confirm_and_send(self, dialog, selected_groups, selected_themes, message):
        dialog.destroy()
        self.is_sending = True
        self.send_btn.config(state='disabled', text="⏳ Идет отправка...")
        self.log("🚀 Начинаю отправку сообщений...\n")
        threading.Thread(target=self.send_in_thread, args=(selected_groups, selected_themes, message),
                         daemon=True).start()

    def send_in_thread(self, selected_groups, selected_themes, message):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.log("🔐 Подключение к Telegram...")
            loop.run_until_complete(
                init_client(self, self.config["api_id"], self.config["api_hash"], self.config["phone"]))
            self.log("✓ Успешно подключено!\n")
            success, failed = loop.run_until_complete(
                send_messages(selected_groups, selected_themes, message, self.log))
            self.log(f"\n{'=' * 50}\n📊 ИТОГО: Успешно: {success} | Ошибок: {failed}\n{'=' * 50}\n")
            self.root.after(0, self.show_thread_safe_message, "info", "Готово",
                            f"Отправка завершена!\n\n✓ Успешно: {success}\n✗ Ошибок: {failed}")
        except Exception as e:
            self.log(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА: {e}\n")
            self.root.after(0, self.show_thread_safe_message, "error", "Ошибка", f"Произошла ошибка:\n{e}")
        finally:
            if client and client.is_connected(): loop.run_until_complete(client.disconnect())
            loop.close()
            self.is_sending = False
            self.root.after(0, lambda: self.send_btn.config(state='normal', text="📨  Отправить сообщения"))


# ============================================
# ЗАПУСК ПРИЛОЖЕНИЯ
# ============================================
if __name__ == "__main__":
    if sys.platform == 'win32':
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except AttributeError:
            pass

    root = tk.Tk()
    app = ThemedTelegramSenderApp(root)
    root.mainloop()
