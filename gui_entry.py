# gui_entry.py
import os, sys, traceback, datetime
from tkinter import messagebox, Tk

def main():
    # Defer heavy imports so we can show a messagebox if they fail
    from rvprospector.ui import run_ui_default
    run_ui_default()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Log to a per-user file
        logdir = os.path.join(os.path.expanduser("~"), ".rvprospector")
        os.makedirs(logdir, exist_ok=True)
        log_path = os.path.join(logdir, "error.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n=== " + datetime.datetime.now().isoformat() + " ===\n")
            traceback.print_exc(file=f)

        # Show a friendly popup
        try:
            root = Tk(); root.withdraw()
            messagebox.showerror("RV Prospector error",
                                 f"Sorry, something went wrong.\n\nDetails were written to:\n{log_path}")
        finally:
            try: root.destroy()
            except: pass
        sys.exit(1)
