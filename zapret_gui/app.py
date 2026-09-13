"""Zapret Control GUI — hosts elevation, strategy services, in-app lists editor."""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QSemaphore, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from zapret_gui import HOSTS_PATH, PROJECT_NAME, SERVICE_NAME, __version__
from zapret_gui.diagnostics import DiagLine, default_probes, run_diagnostics
from zapret_gui.i18n import apply_locale, load_catalog, lookup, toggle_locale
from zapret_gui.identity import persist_installed_strategy, strategy_stem
from zapret_gui.lists_io import (
    ListsError,
    backup_all_lists,
    backup_list,
    list_files,
    read_list,
    write_list,
)
from zapret_gui.paths import ProjectRootError, detect_project_root
from zapret_gui.privileges import (
    HostsLaunchResult,
    is_process_elevated,
    launch_hosts_notepad,
    relaunch_self_elevated,
)
from zapret_gui.services import (
    RemoveResult,
    ScmOpResult,
    ServiceSnapshot,
    StepReport,
    image_install_root,
    image_is_under,
    install_service,
    query_service_snapshot,
    remove_service,
)
from zapret_gui.strategies import (
    GAME_FILTER_MODES,
    ParsedStrategy,
    StrategyError,
    discover_strategies,
    load_game_filter,
    parse_strategy,
    save_game_filter,
)
from zapret_gui.theme import CONSOLE_FONT_FAMILY, GUI_FONT_FAMILY, PALETTE, STYLESHEET
from zapret_gui.tools import (
    POWERSHELL_REQUIRED,
    RESULTS_DIR,
    TESTS_STARTING,
    find_best_strategy_line,
    launch_tests,
    newest_result_since,
    powershell_supported,
)

REQUIRED_OBJECT_NAMES = (
    "hostsButton",
    "strategyCombo",
    "installButton",
    "removeButton",
    "testsButton",
    "diagnosticsButton",
    "testsHint",
    "serviceStatusLabel",
    "gameFilterCombo",
    "listFileList",
    "listEditor",
    "backupButton",
    "saveButton",
    "languageButton",
    "consoleView",
    "clearConsoleButton",
)

GAME_FILTER_LABELS = (
    ("disabled", "Disabled (port 12)"),
    ("all", "TCP and UDP"),
    ("tcp", "TCP only"),
    ("udp", "UDP only"),
)

# service.bat paints its console with :PrintGreen / :PrintYellow / :PrintRed.
CONSOLE_OK = PALETTE["success"]
CONSOLE_WARN = PALETTE["attention"]
CONSOLE_FAIL = PALETTE["danger"]
CONSOLE_CMD = PALETTE["accent"]
CONSOLE_MUTED = PALETTE["fg_muted"]
CONSOLE_TEXT = PALETTE["fg"]

# The resolved binPath runs past 4000 characters; the full text already lives in
# the args preview, so the console shows a readable head of it.
CONSOLE_COMMAND_LIMIT = 320

# The header status line re-reads the SCM this often. One QueryServiceStatusEx
# call costs ~0.1 ms, against ~35 ms and a process launch for `sc query`.
STATUS_POLL_MS = 2000

# SCM state token -> (lang-file key, fallback text, QSS state)
STATUS_TOKENS: dict[str, tuple[str, str, str]] = {
    "RUNNING": ("statusRunning", "RUNNING", "running"),
    "START_PENDING": ("statusStarting", "STARTING…", "pending"),
    "CONTINUE_PENDING": ("statusStarting", "STARTING…", "pending"),
    "STOP_PENDING": ("statusStopping", "STOPPING…", "pending"),
    "PAUSE_PENDING": ("statusStopping", "STOPPING…", "pending"),
    "STOPPED": ("statusStopped", "STOPPED", "stopped"),
    "PAUSED": ("statusStopped", "STOPPED", "stopped"),
    "NOT_INSTALLED": ("statusNotInstalled", "NOT INSTALLED", "not_installed"),
}
STATUS_ERROR = ("statusError", "ERROR", "error")

TESTS_HINT = "Don't know what strategy to use? Run tests and it will choose the best strategy for you!"

# The results file lands at the very end of a run; if the watcher fires before
# its `Best strategy:` line is written, look again shortly.
RESULTS_RETRY_MS = 500
RESULTS_RETRIES = 10
# File times come from a coarser clock than time.time(); do not miss a file
# stamped a moment "before" the launch was recorded.
RESULTS_CLOCK_SLACK_S = 2.0

# Dialog text when the lang-file lacks a key; service.bat's own prompts.
PROMPT_FALLBACKS = {
    "diagConflictsPrompt": "Do you want to remove these conflicting services?",
    "diagDiscordPrompt": "Do you want to clear the Discord cache (Stable, PTB, Canary, Development)?",
}


class WorkerIO:
    """What an action running on ScmWorker may call back into."""

    def __init__(self, worker: "ScmWorker") -> None:
        self._worker = worker

    def step(self, report: StepReport) -> None:
        self._worker.step.emit(report)

    def line(self, line: DiagLine) -> None:
        self._worker.line.emit(line)

    def ask(self, key: str, default: bool) -> bool:
        return self._worker.request_answer(key, default)


