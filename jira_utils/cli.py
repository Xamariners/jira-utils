from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass
import threading
from typing import Callable, List, Optional, Sequence, Set

try:
    import curses
except ImportError:  # pragma: no cover - platform without curses (e.g. Windows)
    curses = None

from .config import JiraConfig, load_config
from .jira_client import JiraAPIError, JiraClient
from .state import AttachmentState, StateManager


@dataclass
class AttachmentRecord:
    project_key: str
    issue_key: str
    issue_summary: str
    attachment_id: str
    attachment_name: str
    size_bytes: int
    created: dt.datetime

    @property
    def created_iso(self) -> str:
        return self.created.astimezone(dt.timezone.utc).isoformat()

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    def to_state(self) -> AttachmentState:
        return AttachmentState(
            attachment_id=self.attachment_id,
            project_key=self.project_key,
            issue_key=self.issue_key,
            issue_summary=self.issue_summary,
            attachment_name=self.attachment_name,
            size_bytes=self.size_bytes,
            created=self.created_iso,
        )


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    state_manager = StateManager(args.state_file)

    if args.command == "list":
        with JiraClient(config) as client:
            records = collect_attachments(client, config, args)
        render_table(records, state_manager)
    elif args.command == "mark":
        with JiraClient(config) as client:
            records = collect_attachments(client, config, args)
        apply_mark(records, state_manager, args)
    elif args.command == "process":
        with JiraClient(config) as client:
            process_queue(client, state_manager, args)
    elif args.command == "status":
        render_state(state_manager)
    elif args.command == "interactive":
        with JiraClient(config) as client:
            run_interactive(client, state_manager, config, args)
    else:
        raise ValueError(f"Unknown command: {args.command}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventory and clean Jira attachments.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", default="config.json", help="Path to the config file.")
    parser.add_argument(
        "--state-file", default="state/state.json", help="Where to store attachment state."
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common_filters(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--projects",
            nargs="+",
            help="Override projects from config (space separated list of keys).",
        )
        subparser.add_argument(
            "--issue-types",
            nargs="+",
            help="Limit results to the given issue type name(s) (as displayed in Jira).",
        )
        subparser.add_argument(
            "--min-size-mb", type=float, default=5.0, help="Attachment minimum size."
        )
        subparser.add_argument(
            "--before",
            type=str,
            required=True,
            help='Only include attachments created before this date (YYYY-MM-DD).',
        )
        subparser.add_argument(
            "--max-issues",
            type=int,
            default=None,
            help="Limit how many matching issues are scanned (useful for testing).",
        )
        subparser.add_argument(
            "--verbose",
            action="store_true",
            help="Print reasons why attachments were skipped by the filters.",
        )

    list_parser = subparsers.add_parser("list", help="List attachments that match filters.")
    add_common_filters(list_parser)

    mark_parser = subparsers.add_parser("mark", help="Mark attachments for deletion.")
    add_common_filters(mark_parser)
    mark_parser.add_argument(
        "--attachment-id",
        action="append",
        dest="attachment_ids",
        help="Only mark the given attachment id(s).",
    )

    process_parser = subparsers.add_parser(
        "process", help="Delete attachments that have been marked."
    )
    process_parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Retry attachments that previously failed.",
    )
    process_parser.add_argument(
        "--limit",
        type=int,
        help="Process at most this many attachments.",
    )

    subparsers.add_parser("status", help="Show tracked attachments and their status.")
    interactive_parser = subparsers.add_parser(
        "interactive", help="Launch an interactive picker to review and delete attachments."
    )
    add_common_filters(interactive_parser)

    return parser.parse_args()


LogFn = Callable[[str], None]


def collect_attachments(
    client: JiraClient,
    config: JiraConfig,
    args: argparse.Namespace,
    log: Optional[LogFn] = None,
    verbose_log: Optional[LogFn] = None,
) -> List[AttachmentRecord]:
    before = parse_before_date(args.before)
    min_size_bytes = int(args.min_size_mb * 1024 * 1024)
    projects = args.projects or config.project_keys
    issue_types = getattr(args, "issue_types", None) or []
    if not projects:
        raise ValueError(
            "At least one project key must be provided via the config file or --projects."
        )
    jql = build_jql(projects, issue_types)

    records: List[AttachmentRecord] = []
    max_results = config.page_size
    issue_count = 0
    next_page_token: Optional[str] = None
    verbose = getattr(args, "verbose", False)

    while True:
        payload = client.search_issues(
            jql, max_results=max_results, next_page_token=next_page_token
        )
        for warning in payload.get("warnings", []):
            message = f"Warning from Jira search API: {warning}"
            if log:
                log(message)
            else:
                print(message)
        issues = payload.get("issues", [])
        if not issues:
            break

        for issue in issues:
            issue_count += 1
            if args.max_issues and issue_count > args.max_issues:
                return records

            fields = issue.get("fields") or {}
            attachments = fields.get("attachment") or []
            for attachment in attachments:
                attachment_id = attachment.get("id")
                attachment_name = attachment.get("filename")
                if not attachment_id or not attachment_name:
                    continue

                created = parse_jira_timestamp(attachment.get("created"))
                if before and created >= before:
                    if verbose:
                        message = (
                            f"[skip-date] {issue.get('key')} attachment {attachment_name} "
                            f"created {created.isoformat()} >= cutoff {before.isoformat()}"
                        )
                        if verbose_log:
                            verbose_log(message)
                        else:
                            print(message)
                    continue

                size = attachment.get("size", 0)
                if size < min_size_bytes:
                    if verbose:
                        message = (
                            f"[skip-size] {issue.get('key')} attachment {attachment_name} "
                            f"size {size} < min {min_size_bytes}"
                        )
                        if verbose_log:
                            verbose_log(message)
                        else:
                            print(message)
                    continue

                record = AttachmentRecord(
                    project_key=fields.get("project", {}).get("key", "<unknown>"),
                    issue_key=issue.get("key", "<unknown>"),
                    issue_summary=fields.get("summary", ""),
                    attachment_id=attachment_id,
                    attachment_name=attachment_name,
                    size_bytes=size,
                    created=created,
                )
                records.append(record)

        next_page_token = payload.get("nextPageToken")
        if not next_page_token or payload.get("isLast", True):
            break

    attachment_ids = set(getattr(args, "attachment_ids", []) or [])
    if attachment_ids:
        wanted = attachment_ids
        records = [record for record in records if record.attachment_id in wanted]
    return records


