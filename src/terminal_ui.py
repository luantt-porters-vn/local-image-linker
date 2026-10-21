"""Consistent terminal output with a plain-text fallback for captured logs."""
import os
import re
import shutil
import sys
import textwrap
import threading
import time


TITLES = {
    'prepare': 'Prepare Cluster', 'paths': 'Repository Paths', 'build': 'Build Images',
    'build-share': 'Build & Share', 'share': 'Share Last Build', 'pull-shared': 'Pull Shared',
    'stop-sharing': 'Stop Sharing', 'up': 'Deploy Images', 'restore': 'Restore Cluster',
    'stop': 'Stop Services', 'status': 'Cluster Status', 'clean': 'Clean Images',
}


class TerminalUI:
    def __init__(self, stream=None):
        self.stream = stream
        self.lock = threading.RLock()
        self.started = time.monotonic()
        self.progress_active = False

    @property
    def output(self):
        return self.stream if self.stream is not None else sys.stdout

    @property
    def color(self):
        return self.output.isatty() and os.environ.get('TERM') != 'dumb' and 'NO_COLOR' not in os.environ

    @property
    def width(self):
        return max(32, min(110, shutil.get_terminal_size().columns - 1))

    def text(self, value):
        return re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', str(value))

    def paint(self, value, code):
        return f'\x1b[{code}m{value}\x1b[0m' if self.color else value

    def line(self, value=''):
        with self.lock:
            if self.progress_active:
                self.output.write('\r\x1b[2K')
                self.progress_active = False
            print(value, file=self.output, flush=True)

    def heading(self, command, targets):
        self.started = time.monotonic()
        self.line()
        self.line(self.paint('LOCAL IMAGE LINKER', '1;36'))
        self.line(self.paint(TITLES.get(command, self.text(command)), '1'))
        self.line(self.paint('-' * self.width, '2'))
        if command not in {'stop-sharing', 'pull-shared', 'prepare'}:
            self.detail('Targets', ', '.join(targets))

    def section(self, title):
        self.line()
        self.line(self.paint(self.text(title).upper(), '1;36'))

    def detail(self, label, value):
        prefix = f'  {self.text(label):<12} '
        rows = textwrap.wrap(self.text(value), width=self.width, initial_indent=prefix,
                             subsequent_indent=' ' * len(prefix), break_long_words=True,
                             break_on_hyphens=False) or [prefix]
        with self.lock:
            for row in rows:
                self.line(row)

    def message(self, label, value, color):
        prefix = f'  [{label}] '
        rows = textwrap.wrap(self.text(value), width=self.width, initial_indent=prefix,
                             subsequent_indent=' ' * len(prefix), break_long_words=True,
                             break_on_hyphens=False)
        with self.lock:
            for index, row in enumerate(rows):
                if index == 0:
                    row = row.replace(f'[{label}]', self.paint(f'[{label}]', color), 1)
                self.line(row)

    def info(self, value):
        self.message('INFO', value, '36')

    def step(self, value):
        self.message('STEP', value, '34')

    def success(self, value):
        self.message('OK', value, '32')

    def warning(self, value):
        self.message('WARN', value, '33')

    def error(self, value):
        self.message('ERROR', value, '31')

    def prompt(self, value):
        self.line()
        return input(f'  {self.text(value)}\n  > ').strip()

    def share_link(self, link):
        self.section('Share Link')
        self.line('  ' + link)
        self.line()

    def progress(self, current, total, label='Download'):
        ratio = min(1, current / total) if total else 0
        if self.width < 64:
            self.detail(label, f'{ratio:.0%} | {current / (1024 * 1024):.1f} / {total / (1024 * 1024):.1f} MiB')
            return
        cells = max(8, min(24, self.width - 52))
        completed = int(cells * ratio)
        row = (f'  {self.text(label)} [{"#" * completed}{"-" * (cells - completed)}] '
               f'{ratio:>4.0%}  {current / (1024 * 1024):.1f} / {total / (1024 * 1024):.1f} MiB')
        if not self.output.isatty() or os.environ.get('TERM') == 'dumb':
            self.line(row)
            return
        with self.lock:
            self.output.write('\r\x1b[2K' + row[:self.width])
            self.progress_active = ratio < 1
            if not self.progress_active:
                self.output.write('\n')
            self.output.flush()

    def finish(self, value='Task completed'):
        self.line()
        self.success(f'{value} ({time.monotonic() - self.started:.1f}s)')


ui = TerminalUI()