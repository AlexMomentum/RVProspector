import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox
from .core import load_api_key, save_api_key, generate_daily, DEFAULT_DAILY_TARGET

LOCATION_CHOICES = [
    "Charlotte, NC", "Atlanta, GA", "Raleigh, NC", "Greenville, SC",
    "Columbia, SC", "Savannah, GA", "Jacksonville, FL", "Nashville, TN",
    "Dallas, TX", "Houston, TX", "Phoenix, AZ", "Denver, CO", "Custom…"
]

def run_ui_default():
    root = tk.Tk()
    root.title("RV Prospector")
    pad = {"padx": 8, "pady": 6}

    detected_key = load_api_key()
    api_var = tk.StringVar(value="")
    show_api_row = (detected_key == "")
    row_idx = 0

    # --- API key (first run only) ---
    if show_api_row:
        tk.Label(root, text="Google Places API Key:").grid(row=row_idx, column=0, sticky="w", **pad)
        api_entry = tk.Entry(root, textvariable=api_var, width=48, show="•")
        api_entry.grid(row=row_idx, column=1, columnspan=2, sticky="we", **pad)

        show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            root, text="Show", variable=show_var,
            command=lambda: api_entry.config(show="" if show_var.get() else "•")
        ).grid(row=row_idx, column=3, sticky="w", **pad)
        row_idx += 1

    # --- Near me checkbox (controls location widgets) ---
    nearme_var = tk.BooleanVar(value=True)
    tk.Checkbutton(root,
        text="Use my approximate location (near me)",
        variable=nearme_var
    ).grid(row=row_idx, column=0, columnspan=3, sticky="w", **pad)

    # --- Location dropdown (disabled when near-me is ON) ---
    row_idx += 1
    tk.Label(root, text="Location:").grid(row=row_idx, column=0, sticky="w", **pad)
    loc_var = tk.StringVar(value=LOCATION_CHOICES[0])
    loc_combo = ttk.Combobox(root, textvariable=loc_var, values=LOCATION_CHOICES,
                             state="readonly", width=45)
    loc_combo.grid(row=row_idx, column=1, columnspan=2, sticky="we", **pad)

    # Custom location (only when "Custom…" selected and near-me is OFF)
    custom_loc_var = tk.StringVar(value="")
    custom_label = tk.Label(root, text="Custom Location:")
    custom_entry = tk.Entry(root, textvariable=custom_loc_var, width=48)

    def refresh_location_widgets():
        if nearme_var.get():
            loc_combo.configure(state="disabled")
            custom_label.grid_forget()
            custom_entry.grid_forget()
        else:
            loc_combo.configure(state="readonly")
            if loc_var.get() == "Custom…":
                custom_label.grid(row=row_idx, column=0, sticky="w", **pad)
                custom_entry.grid(row=row_idx, column=1, columnspan=2, sticky="we", **pad)
            else:
                custom_label.grid_forget()
                custom_entry.grid_forget()

    def on_loc_change(*_):
        refresh_location_widgets()

    loc_var.trace_add("write", on_loc_change)
    nearme_var.trace_add("write", on_loc_change)
    refresh_location_widgets()

    # --- Target ---
    row_idx += 1
    tk.Label(root, text="New Prospects:").grid(row=row_idx, column=0, sticky="w", **pad)
    target_var = tk.IntVar(value=DEFAULT_DAILY_TARGET)
    tk.Spinbox(root, from_=1, to=200, textvariable=target_var, width=10)\
        .grid(row=row_idx, column=1, sticky="w", **pad)

    # --- Avoid conglomerates ---
    row_idx += 1
    avoid_var = tk.BooleanVar(value=True)
    tk.Checkbutton(root,
        text="Avoid conglomerates (KOA, Sun Outdoors, etc.)",
        variable=avoid_var
    ).grid(row=row_idx, column=0, columnspan=3, sticky="w", **pad)

    # --- Buttons ---
    row_idx += 1
    btn_frame = tk.Frame(root)
    btn_frame.grid(row=row_idx, column=0, columnspan=4, sticky="e", **pad)

    # ---- loading modal helpers ----
    def show_loading_modal():
        win = tk.Toplevel(root)
        win.title("Finding prospects…")
        win.transient(root)
        win.grab_set()  # modal
        tk.Label(win, text="Working… this can take a minute.").pack(padx=12, pady=(12, 6), anchor="w")
        pb = ttk.Progressbar(win, mode="indeterminate")
        pb.pack(fill="x", padx=12, pady=6)
        pb.start(10)

        # live log area
        log_var = tk.StringVar(value="")
        log = tk.Label(win, textvariable=log_var, justify="left", anchor="w")
        log.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        # position in center
        win.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - win.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - win.winfo_height()) // 2
        try:
            win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            pass

        return win, pb, log_var

    def on_run():
        api = detected_key
        if show_api_row:
            api = api_var.get().strip()
            if not api:
                messagebox.showerror("Missing API Key", "Please paste your Google Places API key.")
                return
            try:
                save_api_key(api, prefer="user")
            except Exception as e:
                messagebox.showwarning("Warning", f"Couldn't write .env: {e}")

        # compute location
        use_near_me = bool(nearme_var.get())
        loc = ""
        if not use_near_me:
            loc = loc_var.get()
            if loc == "Custom…":
                loc = custom_loc_var.get().strip()
                if not loc:
                    messagebox.showerror("Missing Location", "Enter a custom location.")
                    return

        tgt = max(1, min(200, int(target_var.get())))
        avoid = bool(avoid_var.get())

        # show modal
        modal, pb, log_var = show_loading_modal()

        # queue for progress messages
        q = queue.Queue()

        def progress_fn(msg: str):
            try:
                q.put_nowait(str(msg))
            except Exception:
                pass

        # background worker
        def worker():
            try:
                generate_daily(
                    api_key=api,
                    location_bias=loc or "Charlotte, NC",  # fallback ignored if near_me=True internally
                    daily_target=tgt,
                    avoid_conglomerates=avoid,
                    near_me=use_near_me,
                    radius_m=50000,
                    progress_fn=progress_fn
                )
            except Exception as e:
                q.put_nowait(f"[error] {e}")
            finally:
                q.put_nowait("__DONE__")

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        # poll queue & update UI
        def pump():
            try:
                while True:
                    msg = q.get_nowait()
                    if msg == "__DONE__":
                        pb.stop()
                        modal.grab_release()
                        modal.destroy()
                        root.deiconify()
                        messagebox.showinfo("RV Prospector", "Done! Your spreadsheet has been updated.")
                        return
                    # append message
                    prev = log_var.get()
                    log_var.set((prev + "\n" + msg).strip()[:4000])  # trim if very long
            except queue.Empty:
                pass
            root.after(120, pump)

        pump()  # start polling

    def on_cancel():
        root.destroy()

    tk.Button(btn_frame, text="Run", command=on_run).pack(side="right", padx=6)
    tk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="right")

    root.mainloop()
