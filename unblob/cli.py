#!/usr/bin/env python3
import atexit
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from structlog import get_logger

from unblob.models import DirectoryHandlers, Handlers, ProcessResult
from unblob.plugins import UnblobPluginManager
from unblob.report import ChunkReport, Severity, StatReport, UnknownChunkReport

from .cli_options import verbosity_option
from .dependencies import get_dependencies, pretty_format_dependencies
from .handlers import BUILTIN_DIR_HANDLERS, BUILTIN_HANDLERS
from .logging import configure_logger
from .processing import (
    DEFAULT_DEPTH,
    DEFAULT_PROCESS_NUM,
    DEFAULT_SKIP_MAGIC,
    ExtractionConfig,
    process_file,
)

logger = get_logger()


def restore_cursor():
    # Restore cursor visibility
    sys.stdout.write("\033[?25h")  # ANSI escape code to show cursor


def get_version():
    return version("unblob")


def show_version(
    ctx: click.Context, _param: click.Option, value: bool  # noqa: FBT001
) -> None:
    if not value or ctx.resilient_parsing:
        return
    click.echo(get_version())
    ctx.exit(code=0)


def show_external_dependencies(
    ctx: click.Context, _param: click.Option, value: bool  # noqa: FBT001
) -> None:
    if not value or ctx.resilient_parsing:
        return

    plugin_manager = ctx.params["plugin_manager"]
    plugins_path = ctx.params.get(
        "plugins_path"
    )  # may not exist, depends on parameter order...
    plugin_manager.import_plugins(plugins_path)
    extra_handlers = plugin_manager.load_handlers_from_plugins()
    handlers = ctx.params["handlers"] + tuple(extra_handlers)

    extra_dir_handlers = plugin_manager.load_dir_handlers_from_plugins()
    dir_handlers = ctx.params["dir_handlers"] + tuple(extra_dir_handlers)

    dependencies = get_dependencies(handlers, dir_handlers)
    text = pretty_format_dependencies(dependencies)
    exit_code = 0 if all(dep.is_installed for dep in dependencies) else 1

    click.echo(text)
    ctx.exit(code=exit_code)


def get_help_text():
    dependencies = get_dependencies(BUILTIN_HANDLERS, BUILTIN_DIR_HANDLERS)
    lines = [
        "A tool for getting information out of any kind of binary blob.",
        "",
        "You also need these extractor commands to be able to extract the supported file types:",
        ", ".join(dep.command for dep in dependencies),
        "",
        "NOTE: Some older extractors might not be compatible.",
    ]
    return "\n".join(lines)


