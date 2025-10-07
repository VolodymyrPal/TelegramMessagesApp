# Telegram Messages App

**Telegram Messages App** helps you create a Telegram session, fetch your groups, set message themes, and send saved messages from your own account to selected groups.

---

## Command Line Usage

1. **Install PyInstaller**  
   If you don’t have PyInstaller yet, install it:
   ```bash
   pip install pyinstaller
   ```
   If you already have it, update to the latest version:
   ```bash
   pip install --upgrade pyinstaller
   ```

2. **Build the Executable**  
   Run the following command:
   ```bash
   pyinstaller --onefile --windowed --name "Telegram Messages App" --hidden-import Tcl --hidden-import Tk main.py
   ```
   After the process completes, open the **dist** directory to find the executable file.

---

## Setting Up API Access

1. Go to [my.telegram.org](https://my.telegram.org).  
2. Enter your phone number and complete authentication.  
3. Open **API Tools**, then copy your **API ID** and **API Hash** into the app.

---

## Using the Application

- Retrieve your groups and add the ones you need to your list.  
- Create **tags** to filter recipients.  
- Create and manage **cheatsheets** for quick message setups.  
- Choose the recipient, select or add messages, and send them with a time delay from the main screen.

---

**Thank you for using Telegram Messages App.**

This project is licensed under the MIT License - see the [LICENSE](License) file for details.
