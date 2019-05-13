import logging
import os
import re
from collections import defaultdict
from . import uris

from mypy.dmypy_server import Server
from mypy.dmypy_util import DEFAULT_STATUS_FILE
from mypy.options import Options
from mypy.main import parse_config_file
from mypy.version import __version__ as mypy_version
from typing import Set, Dict, Optional

from . import lsp
from contextlib import redirect_stderr
from io import StringIO

line_pattern = r"([^:]+):(?:(\d+):)?(?:(\d+):)? (\w+): (.*)"

log = logging.getLogger(__name__)
settings: Optional[Dict[str, object]] = None

def configuration_changed(config, workspace):
    global settings
    if not workspace.root_path:
        return
    if settings is not None:
        new_settings = config.settings()
        if new_settings != settings:
            workspace.show_message('Please reload window to update mypy configuration.')
        return

    settings = config.settings()
    options = Options()
    options.check_untyped_defs = True
    options.follow_imports = 'error'
    options.use_fine_grained_cache = True
    stderr_stream = StringIO()
    config_file = settings.get('configFile')
    if config_file == '':
        # Use empty string rather than null in vscode settings, so that it's shown in settings editor GUI.
        config_file = None
    log.info(f'Trying to read mypy config file from {config_file or "default locations"}')
    with redirect_stderr(stderr_stream):
        parse_config_file(options, config_file)
    stderr = stderr_stream.getvalue()
    if stderr:
        log.error(f'Error reading mypy config file:\n{stderr}')
        workspace.show_message(f'Error reading mypy config file:\n{stderr}')
    if options.config_file:
        log.info(f'Read mypy config from: {options.config_file}')
    else:
        log.info(f'Mypy configuration not read, using defaults.')
        if config_file:
            workspace.show_message(f'Mypy config file not found:\n{config_file}')

    options.show_column_numbers = True
    if options.follow_imports not in ('error', 'skip'):
        workspace.show_message(f"Cannot use follow_imports='{options.follow_imports}', using 'error' instead.")
        options.follow_imports = 'error'

    workspace.mypy_server = Server(options, DEFAULT_STATUS_FILE)

    mypy_check(workspace, config)

def mypy_check(workspace, config):
    if not workspace.root_path:
        return

    log.info('Checking mypy...')
    workspace.report_progress('$(gear~spin) mypy')
    try:
        if is_patched_mypy():
            def report_status(processed_targets: int) -> None:
                workspace.report_progress(f'$(gear~spin) mypy ({processed_targets})')
            workspace.mypy_server.status_callback = report_status

        targets = settings.get('targets') or ['.']
        targets = [os.path.join(workspace.root_path, target) for target in targets]
        result = workspace.mypy_server.cmd_check(targets)
        log.info(f'mypy done, exit code {result["status"]}')
        if result['err']:
            log.info(f'mypy stderr:\n{result["err"]}')
            workspace.show_message(f'Error running mypy: {result["err"]}')
        if result['out']:
            log.info(f'mypy stdout:\n{result["out"]}')
            publish_diagnostics(workspace, result['out'])
    except Exception as e:
        log.exception('Error in mypy check:')
        workspace.show_message(f'Error running mypy: {e}')
    except SystemExit as e:
        log.exception('Internal error running mypy:')
        workspace.show_message('Internal error running mypy. Open output pane for details.')
    finally:
        workspace.report_progress(None)
        if is_patched_mypy():
            workspace.mypy_server.status_callback = None

def parse_line(line):
    result = re.match(line_pattern, line)
    if result is None:
        log.info(f'Skipped unrecognized mypy line: {line}')
        return None, None

    path, lineno, offset, severity, msg = result.groups()
    lineno = int(lineno or 1)
    offset = int(offset or 0)

    errno = lsp.DiagnosticSeverity.Error if severity == 'error' else lsp.DiagnosticSeverity.Information
    diag = {
        'source': 'mypy',
        'range': {
            'start': {'line': lineno - 1, 'character': offset},
            # There may be a better solution, but mypy does not provide end
            'end': {'line': lineno - 1, 'character': offset}
        },
        'message': msg,
        'severity': errno
    }

    return path, diag


def parse_mypy_output(mypy_output):
    diagnostics = defaultdict(list)
    for line in mypy_output.splitlines():
        path, diag = parse_line(line)
        if diag:
            diagnostics[path].append(diag)

    return diagnostics


documents_with_diagnostics: Set[str] = set()

def publish_diagnostics(workspace, mypy_output):
    diagnostics_by_path = parse_mypy_output(mypy_output)
    previous_documents_with_diagnostics = documents_with_diagnostics.copy()
    documents_with_diagnostics.clear()
    for path, diagnostics in diagnostics_by_path.items():
        uri = uris.from_fs_path(os.path.join(workspace.root_path, path))
        documents_with_diagnostics.add(uri)
        # TODO: If mypy is really fast, it may finish before initialization is complete,
        #       and this call will have no effect. (?)
        workspace.publish_diagnostics(uri, diagnostics)

    documents_to_clear = previous_documents_with_diagnostics - documents_with_diagnostics
    for uri in documents_to_clear:
        workspace.publish_diagnostics(uri, [])

def is_patched_mypy():
    return 'langserver' in mypy_version