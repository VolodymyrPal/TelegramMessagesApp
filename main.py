import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from telethon import TelegramClient
from telethon.errors.rpcerrorlist import SessionPasswordNeededError
import asyncio
import threading
import json
import os
import sys
from typing import Optional, List, Dict, Any


# ============================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (ИСПРАВЛЕНО ДЛЯ СОХРАНЕНИЯ ДАННЫХ)
# ============================================
def get_persistent_path(filename: str) -> str:
    """
    Получает путь для пользовательских данных (конфигурации, сессий, контактов).
    Всегда использует текущую рабочую директорию, чтобы обеспечить сохранение
    файлов даже после закрытия скомпилированного приложения.
    """
    # Мы используем os.path.abspath(".") (текущий рабочий каталог),
    # чтобы избежать проблем с временными папками вроде sys._MEIPASS.
    return os.path.join(os.path.abspath("."), filename)


# ============================================
# КОНСТАНТЫ
# ============================================
# Теперь файлы конфигурации и контактов используют постоянный путь
CONFIG_FILE = get_persistent_path("config.json")
CONTACTS_FILE = get_persistent_path("contacts.json")
SESSION_FILE_NAME = 'session_ui'  # Имя файла сессии Telethon


class ThemedTelegramSenderApp:
    """
    Улучшенная версия приложения для отправки сообщений в Telegram с современным UI,
    редактором контактов и стабильной работой с сессиями.
    """

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Telegram Sender v2.3")
        self.root.geometry("1000x800")
        self.root.minsize(800, 600)

        # Клиент Telethon
        self.client: Optional[TelegramClient] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        # Состояние приложения
        self.is_sending = False
        self.is_connected = False
        # Загрузка конфигурации и контактов из постоянных путей
        self.config = self._load_json(CONFIG_FILE, default={"api_id": "", "api_hash": "", "phone": ""})
        self.contacts = self._load_json(CONTACTS_FILE, default={"groups": [], "themes": []})

        # Стилизация и виджеты
        self._setup_styles()
        self._create_widgets()
        self._load_config_into_ui()
        self._populate_contacts_tree()

        # Обработка закрытия окна
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _setup_styles(self) -> None:
        """Настройка стилей для виджетов ttk."""
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TLabel", padding=5, font=('Helvetica', 10))
        style.configure("TButton", padding=8, font=('Helvetica', 10, 'bold'))
        # Добавляем insertbackground для видимого курсора
        style.configure("TEntry", padding=5, insertbackground='black')
        style.configure("TNotebook.Tab", font=('Helvetica', 11, 'bold'), padding=[10, 5])
        style.configure("Treeview.Heading", font=('Helvetica', 10, 'bold'))
        style.configure("Treeview", rowheight=25, font=('Helvetica', 10))
        style.configure("Status.TLabel", font=('Helvetica', 10, 'italic'))
        style.configure("Success.TLabel", foreground="green")
        style.configure("Error.TLabel", foreground="red")
        style.configure("Info.TLabel", foreground="blue")

    def _create_widgets(self) -> None:
        """Создание и размещение всех виджетов в окне."""
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill="both", expand=True, pady=5)

        settings_tab = ttk.Frame(self.notebook, padding=10)
        sending_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(settings_tab, text="⚙️ Настройки")
        self.notebook.add(sending_tab, text="📤 Отправка")

        self._create_settings_tab(settings_tab)
        self._create_sending_tab(sending_tab)

    def _create_settings_tab(self, parent: ttk.Frame) -> None:
        """Создание вкладки с настройками API и подключением."""
        parent.columnconfigure(0, weight=1)

        api_frame = ttk.LabelFrame(parent, text="Настройки Telegram API", padding=20)
        api_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        api_frame.columnconfigure(1, weight=1)

        ttk.Label(api_frame, text="API ID:").grid(row=0, column=0, sticky="w", pady=5)
        self.api_id_entry = ttk.Entry(api_frame, width=40)
        self.api_id_entry.grid(row=0, column=1, sticky="ew")

        ttk.Label(api_frame, text="API Hash:").grid(row=1, column=0, sticky="w", pady=5)
        self.api_hash_entry = ttk.Entry(api_frame, width=40)
        self.api_hash_entry.grid(row=1, column=1, sticky="ew")

        ttk.Label(api_frame, text="Номер телефона:").grid(row=2, column=0, sticky="w", pady=5)
        self.phone_entry = ttk.Entry(api_frame, width=40)
        self.phone_entry.grid(row=2, column=1, sticky="ew")

        connection_frame = ttk.Frame(parent, padding=10)
        connection_frame.grid(row=1, column=0, sticky="ew")
        connection_frame.columnconfigure(0, weight=1)
        connection_frame.columnconfigure(1, weight=1)

        self.connect_btn = ttk.Button(connection_frame, text="🔌 Подключиться", command=self._toggle_connection)
        self.connect_btn.grid(row=0, column=0, sticky="e", padx=5)

        self.save_btn = ttk.Button(connection_frame, text="💾 Сохранить", command=self._save_settings)
        self.save_btn.grid(row=0, column=1, sticky="w", padx=5)

        self.status_label = ttk.Label(parent, text="Статус: Отключено", style="Status.TLabel")
        self.status_label.grid(row=2, column=0, sticky="ew", pady=10)

    def _create_sending_tab(self, parent: ttk.Frame) -> None:
        """Создание вкладки для выбора контактов и отправки сообщений."""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=3)
        parent.rowconfigure(2, weight=2)
        parent.rowconfigure(4, weight=1)

        contacts_frame = ttk.LabelFrame(parent, text="Выберите получателей", padding=10)
        contacts_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        contacts_frame.columnconfigure(0, weight=1)
        contacts_frame.rowconfigure(0, weight=1)

        cols = ("name", "cabinet", "type", "id")
        self.contacts_tree = ttk.Treeview(contacts_frame, columns=cols, show="headings", selectmode="extended")

        self.contacts_tree.heading("name", text="Название")
        self.contacts_tree.heading("cabinet", text="Кабинет")
        self.contacts_tree.heading("type", text="Тип")
        self.contacts_tree.heading("id", text="ID")
        self.contacts_tree.column("name", width=250)
        self.contacts_tree.column("cabinet", width=100)
        self.contacts_tree.column("type", width=80, anchor="center")
        self.contacts_tree.column("id", width=150)

        scrollbar = ttk.Scrollbar(contacts_frame, orient="vertical", command=self.contacts_tree.yview)
        self.contacts_tree.configure(yscrollcommand=scrollbar.set)
        self.contacts_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        selection_frame = ttk.Frame(parent)
        selection_frame.grid(row=1, column=0, sticky="w", pady=5)
        ttk.Button(selection_frame, text="✅ Выбрать все", command=self._select_all_contacts).pack(side="left", padx=5)
        ttk.Button(selection_frame, text="❌ Снять все", command=self._deselect_all_contacts).pack(side="left", padx=5)
        ttk.Button(selection_frame, text="➕ Добавить получателя", command=self._open_add_contact_dialog).pack(
            side="left", padx=5)

        msg_frame = ttk.LabelFrame(parent, text="Текст сообщения", padding=10)
        msg_frame.grid(row=2, column=0, sticky="nsew", pady=5)
        msg_frame.rowconfigure(0, weight=1)
        msg_frame.columnconfigure(0, weight=1)
        self.message_text = scrolledtext.ScrolledText(msg_frame, height=10, wrap=tk.WORD, font=('Helvetica', 10))
        # Устанавливаем видимый курсор. Нативные привязки работают по умолчанию.
        self.message_text.config(insertbackground='black')

        self.message_text.grid(row=0, column=0, sticky="nsew")
        self.message_text.insert("1.0", "Вітаю!\n\nЦе тестове повідомлення.")

        send_controls_frame = ttk.Frame(parent)
        send_controls_frame.grid(row=3, column=0, sticky="nsew", pady=10)
        send_controls_frame.columnconfigure(1, weight=1)

        self.send_btn = ttk.Button(send_controls_frame, text="📨 Отправить сообщения", command=self._start_sending)
        self.send_btn.grid(row=0, column=0, sticky="w")

        self.progress_bar = ttk.Progressbar(send_controls_frame, orient="horizontal", mode="determinate")
        self.progress_bar.grid(row=0, column=1, sticky="ew", padx=10)

        log_frame = ttk.LabelFrame(parent, text="Лог отправки", padding=10)
        log_frame.grid(row=4, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, state="disabled", font=('Courier New', 9))
        self.log_text.grid(row=0, column=0, sticky="nsew")

    def _open_add_contact_dialog(self):
        """Открывает диалоговое окно для добавления нового контакта."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Добавить получателя")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(expand=True, fill="both")

        fields = {}
        ttk.Label(frame, text="Тип:").grid(row=0, column=0, sticky="w", pady=5)
        fields['type'] = ttk.Combobox(frame, values=["Группа", "Тема"], state="readonly")
        fields['type'].grid(row=0, column=1, sticky="ew", pady=5)
        fields['type'].set("Группа")

        ttk.Label(frame, text="Название:").grid(row=1, column=0, sticky="w", pady=5)
        fields['name'] = ttk.Entry(frame)
        fields['name'].grid(row=1, column=1, sticky="ew", pady=5)

        ttk.Label(frame, text="Кабинет:").grid(row=2, column=0, sticky="w", pady=5)
        fields['cabinet'] = ttk.Entry(frame)
        fields['cabinet'].grid(row=2, column=1, sticky="ew", pady=5)

        id_label = ttk.Label(frame, text="ID Группы:")
        id_label.grid(row=3, column=0, sticky="w", pady=5)
        fields['id'] = ttk.Entry(frame)
        fields['id'].grid(row=3, column=1, sticky="ew", pady=5)

        group_id_label = ttk.Label(frame, text="ID Группы (для темы):")
        fields['group_id'] = ttk.Entry(frame, state="disabled")

        def on_type_change(event):
            if fields['type'].get() == "Тема":
                id_label.config(text="ID Темы:")
                group_id_label.grid(row=4, column=0, sticky="w", pady=5)
                fields['group_id'].grid(row=4, column=1, sticky="ew", pady=5)
                fields['group_id'].config(state="normal")
            else:
                id_label.config(text="ID Группы:")
                group_id_label.grid_forget()
                fields['group_id'].grid_forget()
                fields['group_id'].config(state="disabled")

        fields['type'].bind("<<ComboboxSelected>>", on_type_change)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=5, column=0, columnspan=2, pady=10)

        def save_contact():
            try:
                contact_type = fields['type'].get()
                name = fields['name'].get().strip()
                cabinet = fields['cabinet'].get().strip()

                if not all([contact_type, name, cabinet]):
                    messagebox.showerror("Ошибка", "Все поля должны быть заполнены.", parent=dialog)
                    return

                if contact_type == "Группа":
                    # Проверяем только ID на число, чтобы избежать ошибки при добавлении
                    group_id = int(fields['id'].get())
                    new_contact = {"id": group_id, "name": name, "cabinet": cabinet}
                    self.contacts["groups"].append(new_contact)
                else:  # Тема
                    topic_id = int(fields['id'].get())
                    group_id_for_topic = int(fields['group_id'].get())
                    new_contact = {
                        "group_id": group_id_for_topic, "topic_id": topic_id,
                        "name": name, "cabinet": cabinet
                    }
                    self.contacts["themes"].append(new_contact)

                self._save_json(CONTACTS_FILE, self.contacts)
                self._populate_contacts_tree()
                dialog.destroy()

            except ValueError:
                # В данном диалоге ID группы/темы/группы для темы должны быть числами
                messagebox.showerror("Ошибка", "ID должен быть числом.", parent=dialog)
            except Exception as e:
                messagebox.showerror("Ошибка", f"Не удалось сохранить контакт: {e}", parent=dialog)

        ttk.Button(btn_frame, text="Сохранить", command=save_contact).pack(side="left", padx=10)
        ttk.Button(btn_frame, text="Отмена", command=dialog.destroy).pack(side="left", padx=10)

    def _load_json(self, filename: str, default: Dict = None) -> Dict:
        if not os.path.exists(filename):
            self._save_json(filename, default or {})
            return default or {}
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            messagebox.showerror("Ошибка чтения файла", f"Не удалось прочитать {filename}. Будет создан новый.")
            self._save_json(filename, default or {})
            return default or {}

    def _save_json(self, filename: str, data: Dict) -> None:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    def _load_config_into_ui(self) -> None:
        self.api_id_entry.insert(0, self.config.get("api_id", ""))
        self.api_hash_entry.insert(0, self.config.get("api_hash", ""))
        self.phone_entry.insert(0, self.config.get("phone", ""))

    def _populate_contacts_tree(self) -> None:
        for item in self.contacts_tree.get_children():
            self.contacts_tree.delete(item)
        for group in self.contacts.get("groups", []):
            self.contacts_tree.insert("", "end", values=(group["name"], group["cabinet"], "Группа", group["id"]))
        for theme in self.contacts.get("themes", []):
            self.contacts_tree.insert("", "end", values=(theme["name"], theme["cabinet"], "Тема", theme["topic_id"]))

    def _select_all_contacts(self) -> None:
        for child in self.contacts_tree.get_children():
            self.contacts_tree.selection_add(child)

    def _deselect_all_contacts(self) -> None:
        self.contacts_tree.selection_remove(self.contacts_tree.selection())

    def _update_status(self, text: str, style: str = "Status.TLabel") -> None:
        self.status_label.config(text=f"Статус: {text}", style=style)

    def _log(self, message: str) -> None:
        def append_message():
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")

        self.root.after(0, append_message)

    def _update_progress(self, value: int, maximum: int) -> None:
        def set_progress():
            self.progress_bar['maximum'] = maximum
            self.progress_bar['value'] = value

        self.root.after(0, set_progress)

    def _save_settings(self) -> bool:
        """Сохраняет настройки API как строки."""
        api_id = self.api_id_entry.get().strip()
        api_hash = self.api_hash_entry.get().strip()
        phone = self.phone_entry.get().strip()

        if not all([api_id, api_hash, phone]):
            messagebox.showwarning("Внимание", "Заполните все поля API!")
            return False

        # Сохраняем как строки, как было запрошено. Проверка на число будет при подключении.
        self.config = {"api_id": api_id, "api_hash": api_hash, "phone": phone}
        self._save_json(CONFIG_FILE, self.config)
        self._update_status("Настройки сохранены.", "Success.TLabel")
        return True

    def _toggle_connection(self) -> None:
        if self.is_connected:
            threading.Thread(target=self._threaded_disconnect, daemon=True).start()
        else:
            if self._save_settings():
                threading.Thread(target=self._threaded_connect, daemon=True).start()

    def _threaded_connect(self) -> None:
        self.root.after(0, lambda: self._update_status("Подключение...", "Info.TLabel"))
        self.root.after(0, lambda: self.connect_btn.config(state="disabled"))
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        # Используем постоянный путь для файла сессии Telethon
        session_path = get_persistent_path(SESSION_FILE_NAME)

        # API ID здесь обязательно должен быть числом, иначе TelegramClient выдаст ошибку.
        try:
            api_id_int = int(self.config["api_id"])
        except ValueError:
            self.root.after(0, lambda: self._update_status("Ошибка: API ID должно быть числом!", "Error.TLabel"))
            self.root.after(0, lambda: self.connect_btn.config(state="normal"))
            return

        self.client = TelegramClient(session_path, api_id_int, self.config["api_hash"], loop=self.loop)

        try:
            self.loop.run_until_complete(self.client.connect())

            # --- Логика авторизации ---
            if not self.loop.run_until_complete(self.client.is_user_authorized()):
                self.loop.run_until_complete(self.client.send_code_request(self.config["phone"]))

                # Ввод кода (блокирует поток)
                code = self.prompt_for_input("Введите код из Telegram:")
                if not code:
                    raise Exception("Авторизация отменена пользователем.")

                try:
                    self.loop.run_until_complete(self.client.sign_in(self.config["phone"], code))
                except SessionPasswordNeededError:
                    # Ввод пароля 2FA (блокирует поток)
                    password = self.prompt_for_input("Введите пароль двухфакторной аутентификации:")
                    if not password:
                        raise Exception("Авторизация отменена пользователем.")
                    self.loop.run_until_complete(self.client.sign_in(password=password))

            # --- Успешное подключение ---
            me = self.loop.run_until_complete(self.client.get_me())
            self.is_connected = True
            self.root.after(0, lambda: self._update_status(f"Подключено как {me.first_name}", "Success.TLabel"))
            self.root.after(0, lambda: self.connect_btn.config(text="🔌 Отключиться"))

        except Exception as e:
            # Сюда попадет и исходная struct.error, если ID слишком большой
            self.is_connected = False
            error_message = str(e)
            if "struct.error" in error_message and "number <= 2147483647" in error_message:
                error_message = "API ID слишком большой. Максимально допустимое значение: 2147483647."

            self.root.after(0, lambda err=error_message: self._update_status(f"Ошибка: {err}", "Error.TLabel"))
            # В случае неудачи, пытаемся отключиться, чтобы корректно завершить клиент
            try:
                if self.client:
                    self.loop.run_until_complete(self.client.disconnect())
            except Exception:
                pass
        finally:
            self.root.after(0, lambda: self.connect_btn.config(state="normal"))

    def _threaded_disconnect(self) -> None:
        self.root.after(0, lambda: self._update_status("Отключение...", "Info.TLabel"))
        self.root.after(0, lambda: self.connect_btn.config(state="disabled"))
        if self.client and self.loop:
            self.loop.run_until_complete(self.client.disconnect())
        self.is_connected = False
        self.root.after(0, lambda: self._update_status("Отключено", "Status.TLabel"))
        self.root.after(0, lambda: self.connect_btn.config(text="🔌 Подключиться", state="normal"))

    def _start_sending(self) -> None:
        if self.is_sending:
            messagebox.showwarning("Внимание", "Отправка уже выполняется!")
            return
        if not self.is_connected:
            messagebox.showerror("Ошибка", "Необходимо подключиться к Telegram!")
            self.notebook.select(0)
            return
        selected_items = self.contacts_tree.selection()
        if not selected_items:
            messagebox.showwarning("Внимание", "Выберите хотя бы одного получателя!")
            return
        message = self.message_text.get("1.0", tk.END).strip()
        if not message:
            messagebox.showwarning("Внимание", "Введите текст сообщения!")
            return
        self.is_sending = True
        self.send_btn.config(state="disabled")
        self._log("🚀 Начинаю отправку...")
        threading.Thread(target=self._threaded_send, args=(selected_items, message), daemon=True).start()

    def _threaded_send(self, selected_items: List[str], message: str) -> None:
        success, failed = 0, 0
        total = len(selected_items)
        self._update_progress(0, total)
        for i, item_id in enumerate(selected_items):
            values = self.contacts_tree.item(item_id, "values")
            name, _, item_type, entity_id = values
            try:
                full_contact = None
                # Используем str(entity_id) для поиска, так как в values ID всегда строка
                if item_type == "Группа":
                    full_contact = next((g for g in self.contacts['groups'] if str(g['id']) == entity_id), None)
                elif item_type == "Тема":
                    full_contact = next((t for t in self.contacts['themes'] if str(t['topic_id']) == entity_id), None)
                if not full_contact:
                    raise ValueError("Контакт не найден в файле contacts.json")
                self.loop.run_until_complete(self._send_single_message(full_contact, message))
                self._log(f"✅ Отправлено в '{name}'")
                success += 1
            except Exception as e:
                self._log(f"❌ Ошибка отправки в '{name}': {e}")
                failed += 1
            self._update_progress(i + 1, total)
            self.loop.run_until_complete(asyncio.sleep(10))
        self._log(f"\n📊 Готово! Успешно: {success}, Ошибок: {failed}")
        self.root.after(0, lambda: messagebox.showinfo("Готово",
                                                       f"Отправка завершена!\nУспешно: {success}\nОшибок: {failed}"))
        self.is_sending = False
        self.root.after(0, lambda: self.send_btn.config(state="normal"))
        self._update_progress(0, total)

    async def _send_single_message(self, contact: Dict, message: str) -> None:
        # ID в контактах (групп и тем) хранятся как числа, поэтому не требуется int()
        if 'topic_id' in contact:
            await self.client.send_message(entity=contact["group_id"], message=message, reply_to=contact["topic_id"])
        else:
            await self.client.send_message(contact["id"], message)

    def _on_closing(self) -> None:
        if self.is_sending:
            if not messagebox.askyesno("Подтверждение", "Идет отправка сообщений. Вы уверены, что хотите выйти?"):
                return
        if self.is_connected:
            self._log("Отключаюсь от Telegram перед выходом...")
            # Запускаем отключение в отдельном потоке, чтобы не блокировать GUI
            threading.Thread(target=self._threaded_disconnect, daemon=True).start()
        self.root.destroy()

    def prompt_for_input(self, prompt_text: str) -> str:
        result = tk.StringVar()

        def create_dialog():
            dialog = tk.Toplevel(self.root)
            dialog.title("Требуется ввод")
            dialog.transient(self.root)
            dialog.grab_set()

            # Центрирование диалога относительно родительского окна
            parent_x = self.root.winfo_x()
            parent_y = self.root.winfo_y()
            parent_width = self.root.winfo_width()
            parent_height = self.root.winfo_height()
            dialog_width = 300
            dialog_height = 150
            x = parent_x + (parent_width // 2) - (dialog_width // 2)
            y = parent_y + (parent_height // 2) - (dialog_height // 2)
            dialog.geometry(f"{dialog_width}x{dialog_height}+{x}+{y}")
            dialog.update_idletasks()  # Обновляем для корректного центрирования

            frame = ttk.Frame(dialog, padding=10)
            frame.pack(fill="both", expand=True)

            ttk.Label(frame, text=prompt_text, wraplength=dialog_width - 20).pack(padx=10, pady=(10, 5))
            entry = ttk.Entry(frame)
            entry.pack(padx=10, pady=5, fill="x")
            entry.focus_set()

            def on_ok():
                result.set(entry.get())
                dialog.destroy()

            ttk.Button(frame, text="OK", command=on_ok).pack(pady=10)
            self.root.wait_window(dialog)

        self.root.after(0, create_dialog)
        self.root.wait_variable(result)
        return result.get()


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
