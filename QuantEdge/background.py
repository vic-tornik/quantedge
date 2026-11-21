"""Coalesced portfolio work on a dedicated SQLite connection."""
import threading
from pathlib import Path

from engine import Journal
from exchange import Binance
from foundation import set_health


class BackgroundService:
    def __init__(self, journal, task, report):
        self.mode = journal.mode
        self.directory = Path(journal.db.execute("PRAGMA database_list").fetchone()["file"]).parent
        self.task = task
        self.report = report
        self.requested = threading.Event()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run,name="quantedge-portfolio",daemon=True)

    def start(self):
        self.thread.start()
        self.request()

    def request(self):
        # At most one pending refresh, even if persistence is slower than the feed.
        self.requested.set()

    def run(self):
        journal = None
        final_state = "stopped"
        detail = ""
        try:
            journal = Journal(self.mode,self.directory)
            set_health(journal,"worker","running")
            venue = Binance(self.mode)
            while not self.stopped.is_set():
                self.requested.wait()
                self.requested.clear()
                if self.stopped.is_set():
                    break
                self.task(journal,venue,self.report)
        except Exception as err:
            final_state = "error"
            detail = type(err).__name__
            self.report({"event":"portfolio_worker_failed","error_type":type(err).__name__})
        finally:
            if journal is not None:
                set_health(journal,"worker",final_state,detail)
                journal.db.close()

    def close(self):
        self.stopped.set()
        self.requested.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            self.report({"event":"portfolio_worker_shutdown_pending"})
