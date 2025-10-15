import tkinter as tk
from tkinter import ttk, messagebox

from .core import (
    load_api_key, save_api_key, generate_daily, DEFAULT_DAILY_TARGET
)

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

    # API Key row (only if not already saved)
    if show_api_row:
        tk.Label(root, text="Google Places API Key:").grid(row=row_idx, column=0, sticky="w", **pad)
        api_entry = tk.Entry(root, textvariable=api_var, width=48, show="•")
        api_entry.grid(row=row_idx, column=1, columnspan=2, sticky="we", **pad)

        show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(root, text="Show", variable=show_var,
                       command=lambda: api_entry.config(show="" if show_var.get() else "•")).grid(
            row=row_idx, column=3, sticky="w", **pad)
        row_idx += 1

    # Near me toggle
    nearme_var = tk.BooleanVar(value=True)
    tk.Checkbutton(root, text="Near me (use IP location)", variable=nearme_var).grid(
        row=row_idx, column=0, columnspan=2, sticky="w", **pad
    )

    # Location (disabled when 'Near me' is ON)
    tk.Label(root, text="Location:").grid(row=row_idx, column=2, sticky="e", **pad)
    loc_var = tk.StringVar(value=LOCATION_CHOICES[0])
    loc_combo = ttk.Combobox(root, textvariable=loc_var, values=LOCATION_CHOICES, state="disabled", width=32)
    loc_combo.grid(row=row_idx, column=3, sticky="we", **pad)
    row_idx += 1

    # Custom location entry (only when "Custom…" is chosen, and Near me is OFF)
    custom_loc_var = tk.StringVar(value="")
    custom_label = tk.Label(root, text="Custom Location:")
    custom_entry = tk.Entry(root, textvariable=custom_loc_var, width=48)

    def on_loc_change(*_):
        if nearme_var.get():
            custom_label.grid_forget()
            custom_entry.grid_forget()
            return
        if loc_var.get() == "Custom…":
            custom_label.grid(row=row_idx, column=0, sticky="w", **pad)
            custom_entry.grid(row=row_idx, column=1, columnspan=3, sticky="we", **pad)
        else:
            custom_label.grid_forget()
            custom_entry.grid_forget()

    def on_nearme_toggle():
        if nearme_var.get():
            loc_combo.configure(state="disabled")
            custom_label.grid_forget()
            custom_entry.grid_forget()
        else:
            loc_combo.configure(state="readonly")
            on_loc_change()

    loc_var.trace_add("write", on_loc_change)
    nearme_var.trace_add("write", lambda *_: on_nearme_toggle())
    on_nearme_toggle()

    row_idx += 1

    # Target spinbox
    tk.Label(root, text="New Prospects:").grid(row=row_idx, column=0, sticky="w", **pad)
    target_var = tk.IntVar(value=DEFAULT_DAILY_TARGET)
    tk.Spinbox(root, from_=1, to=200, textvariable=target_var, width=10).grid(
        row=row_idx, column=1, sticky="w", **pad
    )

    # Avoid conglomerates
    avoid_var = tk.BooleanVar(value=True)
    tk.Checkbutton(root, text="Avoid conglomerates", variable=avoid_var).grid(
        row=row_idx, column=2, columnspan=2, sticky="w", **pad
    )
    row_idx += 1

    # Progress window helper
    def show_progress_window():
        win = tk.Toplevel(root)
        win.title("Finding Prospects…")
        win.geometry("720x420")
        txt = tk.Text(win, width=100, height=26, state="disabled")
        txt.pack(fill="both", expand=True, padx=8, pady=8)

        def append(line: str):
            txt.configure(state="normal")
            txt.insert("end", line + "\n")
            txt.see("end")
            txt.configure(state="disabled")
            win.update_idletasks()
        return win, append

    # Buttons
    btn_frame = tk.Frame(root)
    btn_frame.grid(row=row_idx, column=0, columnspan=4, sticky="e", **pad)

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

        # Location resolve
        near_me = bool(nearme_var.get())
        loc = ""
        if not near_me:
            loc = loc_var.get()
            if loc == "Custom…":
                loc = custom_loc_var.get().strip()
            if not loc:
                messagebox.showerror("Missing Location", "Enter a location or enable 'Near me'.")
                return

        # Target
        try:
            tgt = max(1, min(200, int(target_var.get())))
        except Exception:
            messagebox.showerror("Invalid Target", "Enter a number between 1 and 200.")
            return

        # Progress UI
        prog_win, emit = show_progress_window()

        def progress_fn(msg: str):
            emit(msg)

        # Run (UI stays open; we stream logs)
        try:
            generate_daily(
                api_key=api,
                location_bias=loc,
                daily_target=tgt,
                avoid_conglomerates=bool(avoid_var.get()),
                near_me=near_me,
                radius_m=50_000,
                progress_fn=progress_fn
            )
            emit("Done. Files saved to:")
            emit("  Windows: %USERPROFILE%\\.rvprospector\\")
            emit("  macOS:   ~/Library/Application Support/RVProspector/")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def on_cancel():
        root.destroy()

    tk.Button(btn_frame, text="Run", command=on_run).pack(side="right", padx=6)
    tk.Button(btn_frame, text="Close", command=on_cancel).pack(side="right")
    root.mainloop()