class UnblobContext(click.Context):
    def __init__(
        self,
        *args,
        handlers: Optional[Handlers] = None,
        dir_handlers: Optional[DirectoryHandlers] = None,
        plugin_manager: Optional[UnblobPluginManager] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        handlers = handlers or BUILTIN_HANDLERS
        dir_handlers = dir_handlers or BUILTIN_DIR_HANDLERS
        plugin_manager = plugin_manager or UnblobPluginManager()

        self.params["handlers"] = handlers
        self.params["dir_handlers"] = dir_handlers
        self.params["plugin_manager"] = plugin_manager


@click.command(
    help=get_help_text(), context_settings=dict(help_option_names=["--help", "-h"])
)
@click.argument(
    "file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True, resolve_path=True),
    required=True,
)
@click.option(
    "-e",
    "--extract-dir",
    "extract_root",
    type=click.Path(path_type=Path, dir_okay=True, file_okay=False, resolve_path=True),
    default=Path.cwd(),
    help="Extract the files to this directory. Will be created if doesn't exist.",
)
@click.option(
    "-f",
    "--force",
    is_flag=True,
    show_default=True,
    help="Force extraction even if outputs already exist (they are removed).",
)
@click.option(
    "-d",
    "--depth",
    default=DEFAULT_DEPTH,
    type=click.IntRange(1),
    show_default=True,
    help="Recursion depth. How deep should we extract containers.",
)
@click.option(
    "-n",
    "--entropy-depth",
    type=click.IntRange(0),
    default=1,
    show_default=True,
    help=(
        "Entropy calculation depth. How deep should we calculate entropy for unknown files? "
        "1 means input files only, 0 turns it off."
    ),
)
@click.option(
    "-P",
    "--plugins-path",
    type=click.Path(path_type=Path, exists=True, resolve_path=True),
    default=None,
    help="Load plugins from the provided path.",
    show_default=True,
)
@click.option(
    "-S",
    "--skip-magic",
    "skip_magic",
    type=click.STRING,
    default=DEFAULT_SKIP_MAGIC,
    help="Skip processing files with given magic prefix",
    show_default=True,
    multiple=True,
)
@click.option(
    "-p",
    "--process-num",
    "process_num",
    type=click.IntRange(1),
    default=DEFAULT_PROCESS_NUM,
    help="Number of worker processes to process files parallelly.",
    show_default=True,
)
@click.option(
    "--report",
    "report_file",
    type=click.Path(path_type=Path),
    help="File to store metadata generated during the extraction process (in JSON format).",
)
@click.option(
    "--log",
    "log_path",
    default=Path("unblob.log"),
    type=click.Path(path_type=Path),
    help="File to save logs (in text format). Defaults to unblob.log.",
)
@click.option(
    "-s",
    "--skip_extraction",
    "skip_extraction",
    is_flag=True,
    show_default=True,
    help="Only carve chunks and skip further extraction",
)
@click.option(
    "-k",
    "--keep-extracted-chunks",
    "keep_extracted_chunks",
    is_flag=True,
    show_default=True,
    help="Keep extracted chunks",
)
@verbosity_option
@click.option(
    "--show-external-dependencies",
    help="Shows commands needs to be available for unblob to work properly",
    is_flag=True,
    callback=show_external_dependencies,
    expose_value=False,
)
@click.option(
    "--version",
    help="Shows unblob version",
    is_flag=True,
    callback=show_version,
    expose_value=False,
)
def cli(
    file: Path,
    extract_root: Path,
    report_file: Optional[Path],
    log_path: Path,
    force: bool,  # noqa: FBT001
    process_num: int,
    depth: int,
    entropy_depth: int,
    skip_magic: Iterable[str],
    skip_extraction: bool,  # noqa: FBT001
    keep_extracted_chunks: bool,  # noqa: FBT001
    handlers: Handlers,
    dir_handlers: DirectoryHandlers,
    plugins_path: Optional[Path],
    plugin_manager: UnblobPluginManager,
    verbose: int,
) -> ProcessResult:
    configure_logger(verbose, extract_root, log_path)

    plugin_manager.import_plugins(plugins_path)
    extra_handlers = plugin_manager.load_handlers_from_plugins()
    handlers += tuple(extra_handlers)

    extra_dir_handlers = plugin_manager.load_dir_handlers_from_plugins()
    dir_handlers += tuple(extra_dir_handlers)

    config = ExtractionConfig(
        extract_root=extract_root,
        force_extract=force,
        max_depth=depth,
        entropy_depth=entropy_depth,
        entropy_plot=bool(verbose >= 3),
        skip_extraction=skip_extraction,
        skip_magic=skip_magic,
        process_num=process_num,
        handlers=handlers,
        dir_handlers=dir_handlers,
        keep_extracted_chunks=keep_extracted_chunks,
        verbose=verbose,
    )

    logger.info("Start processing file", file=file)
    process_results = process_file(config, file, report_file)
    if verbose == 0:
        print_report(process_results)
    return process_results


cli.context_class = UnblobContext


def get_exit_code_from_reports(reports: ProcessResult) -> int:
    severity_to_exit_code = [
        (Severity.ERROR, 1),
        (Severity.WARNING, 0),
    ]
    severities = {error.severity for error in reports.errors}

    for severity, exit_code in severity_to_exit_code:
        if severity in severities:
            return exit_code

    return 0


def human_size(size: float):
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.2f} {units[i]}"