def build_jql(projects: Sequence[str], issue_types: Optional[Sequence[str]] = None) -> str:
    if not projects:
        raise ValueError("At least one project key is required to build the search query.")
    clauses = [f'project in ({",".join(projects)})']
    if issue_types:
        formatted = ",".join(_quote_jql_value(value) for value in issue_types if value)
        if formatted:
            clauses.append(f"issuetype in ({formatted})")
    return " AND ".join(clauses) + " ORDER BY updated ASC"


def _quote_jql_value(value: str) -> str:
    if not value:
        return '""'
    escaped = value.replace('"', '\\"')
    return f'"{escaped}"'


def parse_before_date(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Date must be in YYYY-MM-DD format.") from exc
    return parsed.replace(tzinfo=dt.timezone.utc)


def parse_jira_timestamp(value: Optional[str]) -> dt.datetime:
    if not value:
        return dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    elif len(value) >= 5 and (value[-5] in {"+", "-"}):
        # Convert +0000 to +00:00 so fromisoformat can parse it
        value = f"{value[:-2]}:{value[-2:]}"
    return dt.datetime.fromisoformat(value)


def render_table(records: List[AttachmentRecord], state_manager: StateManager) -> None:
    if not records:
        print("No attachments matched the given filters.")
        return

    tracked = {item.attachment_id: item for item in state_manager.list()}
    headers = ["Project", "Issue", "Attachment", "Size (MB)", "Created", "Status"]
    rows = []
    for record in records:
        state = tracked.get(record.attachment_id)
        rows.append(
            [
                record.project_key,
                record.issue_key,
                f"{record.attachment_name} ({record.attachment_id})",
                f"{record.size_mb:.2f}",
                record.created_iso,
                state.status if state else "UNTRACKED",
            ]
        )
    print_table(headers, rows)


def render_state(state_manager: StateManager) -> None:
    records = state_manager.list()
    if not records:
        print("State file is empty.")
        return
    headers = ["Project", "Issue", "Attachment", "Size (MB)", "Created", "Status", "Error"]
    rows = [
        [
            record.project_key,
            record.issue_key,
            f"{record.attachment_name} ({record.attachment_id})",
            f"{record.size_mb:.2f}",
            record.created,
            record.status,
            record.error or "",
        ]
        for record in records
    ]
    print_table(headers, rows)


def print_table(headers: Sequence[str], rows: List[Sequence[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))

    def fmt_row(row: Sequence[str]) -> str:
        return " | ".join(value.ljust(widths[idx]) for idx, value in enumerate(row))

    print(fmt_row(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(fmt_row(row))


def apply_mark(records: List[AttachmentRecord], state_manager: StateManager, args: argparse.Namespace) -> None:
    requested = set(getattr(args, "attachment_ids", []) or [])
    if requested:
        found = {record.attachment_id for record in records}
        missing = requested - found
        if missing:
            print(
                "Warning: the following attachment ids were not found and were skipped: "
                + ", ".join(sorted(missing))
            )

    if not records:
        print("Nothing to mark. Adjust filters or attachment ids.")
        return

    for record in records:
        state_manager.upsert(record.to_state())
    print(f"Marked {len(records)} attachment(s).")


def process_queue(client: JiraClient, state_manager: StateManager, args: argparse.Namespace) -> None:
    targets = state_manager.filter_by_status(["TODO"])
    if args.retry_errors:
        error_records = state_manager.filter_by_status(["ERROR"])
        tracked = {record.attachment_id for record in targets}
        targets.extend(record for record in error_records if record.attachment_id not in tracked)

    if not targets:
        print("No attachments queued for processing.")
        return

    if args.limit:
        targets = targets[: args.limit]

    for record in targets:
        print(f"Deleting attachment {record.attachment_name} ({record.attachment_id})...")
        state_manager.update_status(record.attachment_id, "IN_PROGRESS", None)
        try:
            delete_with_comment_cleanup(client, record)
            state_manager.update_status(record.attachment_id, "DONE", None)
            print(f"  ✔ Deleted {record.attachment_id}")
        except JiraAPIError as exc:
            state_manager.update_status(record.attachment_id, "ERROR", str(exc))
            print(f"  ✖ Failed {record.attachment_id}: {exc}")


def delete_with_comment_cleanup(client: JiraClient, record: AttachmentState) -> None:
    try:
        client.delete_attachment(record.attachment_id)
        return
    except JiraAPIError as exc:
        if exc.status_code not in {400, 403, 409}:
            raise
        removed = cleanup_comments(client, record)
        if removed:
            client.delete_attachment(record.attachment_id)
            return
        raise


def cleanup_comments(client: JiraClient, record: AttachmentState) -> bool:
    comments = client.get_issue_comments(record.issue_key)
    removed_any = False
    for comment in comments:
        body = comment.get("body")
        text = flatten_comment(body)
        if not text:
            continue

        if record.attachment_id in text or record.attachment_name in text or f"[^{record.attachment_name}]" in text:
            client.delete_issue_comment(record.issue_key, comment["id"])
            removed_any = True
    return removed_any


def flatten_comment(body: object) -> str:
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        text = []
        for item in body.get("content", []):
            text.append(flatten_comment(item))
        if body.get("text"):
            text.append(body["text"])
        return " ".join(part for part in text if part)
    if isinstance(body, list):
        return " ".join(flatten_comment(item) for item in body)
    return ""


@dataclass
class InteractiveRow:
    record: AttachmentRecord
    status: str
    error: Optional[str] = None


@dataclass
class ProjectChoice:
    key: str
    name: str


@dataclass
class IssueTypeChoice:
    name: str


def run_interactive(
    client: JiraClient,
    state_manager: StateManager,
    config: JiraConfig,
    args: argparse.Namespace,
) -> None:
    if curses is None:
        raise RuntimeError("Interactive mode requires the curses module, which is not available.")

    seed_projects: Sequence[str] = args.projects or config.project_keys
    seed_issue_types: Sequence[str] = getattr(args, "issue_types", []) or []
    try:
        project_choices = load_project_choices(client, seed_projects)
        issue_type_choices = load_issue_type_choices(client, seed_issue_types)
    except JiraAPIError as exc:
        print(f"Failed to bootstrap interactive filters: {exc}")
        return

    if not project_choices:
        print("No Jira projects available. Add project keys to the config or pass --projects.")
        return

    default_selection = set(seed_projects or [])
    default_issue_types = set(seed_issue_types or [])

    def _wrapped(stdscr: "curses._CursesWindow") -> None:
        app = InteractiveApp(
            stdscr=stdscr,
            client=client,
            state_manager=state_manager,
            config=config,
            project_choices=project_choices,
            default_selected_keys=default_selection,
            issue_type_choices=issue_type_choices,
            default_issue_types=default_issue_types,
            before=args.before,
            max_issues=args.max_issues,
            verbose=args.verbose,
            initial_min_size=args.min_size_mb,
        )
        app.run()

    curses.wrapper(_wrapped)


def load_project_choices(
    client: JiraClient, seed_projects: Sequence[str]
) -> List[ProjectChoice]:
    seen: Set[str] = set()
    choices: List[ProjectChoice] = []
    seed_list = [key for key in seed_projects if key]

    try:
        projects = client.list_projects()
    except JiraAPIError:
        if not seed_list:
            raise
        for key in seed_list:
            if key not in seen:
                choices.append(ProjectChoice(key=key, name=key))
                seen.add(key)
        return choices

    for project in projects:
        key = project.get("key")
        name = project.get("name") or key or "<unknown>"
        if key and key not in seen:
            choices.append(ProjectChoice(key=key, name=name))
            seen.add(key)

    for key in seed_list:
        if key not in seen:
            choices.append(ProjectChoice(key=key, name=key))
            seen.add(key)

    return choices


def load_issue_type_choices(
    client: JiraClient, seed_issue_types: Sequence[str]
) -> List[IssueTypeChoice]:
    seen: Set[str] = set()
    choices: List[IssueTypeChoice] = []
    seed_list = [name for name in seed_issue_types if name]

    try:
        issue_types = client.list_issue_types()
    except JiraAPIError:
        if not seed_list:
            raise
        for name in seed_list:
            if name not in seen:
                choices.append(IssueTypeChoice(name=name))
                seen.add(name)
        return choices

    for item in issue_types:
        name = item.get("name")
        if name and name not in seen:
            choices.append(IssueTypeChoice(name=name))
            seen.add(name)

    for name in seed_list:
        if name not in seen:
            choices.append(IssueTypeChoice(name=name))
            seen.add(name)

    return choices


class InteractiveApp:
    HEADER_LINES = 4  # title, instructions, divider, header
    FOOTER_LINES = 3

    def __init__(
        self,
        stdscr: "curses._CursesWindow",
        client: JiraClient,
        state_manager: StateManager,
        config: JiraConfig,
        project_choices: Sequence[ProjectChoice],
        default_selected_keys: Set[str],
        issue_type_choices: Sequence[IssueTypeChoice],
        default_issue_types: Set[str],
        before: str,
        max_issues: Optional[int],
        verbose: bool,
        initial_min_size: float,
    ) -> None:
        self.stdscr = stdscr
        self.client = client
        self.state_manager = state_manager
        self.config = config
        self.project_choices = list(project_choices)
        self.issue_type_choices = list(issue_type_choices)
        self.project_cursor = 0
        self.project_scroll = 0
        self.issue_type_cursor = 0
        self.issue_type_scroll = 0
        self.project_selected: Set[int] = {
            idx for idx, choice in enumerate(self.project_choices) if choice.key in default_selected_keys
        }
        self.issue_type_selected: Set[int] = {
            idx for idx, choice in enumerate(self.issue_type_choices) if choice.name in default_issue_types
        }
        self.mode = "filter_overview"
        self.message = "Adjust filters, then press Enter to load attachments."
        self.status_line = ""
        self.cursor = 0
        self.scroll = 0
        self.rows: List[InteractiveRow] = []
        self.selected: Set[int] = set()
        self.anchor: Optional[int] = None
        self.busy = False
        self.processing_thread: Optional[threading.Thread] = None
        self.processing_result: Optional[str] = None
        self.pending_exit = False
        self.before = before
        self.max_issues = max_issues
        self.verbose = verbose
        self.min_size_mb = initial_min_size
        self.pending_reload = False

    def run(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.stdscr.keypad(True)
        self.stdscr.timeout(200)
        while True:
            self._poll_processing()
            self.draw()
            key = self.stdscr.getch()
            if key == -1:
                continue
            if key in (ord("q"), 27):  # q or ESC
                if self.busy and not self.pending_exit:
                    self.status_line = "Processing in progress. Press q again to exit immediately."
                    self.pending_exit = True
                    continue
                break
            self.pending_exit = False
            if self.mode == "filter_overview":
                self.handle_filter_key(key)
            elif self.mode == "project_picker":
                self.handle_project_key(key)
            elif self.mode == "type_picker":
                self.handle_issue_type_key(key)
            else:
                self.handle_attachment_key(key)

    def handle_attachment_key(self, key: int) -> None:
        if key in (curses.KEY_UP, ord("k")):
            self.move_cursor(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.move_cursor(1)
        elif key == curses.KEY_PPAGE:
            self.move_cursor(-self.visible_rows())
        elif key == curses.KEY_NPAGE:
            self.move_cursor(self.visible_rows())
        elif key in (32, ord("x")):
            if self.busy:
                self.status_line = "Selection disabled while processing."
            else:
                self.toggle_selection(self.cursor)
        elif key == ord("a"):
            if self.busy:
                self.status_line = "Selection disabled while processing."
            else:
                self.select_all()
        elif key == ord("c"):
            if self.busy:
                self.status_line = "Selection disabled while processing."
            else:
                self.clear_selection()
        elif key in (10, curses.KEY_ENTER, ord("d")):
            if self.busy:
                self.status_line = "Already processing selection."
            else:
                self.process_selected()
        elif key in (curses.KEY_SR, 337):
            if self.busy:
                self.status_line = "Selection disabled while processing."
            else:
                self.move_cursor(-1, extend=True)
        elif key in (curses.KEY_SF, 336):
            if self.busy:
                self.status_line = "Selection disabled while processing."
            else:
                self.move_cursor(1, extend=True)
        elif key == ord("g"):
            self.move_cursor(-self.cursor)
        elif key == ord("G"):
            self.move_cursor(len(self.rows) - 1 - self.cursor)
        elif key == ord("p"):
            if self.busy:
                self.status_line = "Cannot change projects while processing."
            else:
                self.enter_project_mode()
        elif key == ord("t"):
            if self.busy:
                self.status_line = "Cannot change issue types while processing."
            else:
                self.enter_type_mode()
        elif key == ord("f"):
            if self.busy:
                self.status_line = "Cannot adjust filters while processing."
            else:
                self.mode = "filter_overview"
                self.message = "Adjust filters, then press Enter to load attachments."
        elif key == ord("b"):
            if self.busy:
                self.status_line = "Cannot change dates while processing."
            else:
                self.prompt_before_date()
        elif key == ord("m"):
            self.prompt_min_size()

    def handle_filter_key(self, key: int) -> None:
        if key in (10, curses.KEY_ENTER):
            if self.fetch_and_update_rows():
                self.mode = "attachments"
        elif key == ord("p"):
            self.enter_project_mode()
        elif key == ord("t"):
            self.enter_type_mode()
        elif key == ord("m"):
            self.prompt_min_size()
        elif key == ord("b"):
            self.prompt_before_date()

    def _poll_processing(self) -> None:
        if self.processing_thread and not self.processing_thread.is_alive():
            self.processing_thread.join()
            self.processing_thread = None
            self.busy = False
            self.selected.clear()
            if self.processing_result:
                self.status_line = self.processing_result
                self.processing_result = None
            self.message = "Press 'd' to delete selected attachments."
        if not self.busy and self.pending_reload and self.mode == "attachments":
            self.pending_reload = False
            self.refresh_rows()

    def visible_rows(self) -> int:
        max_y, _ = self.stdscr.getmaxyx()
        return max(1, max_y - self.HEADER_LINES - self.FOOTER_LINES)

    def move_cursor(self, delta: int, extend: bool = False) -> None:
        if not self.rows:
            return
        prev = self.cursor
        self.cursor = max(0, min(self.cursor + delta, len(self.rows) - 1))
        if extend:
            if self.anchor is None:
                self.anchor = prev
            self.select_range(self.anchor, self.cursor)
        else:
            self.anchor = self.cursor
        self.ensure_visible()

    def ensure_visible(self) -> None:
        visible = self.visible_rows()
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + visible:
            self.scroll = self.cursor - visible + 1
        self.scroll = max(0, min(self.scroll, max(0, len(self.rows) - visible)))

    def select_range(self, start: int, end: int) -> None:
        if start > end:
            start, end = end, start
        for idx in range(start, end + 1):
            self.selected.add(idx)

    def toggle_selection(self, idx: int) -> None:
        if idx in self.selected:
            self.selected.remove(idx)
        else:
            self.selected.add(idx)
        self.anchor = idx

    def select_all(self) -> None:
        if len(self.selected) == len(self.rows):
            self.selected.clear()
        else:
            self.selected = set(range(len(self.rows)))
        self.anchor = self.cursor

    def clear_selection(self) -> None:
        self.selected.clear()
        self.anchor = self.cursor

    def draw(self) -> None:
        self.stdscr.erase()
        if self.mode == "filter_overview":
            self.draw_filter_overview()
            self.stdscr.refresh()
            return
        if self.mode == "project_picker":
            self.draw_project_selector()
            self.stdscr.refresh()
            return
        if self.mode == "type_picker":
            self.draw_issue_type_selector()
            self.stdscr.refresh()
            return
        max_y, max_x = self.stdscr.getmaxyx()
        title = (
            f"Interactive Jira Attachment Cleaner — {len(self.rows)} item(s)"
            f" [{self.project_filter_label()} | {self.issue_type_filter_label()} | ≥ {self.min_size_mb:.2f} MB | before {self.before}]"
        )
        self.stdscr.addnstr(0, 0, title, max_x - 1, curses.A_BOLD)
        instructions = (
            "[↑/↓] move  [Space] toggle  [Shift+↑/↓] range  [a] all  [c] clear  [m] min size  [b] before  [d] delete  [p] projects  [t] types  [f] filters  [q] quit"
        )
        self.stdscr.addnstr(1, 0, instructions, max_x - 1)
        self.stdscr.hline(2, 0, ord("-"), max_x)
        header = self.format_row("Idx", "Sel", "Status", "Issue", "Attachment", "Size", "Created")
        self.stdscr.addnstr(3, 0, header, max_x - 1, curses.A_UNDERLINE)

        visible = self.visible_rows()
        for offset in range(visible):
            row_idx = self.scroll + offset
            if row_idx >= len(self.rows):
                break
            row = self.rows[row_idx]
            selected = row_idx in self.selected
            checkbox = "[x]" if selected else "[ ]"
            line = self.format_row(
                f"{row_idx + 1}",
                checkbox,
                row.status,
                row.record.issue_key,
                f"{row.record.attachment_name} ({row.record.attachment_id})",
                f"{row.record.size_mb:.2f}",
                row.record.created_iso,
            )
            attr = curses.A_REVERSE if row_idx == self.cursor else curses.A_NORMAL
            if row.status == "ERROR":
                attr |= curses.A_BOLD
            self.stdscr.addnstr(self.HEADER_LINES + offset, 0, line, max_x - 1, attr)

        footer_y = max_y - self.FOOTER_LINES
        if footer_y >= self.HEADER_LINES:
            self.stdscr.hline(footer_y, 0, ord("-"), max_x)
            msg_line = min(max_y - 2, footer_y + 1)
            status_line = min(max_y - 1, footer_y + 2)
            self.stdscr.addnstr(msg_line, 0, self.message[: max_x - 1], max_x - 1)
            if self.status_line:
                self.stdscr.addnstr(status_line, 0, self.status_line[: max_x - 1], max_x - 1)
        self.stdscr.refresh()

    def format_row(
        self,
        idx: str,
        checkbox: str,
        status: str,
        issue: str,
        attachment: str,
        size: str,
        created: str,
    ) -> str:
        max_y, max_x = self.stdscr.getmaxyx()
        idx_w = 4
        sel_w = 3
        status_w = 12
        issue_w = 12
        size_w = 8
        created_w = 20
        base = idx_w + sel_w + status_w + issue_w + size_w + created_w + 6  # spaces between columns
        attachment_w = max(10, max_x - base)
        return (
            f"{idx:>{idx_w}} "
            f"{checkbox:<{sel_w}} "
            f"{self._trim(status, status_w):<{status_w}} "
            f"{self._trim(issue, issue_w):<{issue_w}} "
            f"{self._trim(attachment, attachment_w):<{attachment_w}} "
            f"{size:>{size_w}} "
            f"{self._trim(created, created_w):<{created_w}}"
        )

    @staticmethod
    def _trim(value: str, width: int) -> str:
        if len(value) <= width:
            return value
        if width <= 3:
            return value[:width]
        return value[: width - 3] + "..."

    def process_selected(self) -> None:
        if not self.selected:
            self.status_line = "Select one or more attachments first."
            return

        order = sorted(self.selected)
        processable: List[int] = []
        skipped_done = 0
        for idx in order:
            row = self.rows[idx]
            if row.status == "DONE":
                skipped_done += 1
                continue
            processable.append(idx)
        if not processable:
            self.status_line = "Nothing to do: selected attachment(s) already DONE."
            return
        if skipped_done:
            self.status_line = f"Skipping {skipped_done} DONE attachment(s)."

        self.busy = True
        self.message = "Processing attachments... (use arrow keys to scroll, q twice to quit)"
        self.processing_result = None
        self.processing_thread = threading.Thread(
            target=self._process_worker,
            args=(processable,),
            daemon=True,
        )
        self.processing_thread.start()

    def _process_worker(self, indexes: List[int]) -> None:
        processed = 0
        try:
            for idx in indexes:
                row = self.rows[idx]
                if row.status == "UNTRACKED":
                    self.state_manager.upsert(row.record.to_state())
                    row.status = "TODO"
                    row.error = None

                attachment_id = row.record.attachment_id
                self.status_line = f"Deleting {attachment_id}..."
                self.state_manager.update_status(attachment_id, "IN_PROGRESS", None)
                row.status = "IN_PROGRESS"
                row.error = None

                try:
                    delete_with_comment_cleanup(self.client, row.record.to_state())
                except JiraAPIError as exc:
                    error_text = str(exc)
                    self.state_manager.update_status(attachment_id, "ERROR", error_text)
                    row.status = "ERROR"
                    row.error = error_text
                    self.status_line = f"✖ {attachment_id} failed: {error_text}"
                else:
                    self.state_manager.update_status(attachment_id, "DONE", None)
                    row.status = "DONE"
                    row.error = None
                    processed += 1
                    self.status_line = f"✔ {attachment_id} deleted."
        except Exception as exc:  # pragma: no cover - defensive guard for runtime issues
            self.processing_result = f"Processing aborted: {exc}"
        else:
            self.processing_result = f"Done processing {processed} attachment(s)."

    def handle_project_key(self, key: int) -> None:
        if key in (curses.KEY_UP, ord("k")):
            self.move_project_cursor(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.move_project_cursor(1)
        elif key == curses.KEY_PPAGE:
            self.move_project_cursor(-self.visible_project_rows())
        elif key == curses.KEY_NPAGE:
            self.move_project_cursor(self.visible_project_rows())
        elif key in (32, ord("x")):
            self.toggle_project_selection(self.project_cursor)
        elif key == ord("a"):
            if len(self.project_selected) == len(self.project_choices):
                self.project_selected.clear()
            else:
                self.project_selected = set(range(len(self.project_choices)))
        elif key == ord("c"):
            self.project_selected.clear()
        elif key in (10, curses.KEY_ENTER):
            self.mode = "filter_overview"
            self.status_line = "Project selection updated."
        elif key == ord("m"):
            self.prompt_min_size()
        elif key == ord("b"):
            self.prompt_before_date()

    def move_project_cursor(self, delta: int) -> None:
        if not self.project_choices:
            return
        self.project_cursor = max(0, min(self.project_cursor + delta, len(self.project_choices) - 1))
        if self.project_cursor < self.project_scroll:
            self.project_scroll = self.project_cursor
        visible = self.visible_project_rows()
        if self.project_cursor >= self.project_scroll + visible:
            self.project_scroll = self.project_cursor - visible + 1

    def visible_project_rows(self) -> int:
        max_y, _ = self.stdscr.getmaxyx()
        return max(1, max_y - self.HEADER_LINES - self.FOOTER_LINES)

    def toggle_project_selection(self, idx: int) -> None:
        if idx in self.project_selected:
            self.project_selected.remove(idx)
        else:
            self.project_selected.add(idx)

    def current_project_keys(self) -> List[str]:
        if not self.project_choices:
            return []
        if not self.project_selected:
            return [choice.key for choice in self.project_choices]
        ordered = sorted(self.project_selected)
        return [self.project_choices[idx].key for idx in ordered]

    def handle_issue_type_key(self, key: int) -> None:
        if key in (curses.KEY_UP, ord("k")):
            self.move_issue_type_cursor(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.move_issue_type_cursor(1)
        elif key == curses.KEY_PPAGE:
            self.move_issue_type_cursor(-self.visible_project_rows())
        elif key == curses.KEY_NPAGE:
            self.move_issue_type_cursor(self.visible_project_rows())
        elif key in (32, ord("x")):
            self.toggle_issue_type_selection(self.issue_type_cursor)
        elif key == ord("a"):
            if len(self.issue_type_selected) == len(self.issue_type_choices):
                self.issue_type_selected.clear()
            else:
                self.issue_type_selected = set(range(len(self.issue_type_choices)))
        elif key == ord("c"):
            self.issue_type_selected.clear()
        elif key in (10, curses.KEY_ENTER):
            self.mode = "filter_overview"
            self.status_line = "Issue type selection updated."
        elif key == ord("m"):
            self.prompt_min_size()
        elif key == ord("b"):
            self.prompt_before_date()

    def move_issue_type_cursor(self, delta: int) -> None:
        if not self.issue_type_choices:
            return
        self.issue_type_cursor = max(
            0, min(self.issue_type_cursor + delta, len(self.issue_type_choices) - 1)
        )
        if self.issue_type_cursor < self.issue_type_scroll:
            self.issue_type_scroll = self.issue_type_cursor
        visible = self.visible_project_rows()
        if self.issue_type_cursor >= self.issue_type_scroll + visible:
            self.issue_type_scroll = self.issue_type_cursor - visible + 1

    def toggle_issue_type_selection(self, idx: int) -> None:
        if idx in self.issue_type_selected:
            self.issue_type_selected.remove(idx)
        else:
            self.issue_type_selected.add(idx)

    def fetch_and_update_rows(self) -> bool:
        projects = self.current_project_keys()
        if not projects:
            self.status_line = "No Jira projects available. Add keys or update your permissions."
            return False
        if not self._validate_before():
            return False
        self._draw_loading("Loading attachments...")
        try:
            records = self._fetch_records(projects)
        except Exception as exc:
            self.status_line = f"Failed to load attachments: {exc}"
            return False
        self._update_rows(records)
        if not self.rows:
            self.status_line = "No attachments matched the selected project(s) and filters."
        else:
            self.status_line = f"Loaded {len(self.rows)} attachment(s)."
        return True

    def _fetch_records(self, project_keys: Sequence[str]) -> List[AttachmentRecord]:
        fetch_args = argparse.Namespace(
            before=self.before,
            min_size_mb=self.min_size_mb,
            projects=list(project_keys),
            issue_types=self.current_issue_types(),
            max_issues=self.max_issues,
            verbose=self.verbose,
            attachment_ids=None,
        )
        log_fn: LogFn = self._log_message
        verbose_log = log_fn if self.verbose else None
        return collect_attachments(
            self.client,
            self.config,
            fetch_args,
            log=log_fn,
            verbose_log=verbose_log,
        )

    def _update_rows(self, records: List[AttachmentRecord]) -> None:
        tracked = {item.attachment_id: item for item in self.state_manager.list()}
        updated_rows: List[InteractiveRow] = []
        for record in records:
            if record.attachment_id in tracked:
                state = tracked[record.attachment_id]
                updated_rows.append(
                    InteractiveRow(record=record, status=state.status, error=state.error)
                )
            else:
                updated_rows.append(InteractiveRow(record=record, status="UNTRACKED", error=None))
        self.rows = updated_rows
        self.cursor = 0
        self.scroll = 0
        self.selected.clear()
        self.anchor = None
        if not self.rows:
            self.message = "No attachments matched. Press 'p' to adjust filters."
        else:
            self.message = "Press 'd' to delete selected attachments."

    def _log_message(self, message: str) -> None:
        self.status_line = message

    def enter_project_mode(self) -> None:
        if not self.project_choices:
            self.status_line = "No projects available."
            return
        self.mode = "project_picker"
        self.status_line = "Select one or more projects (or leave empty for all)."

    def enter_type_mode(self) -> None:
        if not self.issue_type_choices:
            self.status_line = "Issue type metadata not available."
            return
        self.mode = "type_picker"
        self.status_line = "Select one or more issue types (or leave empty for all)."

    def project_filter_label(self) -> str:
        total = len(self.project_choices)
        if total == 0:
            return "No projects"
        if not self.project_selected or len(self.project_selected) == total:
            return "All projects"
        if len(self.project_selected) == 1:
            idx = next(iter(self.project_selected))
            return self.project_choices[idx].key
        return f"{len(self.project_selected)} projects"

    def issue_type_filter_label(self) -> str:
        total = len(self.issue_type_choices)
        if total == 0:
            return "All issue types"
        if not self.issue_type_selected or len(self.issue_type_selected) == total:
            return "All issue types"
        if len(self.issue_type_selected) == 1:
            idx = next(iter(self.issue_type_selected))
            return self.issue_type_choices[idx].name
        return f"{len(self.issue_type_selected)} issue types"

    def draw_filter_overview(self) -> None:
        max_y, max_x = self.stdscr.getmaxyx()
        title = "Interactive Jira Attachment Cleaner — Filters"
        self.stdscr.addnstr(0, 0, title, max_x - 1, curses.A_BOLD)
        instructions = (
            "[Enter] load  [p] projects  [t] types  [m] min size  [b] before date  [q] quit"
        )
        self.stdscr.addnstr(1, 0, instructions, max_x - 1)
        self.stdscr.hline(2, 0, ord("-"), max_x)
        filters = [
            ("Projects", self.project_filter_label()),
            ("Issue types", self.issue_type_filter_label()),
            ("Min size", f"{self.min_size_mb:.2f} MB"),
            ("Before date", self.before),
        ]
        for idx, (label, value) in enumerate(filters, start=0):
            line = f"{label:<12}: {value}"
            self.stdscr.addnstr(self.HEADER_LINES - 1 + idx, 0, line, max_x - 1)
        footer_y = max_y - self.FOOTER_LINES
        if footer_y >= self.HEADER_LINES:
            self.stdscr.hline(footer_y, 0, ord("-"), max_x)
            msg_line = min(max_y - 2, footer_y + 1)
            status_line = min(max_y - 1, footer_y + 2)
            self.stdscr.addnstr(msg_line, 0, self.message[: max_x - 1], max_x - 1)
            if self.status_line:
                self.stdscr.addnstr(status_line, 0, self.status_line[: max_x - 1], max_x - 1)

    def draw_project_selector(self) -> None:
        max_y, max_x = self.stdscr.getmaxyx()
        title = "Select Projects"
        self.stdscr.addnstr(0, 0, title, max_x - 1, curses.A_BOLD)
        instructions = (
            "[↑/↓] move  [Space] toggle  [a] all  [c] clear  [m] min size  [b] before  [Enter] done  [q] quit"
        )
        self.stdscr.addnstr(1, 0, instructions, max_x - 1)
        self.stdscr.hline(2, 0, ord("-"), max_x)
        header = "Idx  Sel  Project"
        self.stdscr.addnstr(3, 0, header, max_x - 1, curses.A_UNDERLINE)
        visible = self.visible_project_rows()
        for offset in range(visible):
            idx = self.project_scroll + offset
            if idx >= len(self.project_choices):
                break
            choice = self.project_choices[idx]
            label = choice.key
            if choice.name and choice.name != choice.key:
                label = f"{choice.key} — {choice.name}"
            checkbox = "[x]" if idx in self.project_selected else "[ ]"
            line = f"{idx + 1:>4}  {checkbox}  {self._trim(label, max_x - 12)}"
            attr = curses.A_REVERSE if idx == self.project_cursor else curses.A_NORMAL
            self.stdscr.addnstr(self.HEADER_LINES + offset, 0, line[: max_x - 1], max_x - 1, attr)
        footer_y = max_y - self.FOOTER_LINES
        if footer_y >= self.HEADER_LINES:
            self.stdscr.hline(footer_y, 0, ord("-"), max_x)
            msg_line = min(max_y - 2, footer_y + 1)
            status_line = min(max_y - 1, footer_y + 2)
            self.stdscr.addnstr(
                msg_line,
                0,
                "Leave the selection empty to include every accessible project."[: max_x - 1],
                max_x - 1,
            )
            footer = f"{self.project_filter_label()} — Min size {self.min_size_mb:.2f} MB"
            self.stdscr.addnstr(status_line, 0, footer[: max_x - 1], max_x - 1)

    def draw_issue_type_selector(self) -> None:
        max_y, max_x = self.stdscr.getmaxyx()
        title = "Select Issue Types"
        self.stdscr.addnstr(0, 0, title, max_x - 1, curses.A_BOLD)
        instructions = (
            "[↑/↓] move  [Space] toggle  [a] all  [c] clear  [m] min size  [b] before  [Enter] done  [q] quit"
        )
        self.stdscr.addnstr(1, 0, instructions, max_x - 1)
        self.stdscr.hline(2, 0, ord("-"), max_x)
        header = "Idx  Sel  Issue type"
        self.stdscr.addnstr(3, 0, header, max_x - 1, curses.A_UNDERLINE)
        visible = self.visible_project_rows()
        for offset in range(visible):
            idx = self.issue_type_scroll + offset
            if idx >= len(self.issue_type_choices):
                break
            choice = self.issue_type_choices[idx]
            checkbox = "[x]" if idx in self.issue_type_selected else "[ ]"
            line = f"{idx + 1:>4}  {checkbox}  {self._trim(choice.name, max_x - 12)}"
            attr = curses.A_REVERSE if idx == self.issue_type_cursor else curses.A_NORMAL
            self.stdscr.addnstr(self.HEADER_LINES + offset, 0, line[: max_x - 1], max_x - 1, attr)
        footer_y = max_y - self.FOOTER_LINES
        if footer_y >= self.HEADER_LINES:
            self.stdscr.hline(footer_y, 0, ord("-"), max_x)
            msg_line = min(max_y - 2, footer_y + 1)
            status_line = min(max_y - 1, footer_y + 2)
            self.stdscr.addnstr(
                msg_line,
                0,
                "Leave the list empty to include all Jira issue types."[: max_x - 1],
                max_x - 1,
            )
            footer = self.issue_type_filter_label()
            self.stdscr.addnstr(status_line, 0, footer[: max_x - 1], max_x - 1)

    def _draw_loading(self, text: str) -> None:
        self.stdscr.erase()
        max_y, max_x = self.stdscr.getmaxyx()
        y = max_y // 2
        x = max(0, (max_x - len(text)) // 2)
        self.stdscr.addnstr(y, x, text, max_x - 1, curses.A_BOLD)
        self.stdscr.refresh()

    def prompt_min_size(self) -> None:
        prompt = "Enter minimum size in MB: "
        curses.echo()
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        max_y, max_x = self.stdscr.getmaxyx()
        self.stdscr.move(max_y - 1, 0)
        self.stdscr.clrtoeol()
        self.stdscr.addnstr(max_y - 1, 0, prompt, max_x - 1)
        self.stdscr.refresh()
        max_input = max(1, max_x - len(prompt) - 1)
        try:
            raw = self.stdscr.getstr(max_y - 1, min(len(prompt), max_x - 1), max_input)
        except curses.error:
            raw = b""
        finally:
            curses.noecho()
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        text = raw.decode().strip()
        if not text:
            self.status_line = "Minimum size unchanged."
            return
        try:
            new_value = float(text)
            if new_value <= 0:
                raise ValueError
        except ValueError:
            self.status_line = "Invalid minimum size."
            return
        self.min_size_mb = new_value
        if self.mode == "attachments":
            if self.busy:
                self.pending_reload = True
                self.status_line = "Minimum size updated. Reloading after processing completes."
            else:
                self.refresh_rows()
        else:
            self.status_line = f"Minimum size set to {new_value:.2f} MB."

    def refresh_rows(self) -> None:
        if self.mode != "attachments":
            return
        self.fetch_and_update_rows()

    def current_issue_types(self) -> List[str]:
        if not self.issue_type_choices:
            return []
        if not self.issue_type_selected or len(self.issue_type_selected) == len(self.issue_type_choices):
            return []
        ordered = sorted(self.issue_type_selected)
        return [self.issue_type_choices[idx].name for idx in ordered]

    def prompt_before_date(self) -> None:
        prompt = "Enter cutoff date (YYYY-MM-DD): "
        curses.echo()
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        max_y, max_x = self.stdscr.getmaxyx()
        self.stdscr.move(max_y - 1, 0)
        self.stdscr.clrtoeol()
        self.stdscr.addnstr(max_y - 1, 0, prompt, max_x - 1)
        self.stdscr.refresh()
        max_input = max(1, max_x - len(prompt) - 1)
        try:
            raw = self.stdscr.getstr(max_y - 1, min(len(prompt), max_x - 1), max_input)
        except curses.error:
            raw = b""
        finally:
            curses.noecho()
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        text = raw.decode().strip()
        if not text:
            self.status_line = "Date unchanged."
            return
        try:
            parse_before_date(text)
        except ValueError:
            self.status_line = "Invalid date format. Use YYYY-MM-DD."
            return
        self.before = text
        if self.mode == "attachments":
            if self.busy:
                self.pending_reload = True
                self.status_line = "Date updated. Reloading after processing completes."
            else:
                self.refresh_rows()
        else:
            self.status_line = f"Date set to {text}."

    def _validate_before(self) -> bool:
        try:
            parse_before_date(self.before)
        except ValueError:
            self.status_line = "Invalid 'before' date. Use YYYY-MM-DD."
            return False
        return True


if __name__ == "__main__":
    main()