class ScmWorker(QThread):
    """Runs one blocking action off the GUI thread.

    ``sc start`` can block for seconds and ``wait_for_state`` polls after it, so
    running this inline would freeze the window for the whole install. A
    diagnostics prompt parks the worker on a semaphore until the GUI thread
    answers, which keeps the check sequence as linear as the bat it ports.
    """

    step = Signal(object)
    line = Signal(object)
    question = Signal(object)
    done = Signal(object)

    def __init__(self, action, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._action = action
        self._answer = False
        self._answered = QSemaphore(0)

    def run(self) -> None:
        try:
            outcome = self._action(WorkerIO(self))
        except Exception as exc:
            outcome = exc
        self.done.emit(outcome)

    def request_answer(self, key: str, default: bool) -> bool:
        """Worker thread: hand the question to the GUI thread and wait for it."""
        self.question.emit((key, bool(default)))
        self._answered.acquire()
        return self._answer

    def provide_answer(self, answer: bool) -> None:
        """GUI thread: unblock the worker with the user's choice."""
        self._answer = bool(answer)
        self._answered.release()


class MainWindow(QMainWindow):
    def __init__(
        self,
        project_root: Path | None = None,
        lists_dir: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("mainWindow")
        self.setWindowTitle(f"{PROJECT_NAME} {__version__}")
        self.setFont(QFont(GUI_FONT_FAMILY))
        self.resize(1100, 760)

        try:
            self.project_root = Path(project_root) if project_root else detect_project_root()
        except ProjectRootError as exc:
            self.project_root = Path.cwd()
            self._root_error = str(exc)
        else:
            self._root_error = ""

        self.lists_dir = Path(lists_dir) if lists_dir else (self.project_root / "lists")
        self.results_dir = self.project_root / RESULTS_DIR
        self._parsed: dict[str, ParsedStrategy] = {}
        self._current_list: Path | None = None
        self._editor_dirty = False
        self._loading_editor = False
        self._catalog = load_catalog()
        self._locale = "en"
        self._i18n_table = self._catalog["en"]
        self._worker: ScmWorker | None = None
        self._last_snapshot: ServiceSnapshot | None = None
        # Parented timers die with the window. A bare QTimer.singleShot bound to
        # a method would still fire after the window is gone, on a dead object.
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._poll_service_status)
        self._status_poll = QTimer(self)
        self._status_poll.setInterval(STATUS_POLL_MS)
        self._status_poll.timeout.connect(self._poll_service_status)
        self._results_since: float | None = None
        self._results_tries = 0
        self._results_watcher = QFileSystemWatcher(self)
        self._results_watcher.directoryChanged.connect(self._on_results_changed)
        self._results_retry = QTimer(self)
        self._results_retry.setSingleShot(True)
        self._results_retry.timeout.connect(self._check_results)

        self._build_ui()
        self._wire()
        apply_locale(self, "en", self._catalog)
        self._load_strategies()
        self._load_list_files()
        self._poll_service_status()
        self._status_poll.start()
        if self._root_error:
            self._log(f"ERROR: {self._root_error}", error=True)

    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 8)
        root.setSpacing(10)

        header = QGridLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setColumnStretch(0, 1)
        header.setColumnStretch(1, 0)
        header.setColumnStretch(2, 1)

        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(14)
        title = QLabel("ZAPRET CONTROL")
        title.setObjectName("titleLabel")
        title.setProperty("i18n", "titleLabel")
        self.service_status_label = QLabel()
        self.service_status_label.setObjectName("serviceStatusLabel")
        self.service_status_label.setProperty("state", "pending")
        title_row.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self.service_status_label, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch(1)
        subtitle = QLabel("STRATEGY SERVICES  ·  HOSTS  ·  LISTS")
        subtitle.setObjectName("subtitleLabel")
        subtitle.setProperty("i18n", "subtitleLabel")
        titles.addLayout(title_row)
        titles.addWidget(subtitle)
        left = QWidget()
        left.setLayout(titles)
        header.addWidget(left, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.language_button = QPushButton("Русский")
        self.language_button.setObjectName("languageButton")
        header.addWidget(
            self.language_button,
            0,
            1,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
        )

        right = QWidget()
        right_layout = QHBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addStretch(1)
        self.privilege_banner = QLabel()
        self.privilege_banner.setObjectName("privilegeBanner")
        right_layout.addWidget(self.privilege_banner)
        self.relaunch_button = QPushButton("Relaunch as Administrator")
        self.relaunch_button.setObjectName("relaunchAdminButton")
        right_layout.addWidget(self.relaunch_button)
        header.addWidget(right, 0, 2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)

        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(12)
        top_layout.addWidget(self._build_service_panel(), 3)
        top_layout.addWidget(self._build_hosts_panel(), 2)
        splitter.addWidget(top)
        splitter.addWidget(self._build_lists_panel())
        splitter.addWidget(self._build_console_panel())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)
        root.addWidget(splitter, 1)

        status = QStatusBar()
        self.setStatusBar(status)
        status.showMessage(f"Root: {self.project_root}")

        save_action = QAction("Save list", self)
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self._on_save_list)
        self.addAction(save_action)

        refresh_action = QAction("Refresh status", self)
        refresh_action.setShortcut("F5")
        refresh_action.triggered.connect(self._poll_service_status)
        self.addAction(refresh_action)

    def _build_service_panel(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        cap = QLabel("SERVICE STRATEGY")
        cap.setObjectName("sectionLabel")
        cap.setProperty("i18n", "serviceSection")
        layout.addWidget(cap)

        self.strategy_combo = QComboBox()
        self.strategy_combo.setObjectName("strategyCombo")
        layout.addWidget(self.strategy_combo)

        # Two buttons per row: the pair that changes the service, then the pair
        # that inspects the machine.
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        self.install_button = QPushButton("Install Service")
        self.install_button.setObjectName("installButton")
        self.remove_button = QPushButton("Remove")
        self.remove_button.setObjectName("removeButton")
        self.tests_button = QPushButton("Tests")
        self.tests_button.setObjectName("testsButton")
        self.tests_button.setProperty("i18nTooltip", "testsHint")
        self.tests_button.setToolTip(TESTS_HINT)
        self.diagnostics_button = QPushButton("Diagnostics")
        self.diagnostics_button.setObjectName("diagnosticsButton")
        grid.addWidget(self.install_button, 0, 0)
        grid.addWidget(self.remove_button, 0, 1)
        grid.addWidget(self.tests_button, 1, 0)
        grid.addWidget(self.diagnostics_button, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.service_buttons_grid = grid
        layout.addLayout(grid)

        self.tests_hint = QLabel(TESTS_HINT)
        self.tests_hint.setObjectName("testsHint")
        self.tests_hint.setProperty("i18n", "testsHint")
        self.tests_hint.setWordWrap(True)
        layout.addWidget(self.tests_hint)

        filter_row = QHBoxLayout()
        filter_cap = QLabel("Game Filter")
        filter_cap.setObjectName("subtitleLabel")
        filter_cap.setProperty("i18n", "gameFilterLabel")
        self.game_filter_combo = QComboBox()
        self.game_filter_combo.setObjectName("gameFilterCombo")
        for mode, label in GAME_FILTER_LABELS:
            self.game_filter_combo.addItem(label, mode)
        filter_row.addWidget(filter_cap)
        filter_row.addWidget(self.game_filter_combo, 1)
        layout.addLayout(filter_row)

        self.args_preview = QPlainTextEdit()
        self.args_preview.setObjectName("argsPreview")
        self.args_preview.setReadOnly(True)
        self.args_preview.setPlaceholderText("Resolved winws.exe arguments appear here.")
        layout.addWidget(self.args_preview, 1)
        return box

    def _build_hosts_panel(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        cap = QLabel("HOSTS FILE")
        cap.setObjectName("sectionLabel")
        cap.setProperty("i18n", "hostsSection")
        layout.addWidget(cap)

        hint = QLabel(
            "Opens Notepad elevated on the system hosts file.\n"
            f"{HOSTS_PATH}\n"
            "The GUI never writes hosts itself."
        )
        hint.setWordWrap(True)
        hint.setObjectName("subtitleLabel")
        hint.setProperty("i18n", "hostsHint")
        layout.addWidget(hint)

        self.hosts_button = QPushButton("Open hosts as Administrator")
        self.hosts_button.setObjectName("hostsButton")
        layout.addWidget(self.hosts_button)
        layout.addStretch(1)
        return box

    def _build_lists_panel(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        cap = QLabel("LISTS  (IN-APP EDITOR — NO EXTERNAL NOTEPAD)")
        cap.setObjectName("sectionLabel")
        cap.setProperty("i18n", "listsSection")
        layout.addWidget(cap)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.list_files_widget = QListWidget()
        self.list_files_widget.setObjectName("listFileList")
        self.list_files_widget.setMinimumWidth(220)
        split.addWidget(self.list_files_widget)

        self.list_editor = QPlainTextEdit()
        self.list_editor.setObjectName("listEditor")
        self.list_editor.setPlaceholderText("Select a file from lists/ to edit it here.")
        split.addWidget(self.list_editor)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)

        buttons = QHBoxLayout()
        self.backup_button = QPushButton("Backup")
        self.backup_button.setObjectName("backupButton")
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("saveButton")
        self.backup_all_button = QPushButton("Backup all lists")
        self.backup_all_button.setObjectName("backupAllButton")
        buttons.addWidget(self.backup_button)
        buttons.addWidget(self.backup_all_button)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        return box

    def _build_console_panel(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        cap = QLabel("CONSOLE")
        cap.setObjectName("sectionLabel")
        cap.setProperty("i18n", "consoleSection")
        self.clear_console_button = QPushButton("Clear")
        self.clear_console_button.setObjectName("clearConsoleButton")
        header.addWidget(cap)
        header.addStretch(1)
        header.addWidget(self.clear_console_button)
        layout.addLayout(header)

        self.console = QTextEdit()
        self.console.setObjectName("consoleView")
        self.console.setReadOnly(True)
        self.console.setMinimumHeight(110)
        self.console.setFont(QFont(CONSOLE_FONT_FAMILY))
        layout.addWidget(self.console, 1)
        return box

    def _wire(self) -> None:
        self.hosts_button.clicked.connect(self._on_open_hosts)
        self.language_button.clicked.connect(self._on_toggle_language)
        self.relaunch_button.clicked.connect(self._on_relaunch_admin)
        self.strategy_combo.currentIndexChanged.connect(self._on_strategy_changed)
        self.install_button.clicked.connect(self._on_install)
        self.remove_button.clicked.connect(self._on_remove)
        self.tests_button.clicked.connect(self._on_tests)
        self.diagnostics_button.clicked.connect(self._on_diagnostics)
        self.game_filter_combo.currentIndexChanged.connect(self._on_game_filter_changed)
        self.list_files_widget.currentItemChanged.connect(self._on_list_file_changed)
        self.list_editor.textChanged.connect(self._on_editor_changed)
        self.backup_button.clicked.connect(self._on_backup)
        self.backup_all_button.clicked.connect(self._on_backup_all)
        self.save_button.clicked.connect(self._on_save_list)
        self.clear_console_button.clicked.connect(self.console.clear)

    def _refresh_privilege_banner(self) -> None:
        elevated = is_process_elevated()
        self.privilege_banner.setProperty("elevated", "true" if elevated else "false")
        if elevated:
            self.privilege_banner.setText(lookup(self, "privilegeElevated", "ELEVATED"))
            self.relaunch_button.hide()
        else:
            self.privilege_banner.setText(
                lookup(
                    self,
                    "privilegeNotElevated",
                    "NOT ELEVATED — installing or removing the service will request UAC",
                )
            )
            self.relaunch_button.show()
        self.privilege_banner.style().unpolish(self.privilege_banner)
        self.privilege_banner.style().polish(self.privilege_banner)

    def _load_strategies(self) -> None:
        self.strategy_combo.blockSignals(True)
        self.strategy_combo.clear()
        self._parsed.clear()
        try:
            paths = discover_strategies(self.project_root)
        except StrategyError as exc:
            self._log(f"Strategy discovery failed: {exc}", error=True)
            self.strategy_combo.blockSignals(False)
            return
        if not paths:
            self._log("No general*.bat strategies found in the project root.", error=True)
        for path in paths:
            self.strategy_combo.addItem(path.name, str(path))
        self.strategy_combo.blockSignals(False)
        if self.strategy_combo.count():
            self.strategy_combo.setCurrentIndex(0)
            self._on_strategy_changed(0)
        self._sync_game_filter_combo()

    def _current_parsed(self) -> ParsedStrategy | None:
        name = self.strategy_combo.currentText()
        if not name:
            return None
        cached = self._parsed.get(name)
        if cached is not None:
            return cached
        data = self.strategy_combo.currentData()
        if not data:
            return None
        try:
            parsed = parse_strategy(Path(str(data)), self.project_root)
        except StrategyError as exc:
            self._log(f"Parse failed for {name}: {exc}", error=True)
            self.args_preview.setPlainText(f"PARSE ERROR: {exc}")
            return None
        self._parsed[name] = parsed
        return parsed

    def _on_strategy_changed(self, _index: int) -> None:
        parsed = self._current_parsed()
        if parsed is None:
            return
        preview = (
            f"image: {parsed.image}\n"
            f"GameFilterTCP={parsed.game_filter.tcp}  "
            f"GameFilterUDP={parsed.game_filter.udp}  "
            f"mode={parsed.game_filter.mode}\n\n"
            f"{parsed.args_line}"
        )
        self.args_preview.setPlainText(preview)
        self._log(f"Loaded strategy {parsed.name}")

    def _on_open_hosts(self) -> None:
        result = launch_hosts_notepad()
        self._report_hosts(result)

    def _on_toggle_language(self) -> None:
        toggle_locale(self, self._catalog)

    def _on_relaunch_admin(self) -> None:
        result = relaunch_self_elevated(project_root=self.project_root)
        if result.ok:
            self._log("Elevated relaunch requested. Close this window after the new instance starts.")
        else:
            self._log(result.error or "Relaunch failed", error=True)
            QMessageBox.warning(self, PROJECT_NAME, result.error or "Relaunch failed")

    def _service_buttons(self) -> tuple[QPushButton, ...]:
        return (
            self.install_button,
            self.remove_button,
            self.tests_button,
            self.diagnostics_button,
        )

    def _run_scm(self, verb: str, action, reporter) -> None:
        """Run ``action(worker_io)`` on a worker thread, streaming its output to the console."""
        if self._worker is not None:
            self._log(f"{verb} ignored: another service action is still running.", error=True)
            return
        self._console_header(verb)
        for button in self._service_buttons():
            button.setEnabled(False)
        # A service handle held across `sc delete` defers the deletion and breaks
        # the `sc create` right after it (1072), so the status poll sits this out.
        self._status_poll.stop()
        self._status_timer.stop()
        worker = ScmWorker(action, self)
        self._worker = worker
        worker.step.connect(self._console_step)
        worker.line.connect(self._console_diag)
        worker.question.connect(lambda payload, source=worker: self._answer_question(source, payload))
        worker.done.connect(lambda outcome: self._finish_scm(verb, reporter, outcome))
        worker.finished.connect(self._release_worker)
        worker.start()

    def _finish_scm(self, verb: str, reporter, outcome) -> None:
        """``done`` carries the result; the thread is still unwinding at this point."""
        for button in self._service_buttons():
            button.setEnabled(True)
        if isinstance(outcome, Exception):
            message = f"{verb} failed: {outcome}"
            self._log(message, error=True)
            QMessageBox.warning(self, PROJECT_NAME, message)
            return
        reporter(outcome)

    def _release_worker(self) -> None:
        """Drop the worker only once the thread has actually terminated.

        Releasing it on ``done`` would leave a live QThread reachable only as a
        child of this window; destroying the window around it aborts the process.
        """
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.wait()
            worker.deleteLater()
        self._poll_service_status()
        self._status_poll.start()

    def _answer_question(self, worker: ScmWorker, payload) -> None:
        key, default = payload
        text = lookup(self, key, PROMPT_FALLBACKS.get(key, key))
        answer = bool(default)
        try:
            buttons = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            preferred = QMessageBox.StandardButton.Yes if default else QMessageBox.StandardButton.No
            choice = QMessageBox.question(self, PROJECT_NAME, text, buttons, preferred)
            answer = choice == QMessageBox.StandardButton.Yes
        finally:
            # service.bat echoes what was typed at its `set /p` prompt.
            self._console_write(
                f"{text} (Y/N) (default: {'Y' if default else 'N'}) {'Y' if answer else 'N'}\n",
                CONSOLE_TEXT,
            )
            worker.provide_answer(answer)

    def _on_install(self) -> None:
        parsed = self._current_parsed()
        if parsed is None:
            self._log("Select a strategy before installing the service.", error=True)
            return
        if not Path(parsed.image).is_file():
            self._log(f"winws.exe is missing: {parsed.image}", error=True)
            QMessageBox.warning(self, PROJECT_NAME, f"winws.exe is missing:\n{parsed.image}")
            return
        stem = strategy_stem(parsed.name)
        image, args = parsed.image, parsed.args_line

        def action(worker_io: WorkerIO) -> ScmOpResult:
            return install_service(image, args, allow_live=True, on_step=worker_io.step)

        self._run_scm(
            f"Install Service — {parsed.name}",
            action,
            lambda result: self._after_install(stem, result),
        )

    def _after_install(self, stem: str, result: ScmOpResult) -> None:
        # service.bat:362 records the strategy name after sc start. A UAC-delegated
        # install cannot be observed, so it must not stamp a name onto whichever
        # service currently owns this service name.
        if result.ok and not result.needs_elevation:
            ident = persist_installed_strategy(stem, allow_live=True)
            self._console_step(
                StepReport(
                    kind="identity",
                    command_line=(
                        f'reg add "{ident.plan.key}" /v {ident.plan.value_name} '
                        f'/t REG_SZ /d "{ident.plan.data}" /f'
                    ),
                    returncode=0 if ident.ok else 1,
                    ok=ident.ok,
                    note="" if ident.ok else (ident.error or "registry write failed"),
                )
            )
        self._report_scm("Install", result)

    def _on_remove(self) -> None:
        def action(worker_io: WorkerIO) -> RemoveResult:
            return remove_service(allow_live=True, on_step=worker_io.step)

        self._run_scm("Remove", action, self._report_remove)

    def _on_tests(self) -> None:
        snapshot = self._last_snapshot
        installed = snapshot is not None and snapshot.state in ("running", "pending", "stopped")

        def action(worker_io: WorkerIO) -> bool:
            # service.bat:1119-1126 refuses to go on without PowerShell 3.0+.
            if powershell_supported():
                return True
            for text in POWERSHELL_REQUIRED:
                worker_io.line(DiagLine("info", text))
            return False

        self._run_scm(
            "Tests",
            action,
            lambda supported: self._after_tests_probe(bool(supported), installed),
        )

    def _after_tests_probe(self, supported: bool, installed: bool) -> None:
        if not supported:
            return
        if installed:
            # The script checks this itself (ps1:374-387) and exits; say so up front.
            self._console_diag(
                DiagLine(
                    "warn",
                    f"Windows service '{SERVICE_NAME}' is installed - "
                    "the tests refuse to run until you press Remove",
                )
            )
        self._console_diag(DiagLine("info", TESTS_STARTING))
        # ShellExecute belongs on the GUI thread, where Qt has already initialised COM.
        result = launch_tests(self.project_root, elevated=is_process_elevated())
        if not result.ok:
            message = result.error or "Failed to start the tests"
            self._log(message, error=True)
            QMessageBox.warning(self, PROJECT_NAME, message)
            return
        self._arm_results_watch(time.time())

    def _arm_results_watch(self, started_at: float) -> None:
        try:
            self.results_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log(f"Cannot watch {self.results_dir}: {exc}", error=True)
            return
        self._disarm_results_watch()
        self._results_since = started_at - RESULTS_CLOCK_SLACK_S
        self._results_tries = 0
        self._results_watcher.addPath(str(self.results_dir))

    def _disarm_results_watch(self) -> None:
        self._results_since = None
        self._results_retry.stop()
        watched = self._results_watcher.directories()
        if watched:
            self._results_watcher.removePaths(watched)

    def _on_results_changed(self, _path: str) -> None:
        self._results_tries = 0
        self._check_results()

    def _check_results(self) -> None:
        since = self._results_since
        if since is None:
            return
        newest = newest_result_since(self.results_dir, since)
        if newest is None:
            return
        try:
            text = newest.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            text = ""
        winner = find_best_strategy_line(text)
        if winner is None:
            # The file exists but its closing line has not landed yet.
            if self._results_tries < RESULTS_RETRIES:
                self._results_tries += 1
                self._results_retry.start(RESULTS_RETRY_MS)
            return
        self._disarm_results_watch()
        if not winner:
            self._console_diag(DiagLine("warn", f"Tests finished without a working strategy ({newest.name})"))
            return
        index = self.strategy_combo.findText(winner)
        if index < 0:
            self._console_diag(DiagLine("warn", f"Tests picked {winner}, which is not in the strategy list"))
            return
        self.strategy_combo.setCurrentIndex(index)
        self._console_write(
            f"Tests picked {winner} — selected; press Install Service to use it\n",
            CONSOLE_OK,
            bold=True,
        )

    def _on_diagnostics(self) -> None:
        root = self.project_root

        def action(worker_io: WorkerIO) -> bool:
            run_diagnostics(root, probes=default_probes(), emit=worker_io.line, ask=worker_io.ask)
            return True

        self._run_scm("Diagnostics", action, self._after_diagnostics)

    def _after_diagnostics(self, _outcome) -> None:
        self._console_write("Diagnostics finished\n", CONSOLE_OK, bold=True)
        self._schedule_status_refresh(200)

    def _sync_game_filter_combo(self) -> None:
        current = load_game_filter(self.project_root)
        self.game_filter_combo.blockSignals(True)
        index = 0
        for i, (mode, _label) in enumerate(GAME_FILTER_LABELS):
            if mode == current.mode:
                index = i
                break
        self.game_filter_combo.setCurrentIndex(index)
        self.game_filter_combo.blockSignals(False)

    def _on_game_filter_changed(self, _index: int) -> None:
        mode = str(self.game_filter_combo.currentData() or "disabled")
        if mode not in GAME_FILTER_MODES:
            mode = "disabled"
        try:
            gf = save_game_filter(self.project_root, mode)
        except StrategyError as exc:
            self._log(str(exc), error=True)
            QMessageBox.warning(self, PROJECT_NAME, str(exc))
            self._sync_game_filter_combo()
            return
        self._parsed.clear()
        self._on_strategy_changed(self.strategy_combo.currentIndex())
        self._log(
            f"Game Filter set to {gf.mode} (TCP={gf.tcp} UDP={gf.udp}). Restart zapret to apply."
        )

    def _schedule_status_refresh(self, delay_ms: int) -> None:
        """Re-query SCM shortly after a mutation, coalescing repeated requests."""
        self._status_timer.start(delay_ms)

    def _poll_service_status(self) -> None:
        if self._worker is not None:
            return
        try:
            snapshot = query_service_snapshot(SERVICE_NAME)
        except Exception as exc:
            snapshot = ServiceSnapshot(SERVICE_NAME, "ERROR", error=str(exc))
        previous = self._last_snapshot
        self._last_snapshot = snapshot
        self._render_service_status()
        if previous is None:
            self._log(f"Service {SERVICE_NAME}: {snapshot.token.lower()}")
        elif previous.token != snapshot.token:
            self._log(f"Service {SERVICE_NAME}: {previous.token.lower()} → {snapshot.token.lower()}")

    def _render_service_status(self) -> None:
        label = getattr(self, "service_status_label", None)
        snapshot = self._last_snapshot
        if label is None or snapshot is None:
            return
        key, fallback, state = STATUS_TOKENS.get(snapshot.token, STATUS_ERROR)
        parts = [f"● {lookup(self, key, fallback)}"]
        if snapshot.strategy and state != "not_installed":
            parts.append(snapshot.strategy)
        # Two installs can own the one service name; show which tree this one is.
        if snapshot.image and not image_is_under(snapshot.image, self.project_root):
            parts.append(image_install_root(snapshot.image))
        label.setText("  ·  ".join(parts))
        label.setToolTip(snapshot.image or snapshot.error)
        label.setProperty("state", state)
        label.style().unpolish(label)
        label.style().polish(label)

    def _load_list_files(self) -> None:
        self.list_files_widget.clear()
        try:
            files = list_files(self.lists_dir)
        except ListsError as exc:
            self._log(str(exc), error=True)
            return
        for path in files:
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.list_files_widget.addItem(item)
        if self.list_files_widget.count():
            self.list_files_widget.setCurrentRow(0)

    def _on_list_file_changed(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        if current is None:
            return
        if self._editor_dirty and self._current_list is not None:
            choice = QMessageBox.question(
                self,
                PROJECT_NAME,
                f"Save changes to {self._current_list.name}?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
            )
            if choice == QMessageBox.StandardButton.Cancel:
                return
            if choice == QMessageBox.StandardButton.Yes:
                self._on_save_list()
        path = Path(str(current.data(Qt.ItemDataRole.UserRole)))
        try:
            text = read_list(path)
        except ListsError as exc:
            self._log(str(exc), error=True)
            return
        self._loading_editor = True
        self.list_editor.setPlainText(text)
        self._loading_editor = False
        self._current_list = path
        self._editor_dirty = False
        self._log(f"Opened {path.name}")

    def _on_editor_changed(self) -> None:
        if not self._loading_editor:
            self._editor_dirty = True

    def _on_backup(self) -> None:
        path = self._current_list
        if path is None:
            self._log("Select a list file before backing up.", error=True)
            return
        try:
            dest = backup_list(path)
        except ListsError as exc:
            self._log(str(exc), error=True)
            QMessageBox.warning(self, PROJECT_NAME, str(exc))
            return
        self._log(f"Backup written: {dest.name}")
        self.statusBar().showMessage(f"Backup: {dest}", 8000)

    def _on_backup_all(self) -> None:
        try:
            dest = backup_all_lists(self.lists_dir)
        except ListsError as exc:
            self._log(str(exc), error=True)
            QMessageBox.warning(self, PROJECT_NAME, str(exc))
            return
        self._log(f"Archive written: {dest}")

    def _on_save_list(self) -> None:
        path = self._current_list
        if path is None:
            self._log("Select a list file before saving.", error=True)
            return
        try:
            write_list(path, self.list_editor.toPlainText())
        except ListsError as exc:
            self._log(str(exc), error=True)
            QMessageBox.warning(self, PROJECT_NAME, str(exc))
            return
        self._editor_dirty = False
        self._log(f"Saved {path.name}")
        self.statusBar().showMessage(f"Saved {path.name}", 5000)

    def _report_hosts(self, result: HostsLaunchResult) -> None:
        plan = result.plan
        self._log(
            f"Hosts launch verb={plan.verb} file={plan.file} params={plan.params} ok={result.ok}"
        )
        if result.ok:
            self.statusBar().showMessage("Elevated Notepad requested for hosts", 5000)
            return
        error = result.error or "Hosts launch failed"
        self._log(error, error=True)
        QMessageBox.warning(self, PROJECT_NAME, error)

    def _report_scm(self, verb: str, result: ScmOpResult) -> None:
        if result.needs_elevation and not is_process_elevated():
            self._delegated(verb, result.command.command_line)
            return
        if result.ok:
            pid = f"  winws pid {result.pid}" if result.pid else ""
            self._console_write(f"{verb} OK{pid}\n", CONSOLE_OK, bold=True)
            self._schedule_status_refresh(200)
            return
        error = result.error or f"{verb} failed"
        self._log(error, error=True)
        QMessageBox.warning(self, PROJECT_NAME, error)
        self._schedule_status_refresh(200)

    def _report_remove(self, result: RemoveResult) -> None:
        if result.needs_elevation and not is_process_elevated():
            self._delegated("Remove", result.command_line)
            return
        if result.ok:
            if result.error:
                # Like service.bat, a failed side step does not stop the removal.
                self._console_diag(DiagLine("warn", f"Some steps failed: {result.error}"))
            self._console_write("Remove OK\n", CONSOLE_OK, bold=True)
            self._schedule_status_refresh(200)
            return
        error = result.error or "Remove failed"
        self._log(error, error=True)
        QMessageBox.warning(self, PROJECT_NAME, error)
        self._schedule_status_refresh(200)

    def _delegated(self, verb: str, command_line: str) -> None:
        """A UAC-delegated action runs in a console this process cannot read.

        Say so rather than claiming success, then re-query for the real state.
        """
        self._console_command(command_line)
        self._console_write(
            f"  [ ?? ] {verb} handed to an elevated console — result not visible here.\n",
            CONSOLE_WARN,
        )
        self._schedule_status_refresh(1500)

    def _console_write(self, text: str, color: str = CONSOLE_TEXT, *, bold: bool = False) -> None:
        cursor = self.console.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if bold:
            fmt.setFontWeight(QFont.Weight.Bold)
        cursor.insertText(text, fmt)
        self.console.setTextCursor(cursor)
        self.console.ensureCursorVisible()

    def _console_header(self, text: str) -> None:
        self._console_write(f"\n=== {text} ===\n", CONSOLE_TEXT, bold=True)

    def _console_command(self, line: str) -> None:
        shown = line
        if len(shown) > CONSOLE_COMMAND_LIMIT:
            shown = f"{shown[:CONSOLE_COMMAND_LIMIT]}... (+{len(line) - CONSOLE_COMMAND_LIMIT} chars)"
        self._console_write(f"> {shown}\n", CONSOLE_CMD)

    def _console_step(self, report: StepReport) -> None:
        self._console_command(report.command_line)
        if report.ok:
            tag, color = "[ OK ]", CONSOLE_OK
        elif not report.fatal:
            tag, color = "[WARN]", CONSOLE_WARN
        else:
            tag, color = "[FAIL]", CONSOLE_FAIL
        head = report.note or (f"exit {report.returncode}" if report.returncode else "")
        self._console_write(f"  {tag} ", color)
        self._console_write(f"{head}\n" if head else "\n", CONSOLE_TEXT if report.ok else color)
        for raw in report.detail.splitlines():
            line = raw.strip()
            if line:
                self._console_write(f"         {line}\n", CONSOLE_MUTED)

    def _console_diag(self, line: DiagLine) -> None:
        if line.level == "blank":
            self._console_write("\n")
            return
        color = {"ok": CONSOLE_OK, "warn": CONSOLE_WARN, "fail": CONSOLE_FAIL}.get(line.level, CONSOLE_TEXT)
        self._console_write(f"{line.text}\n", color)

    def _log(self, message: str, *, error: bool = False) -> None:
        prefix = "ERR" if error else "INF"
        self._console_write(f"[{prefix}] {message}\n", CONSOLE_FAIL if error else CONSOLE_MUTED)
        if error:
            self.statusBar().showMessage(message, 8000)

    def closeEvent(self, event) -> None:
        self._status_poll.stop()
        self._status_timer.stop()
        self._disarm_results_watch()
        # Tearing down a QThread mid-sc.exe aborts the process; let it finish.
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.wait(15000)
        super().closeEvent(event)


def _load_windows_fonts() -> None:
    """Register system TTF files so offscreen/minimal plugins can rasterize glyphs."""
    windir = os.environ.get("WINDIR", r"C:\Windows")
    fonts_dir = Path(windir) / "Fonts"
    for filename in (
        "tahoma.ttf",
        "tahomabd.ttf",
        # The console pane opts out of Tahoma, so its family needs registering too.
        "consola.ttf",
        "consolab.ttf",
    ):
        path = fonts_dir / filename
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))


def collect_required_widgets(window: MainWindow) -> dict[str, QWidget | None]:
    found: dict[str, QWidget | None] = {}
    for name in REQUIRED_OBJECT_NAMES:
        found[name] = window.findChild(QWidget, name)
    return found


def missing_required_widgets(window: MainWindow) -> list[str]:
    return [name for name, widget in collect_required_widgets(window).items() if widget is None]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"{PROJECT_NAME} — Zapret strategy service GUI")
    parser.add_argument("--smoke", action="store_true", help="Construct the window, print control names, exit")
    parser.add_argument("--root", type=Path, default=None, help="Zapret project root (bin/ + lists/)")
    parser.add_argument("--screenshot", type=Path, default=None, help="Write a PNG of the constructed window (smoke)")
    return parser.parse_args(argv)


def run_smoke(project_root: Path | None = None, screenshot: Path | None = None) -> int:
    app = QApplication.instance() or QApplication(["zapret-gui-smoke"])
    _load_windows_fonts()
    app.setFont(QFont(GUI_FONT_FAMILY, 10))
    app.setStyleSheet(STYLESHEET)
    try:
        window = MainWindow(project_root=project_root)
        window.show()
        for _ in range(8):
            app.processEvents()
        window.repaint()
        app.processEvents()
        missing = missing_required_widgets(window)
        title = window.windowTitle()
        visible = window.isVisible()
        print(f"WINDOW_TITLE={title}")
        print(f"WINDOW_VISIBLE={visible}")
        print(f"PROJECT_ROOT={window.project_root}")
        for name, widget in collect_required_widgets(window).items():
            bound = widget is not None
            print(f"CONTROL {name} present={bound}")
        if screenshot is not None:
            screenshot = Path(screenshot)
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            grabbed = window.grab()
            saved = grabbed.save(str(screenshot))
            print(f"SCREENSHOT={screenshot} saved={bool(saved)} size={grabbed.width()}x{grabbed.height()}")
        if missing:
            print(f"SMOKE_MISSING {missing}")
            window.close()
            return 1
        print("SMOKE_OK")
        window.close()
        return 0
    except Exception:
        traceback.print_exc()
        print("SMOKE_FAIL")
        return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.smoke:
        return run_smoke(args.root, args.screenshot)
    try:
        app = QApplication.instance() or QApplication(sys.argv)
        _load_windows_fonts()
        app.setFont(QFont(GUI_FONT_FAMILY, 10))
        app.setStyleSheet(STYLESHEET)
        app.setApplicationName(PROJECT_NAME)
        window = MainWindow(project_root=args.root)
        window.show()
        # Center on the available screen without depending on a desktop manager.
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            frame = window.frameGeometry()
            frame.moveCenter(geo.center())
            window.move(frame.topLeft())
        return int(app.exec())
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
