"""SQLite session, reading, marker, and CSV persistence support."""

import csv
import os
import re
import sqlite3
from datetime import datetime
from tkinter import filedialog, messagebox


class DatabaseMixin:
    def init_database(self):
        db_path = os.path.join(self.base_dir, self.DB_FILE_NAME)
        self.db_conn = sqlite3.connect(db_path)
        self.db_conn.execute("PRAGMA journal_mode=WAL")
        reading_columns_sql = ",\n                ".join(
            "{0} TEXT".format(col_name) for col_name in self.reading_column_names()
        )
        self.db_conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                ended_at   TEXT,
                name       TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS readings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                timestamp  TEXT NOT NULL,
                {0}
            );
            CREATE TABLE IF NOT EXISTS markers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                timestamp  TEXT NOT NULL,
                action     TEXT NOT NULL,
                note       TEXT
            );
        """.format(reading_columns_sql))
        cols = [r[1] for r in self.db_conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "name" not in cols:
            self.db_conn.execute("ALTER TABLE sessions ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        reading_cols = {r[1] for r in self.db_conn.execute("PRAGMA table_info(readings)").fetchall()}
        for col_name in self.reading_column_names():
            if col_name not in reading_cols:
                self.db_conn.execute("ALTER TABLE readings ADD COLUMN {0} TEXT".format(col_name))
        self.db_conn.commit()



    def start_db_session(self):
        cur = self.db_conn.execute(
            "INSERT INTO sessions (started_at) VALUES (?)",
            (datetime.now().isoformat(),)
        )
        self.db_conn.commit()
        self.db_session_id = cur.lastrowid
        self.last_session_id = self.db_session_id


    def end_db_session(self):
        if self.db_session_id is not None:
            self.db_conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (datetime.now().isoformat(), self.db_session_id)
            )
            self.db_conn.commit()


    def save_to_db(self, timestamp):
        if self.db_session_id is None:
            return
        try:
            t = []
            h = []
            for i in range(self.CHANNEL_COUNT):
                if self.channel_record_enabled[i]:
                    t.append(self.current_temps[i])
                    h.append(self.current_hums[i])
                else:
                    t.append("")
                    h.append("")
            reading_columns = self.reading_column_names()
            placeholders = ",".join("?" for _ in range(2 + len(reading_columns)))
            insert_columns = ", ".join(["session_id", "timestamp"] + reading_columns)
            reading_values = []
            for i in range(self.CHANNEL_COUNT):
                reading_values.extend([t[i], h[i]])
            self.db_conn.execute(
                "INSERT INTO readings ({0}) VALUES ({1})".format(insert_columns, placeholders),
                [self.db_session_id, timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")] + reading_values
            )
            self.db_conn.commit()
        except Exception as ex:
            self.append_console("DB write error: {0}".format(ex))


    def _save_marker_to_db(self, action, dt, note):
        if self.db_session_id is None:
            return
        try:
            self.db_conn.execute(
                "INSERT INTO markers (session_id, timestamp, action, note) VALUES (?,?,?,?)",
                (self.db_session_id, dt.strftime("%Y-%m-%dT%H:%M:%S.%f"), action, note)
            )
            self.db_conn.commit()
        except Exception as ex:
            self.append_console("Marker DB write error: {0}".format(ex))


    def _csv_header(self, kind):
        header = ["Timestamp", "Marker", "Marker Text"]
        suffix = "T" if kind == "temp" else "H"
        for i in range(self.CHANNEL_COUNT):
            header.append("CH{0}_{1}".format(i, suffix))
        return header


    def session_has_data(self, session_id):
        row = self.db_conn.execute(
            """SELECT
                   (SELECT COUNT(*) FROM readings WHERE session_id = ?) +
                   (SELECT COUNT(*) FROM markers  WHERE session_id = ?)""",
            (session_id, session_id)
        ).fetchone()
        return bool(row and row[0] > 0)


    def iter_session_rows(self, session_id, kind):
        reading_columns_sql = ", ".join(self.reading_column_names())
        readings_cur = self.db_conn.execute(
            """SELECT timestamp, {0}
               FROM readings
               WHERE session_id = ?
               ORDER BY timestamp""".format(reading_columns_sql),
            (session_id,)
        )
        markers_cur = self.db_conn.execute(
            "SELECT timestamp, action, COALESCE(note, '') FROM markers WHERE session_id = ? ORDER BY timestamp",
            (session_id,)
        )

        r = readings_cur.fetchone()
        m = markers_cur.fetchone()
        empty_channels = tuple("" for _ in range(self.CHANNEL_COUNT))
        value_offset = 1 if kind == "hum" else 0

        while r is not None or m is not None:
            r_dt = self._parse_db_timestamp(r[0]) if r is not None else None
            m_dt = self._parse_db_timestamp(m[0]) if m is not None else None

            use_reading = False
            if r is not None and m is None:
                use_reading = True
            elif r is not None and m is not None:
                if r_dt is None:
                    use_reading = False
                elif m_dt is None:
                    use_reading = True
                else:
                    use_reading = r_dt <= m_dt

            if use_reading:
                values = []
                for i in range(self.CHANNEL_COUNT):
                    values.append(r[1 + 2 * i + value_offset])
                yield (r[0], "", "") + tuple(values)
                r = readings_cur.fetchone()
            else:
                marker_ts = m[0].replace("T", " ")
                yield (marker_ts, m[1], m[2]) + empty_channels
                m = markers_cur.fetchone()


    def export_session_csv(self, show_dialog=True):
        """Export current session from SQLite to a timestamped CSV file."""
        session_id = self.db_session_id if self.db_session_id is not None else self.last_session_id
        if session_id is None:
            if show_dialog:
                messagebox.showwarning("Export", "No session to export.")
            return
        self.export_session_csv_by_id(session_id, show_dialog=show_dialog)


    def export_session_csv_by_id(self, session_id, show_dialog=True):
        if session_id is None:
            if show_dialog:
                messagebox.showwarning("Export", "No session selected.")
            return

        if not self.session_has_data(session_id):
            if show_dialog:
                messagebox.showinfo("Export", "No data recorded in this session.")
            return

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        session_name = self.get_session_name(session_id)
        safe_name = self._safe_filename_part(session_name)
        if safe_name:
            base_name = "log_{0}_{1}_{2}".format(session_id, safe_name, ts)
        else:
            base_name = "log_{0}_{1}".format(session_id, ts)
        temp_filepath = os.path.join(self.base_dir, base_name + "_temperature.csv")
        hum_filepath = os.path.join(self.base_dir, base_name + "_humidity.csv")

        if show_dialog:
            chosen = filedialog.asksaveasfilename(
                title="Save Session CSV",
                initialdir=self.base_dir,
                initialfile=base_name + "_temperature.csv",
                defaultextension=".csv",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
            )
            if not chosen:
                return
            chosen_base, chosen_ext = os.path.splitext(chosen)
            if not chosen_ext:
                chosen_ext = ".csv"
            if chosen_base.endswith("_temperature"):
                chosen_base = chosen_base[:-12]
            elif chosen_base.endswith("_humidity"):
                chosen_base = chosen_base[:-9]
            temp_filepath = chosen_base + "_temperature" + chosen_ext
            hum_filepath = chosen_base + "_humidity" + chosen_ext

        try:
            with open(temp_filepath, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(self._csv_header("temp"))
                for row in self.iter_session_rows(session_id, "temp"):
                    w.writerow(row)
            with open(hum_filepath, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(self._csv_header("hum"))
                for row in self.iter_session_rows(session_id, "hum"):
                    w.writerow(row)

            self.append_console(">>> Exported to: {0}".format(os.path.basename(temp_filepath)))
            self.append_console(">>> Exported to: {0}".format(os.path.basename(hum_filepath)))
            if show_dialog:
                messagebox.showinfo("Export Complete",
                                    "Session data saved to:\n{0}\n{1}".format(temp_filepath, hum_filepath))
        except Exception as ex:
            self.append_console("Export error: {0}".format(ex))
            if show_dialog:
                messagebox.showerror("Export Error", str(ex))


    def append_session_to_data_csv(self, session_id):
        if not self.session_has_data(session_id):
            return
        try:
            temp_filepath = os.path.join(self.base_dir, self.session_output_filename(session_id, "temp"))
            hum_filepath = os.path.join(self.base_dir, self.session_output_filename(session_id, "hum"))

            with open(temp_filepath, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(self._csv_header("temp"))
                for row in self.iter_session_rows(session_id, "temp"):
                    w.writerow(row)
            with open(hum_filepath, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(self._csv_header("hum"))
                for row in self.iter_session_rows(session_id, "hum"):
                    w.writerow(row)
            self.append_console(">>> Saved to: {0}".format(os.path.basename(temp_filepath)))
            self.append_console(">>> Saved to: {0}".format(os.path.basename(hum_filepath)))
        except Exception as ex:
            self.append_console("CSV write error: {0}".format(ex))


    def get_sessions(self):
        return self.db_conn.execute(
            """SELECT s.id,
                      COALESCE(s.name, ''),
                      s.started_at,
                      COALESCE(s.ended_at, ''),
                      (SELECT COUNT(*) FROM readings r WHERE r.session_id = s.id) AS reading_count,
                      (SELECT COUNT(*) FROM markers m WHERE m.session_id = s.id) AS marker_event_count
               FROM sessions s
               ORDER BY s.id DESC"""
        ).fetchall()


    def get_session_name(self, session_id):
        row = self.db_conn.execute("SELECT COALESCE(name, '') FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return (row[0] if row else "").strip()


    def get_session_time_range(self, session_id):
        row = self.db_conn.execute(
            "SELECT started_at, COALESCE(ended_at, '') FROM sessions WHERE id = ?",
            (session_id,)
        ).fetchone()
        if row is None:
            return None, None
        started_at = self._parse_db_timestamp(row[0]) if row[0] else None
        ended_at = self._parse_db_timestamp(row[1]) if row[1] else None
        return started_at, ended_at


    def _format_filename_datetime(self, dt):
        if dt is None:
            return "unknown"
        return dt.strftime("%Y-%m-%d_%H-%M-%S")


    def session_output_filename(self, session_id, kind):
        started_at, ended_at = self.get_session_time_range(session_id)
        configured_name = self.TEMP_DATA_FILE_NAME if kind == "temp" else self.HUM_DATA_FILE_NAME
        stem, ext = os.path.splitext(configured_name)
        safe_stem = self._safe_filename_part(stem) or ("data_temperature" if kind == "temp" else "data_humidity")
        ext = ext or ".csv"
        return "{0}_{1}_to_{2}{3}".format(
            safe_stem,
            self._format_filename_datetime(started_at),
            self._format_filename_datetime(ended_at),
            ext
        )


    def _safe_filename_part(self, text):
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "").strip())
        return cleaned.strip("._")


    def sanity_check_session_counter(self):
        count_row = self.db_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        if not count_row or count_row[0] != 0:
            return
        try:
            self.db_conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('sessions','readings','markers')")
            self.db_conn.commit()
        except Exception:
            pass

    # UI