def get_chunks_distribution(task_results: List) -> Dict:
    chunks_distribution = {"unknown": 0}
    for task_result in task_results:
        chunk_reports = [
            report
            for report in task_result.reports
            if isinstance(report, (ChunkReport, UnknownChunkReport))
        ]

        for chunk_report in chunk_reports:
            if isinstance(chunk_report, UnknownChunkReport):
                chunks_distribution["unknown"] += chunk_report.size
                continue
            if chunk_report.handler_name not in chunks_distribution:
                chunks_distribution[chunk_report.handler_name] = 0
            chunks_distribution[chunk_report.handler_name] += chunk_report.size

    return chunks_distribution


def get_size_report(task_results: List) -> Tuple[int, int, int, int]:
    total_files = 0
    total_dirs = 0
    total_links = 0
    extracted_size = 0

    for task_result in task_results:
        stat_reports = list(
            filter(lambda x: isinstance(x, StatReport), task_result.reports)
        )
        for stat_report in stat_reports:
            total_files += stat_report.is_file
            total_dirs += stat_report.is_dir
            total_links += stat_report.is_link
            if stat_report.is_file:
                extracted_size += stat_report.size

    return total_files, total_dirs, total_links, extracted_size


def print_report(reports: ProcessResult):
    total_files, total_dirs, total_links, extracted_size = get_size_report(
        reports.results
    )
    chunks_distribution = get_chunks_distribution(reports.results)

    valid_size = 0
    total_size = 0
    for handler, size in chunks_distribution.items():
        if handler != "unknown":
            valid_size += size
        total_size += size

    if total_size == 0:
        return

    summary = Panel(
        f"""Extracted files: [#00FFC8]{total_files}[/#00FFC8]
Extracted directories: [#00FFC8]{total_dirs}[/#00FFC8]
Extracted links: [#00FFC8]{total_links}[/#00FFC8]
Extraction directory size: [#00FFC8]{human_size(extracted_size)}[/#00FFC8]
Chunks identification ratio: [#00FFC8]{(valid_size/total_size) * 100:0.2f}%[/#00FFC8]""",
        subtitle="Summary",
        title=f"unblob ({get_version()})",
    )

    console = Console()
    console.print(summary)

    chunks_table = Table(title="Chunks distribution")
    chunks_table.add_column("Chunk type", justify="left", style="#00FFC8", no_wrap=True)
    chunks_table.add_column("Size", justify="center", style="#00FFC8", no_wrap=True)
    chunks_table.add_column("Ratio", justify="center", style="#00FFC8", no_wrap=True)

    for handler, size in sorted(
        chunks_distribution.items(), key=lambda item: item[1], reverse=True
    ):
        chunks_table.add_row(
            handler.upper(), human_size(size), f"{(size/total_size) * 100:0.2f}%"
        )

    console.print(chunks_table)

    if len(reports.errors):
        errors_table = Table(title="Encountered errors")
        errors_table.add_column("Severity", justify="left", style="cyan", no_wrap=True)
        errors_table.add_column("Name", justify="left", style="cyan", no_wrap=True)

        for error in reports.errors:
            errors_table.add_row(str(error.severity), error.__class__.__name__)
        console.print(errors_table)


def main():
    try:
        # Click argument parsing
        ctx = cli.make_context("unblob", sys.argv[1:])
    except click.ClickException as e:
        e.show()
        sys.exit(e.exit_code)
    except click.exceptions.Exit as e:
        sys.exit(e.exit_code)
    except Exception:
        logger.exception("Unhandled exception during unblob")
        sys.exit(1)

    try:
        with ctx:
            reports = cli.invoke(ctx)
    except Exception:
        logger.exception("Unhandled exception during unblob")
        sys.exit(1)
    finally:
        atexit.register(restore_cursor)

    sys.exit(get_exit_code_from_reports(reports))


if __name__ == "__main__":
    main()
