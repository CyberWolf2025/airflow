#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import itertools
import logging.config
import os
import sys
import warnings
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Generic, TextIO, TypeVar

import msgspec
import structlog

if TYPE_CHECKING:
    from structlog.typing import EventDict, ExcInfo, Processor


__all__ = [
    "configure_logging",
    "reset_logging",
]


def exception_group_tracebacks(format_exception: Callable[[ExcInfo], list[dict[str, Any]]]) -> Processor:
    # Make mypy happy
    if not hasattr(__builtins__, "BaseExceptionGroup"):
        T = TypeVar("T")

        class BaseExceptionGroup(Generic[T]):
            exceptions: list[T]

    def _exception_group_tracebacks(logger: Any, method_name: Any, event_dict: EventDict) -> EventDict:
        if exc_info := event_dict.get("exc_info", None):
            group: BaseExceptionGroup[Exception] | None = None
            if exc_info is True:
                # `log.exception('mesg")` case
                exc_info = sys.exc_info()
                if exc_info[0] is None:
                    exc_info = None

            if (
                isinstance(exc_info, tuple)
                and len(exc_info) == 3
                and isinstance(exc_info[1], BaseExceptionGroup)
            ):
                group = exc_info[1]
            elif isinstance(exc_info, BaseExceptionGroup):
                group = exc_info

            if group:
                # Only remove it from event_dict if we handle it
                del event_dict["exc_info"]
                event_dict["exception"] = list(
                    itertools.chain.from_iterable(
                        format_exception((type(exc), exc, exc.__traceback__))  # type: ignore[attr-defined,arg-type]
                        for exc in (*group.exceptions, group)
                    )
                )

        return event_dict

    return _exception_group_tracebacks


def logger_name(logger: Any, method_name: Any, event_dict: EventDict) -> EventDict:
    if logger_name := event_dict.pop("logger_name", None):
        event_dict.setdefault("logger", logger_name)
    return event_dict


def redact_jwt(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    for k, v in event_dict.items():
        if isinstance(v, str) and v.startswith("eyJ"):
            event_dict[k] = "eyJ***"
    return event_dict


def drop_positional_args(logger: Any, method_name: Any, event_dict: EventDict) -> EventDict:
    event_dict.pop("positional_args", None)
    return event_dict


class StdBinaryStreamHandler(logging.StreamHandler):
    """A logging.StreamHandler that sends logs as binary JSON over the given stream."""

    stream: BinaryIO

    def __init__(self, stream: BinaryIO):
        super().__init__(stream)

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            buffer = bytearray(msg, "utf-8", "backslashreplace")

            buffer += b"\n"

            stream = self.stream
            stream.write(buffer)
            self.flush()
        except RecursionError:  # See issue 36272
            raise
        except Exception:
            self.handleError(record)


@cache
def logging_processors(
    enable_pretty_log: bool,
):
    if enable_pretty_log:
        timestamper = structlog.processors.MaybeTimeStamper(fmt="%Y-%m-%d %H:%M:%S.%f")
    else:
        timestamper = structlog.processors.MaybeTimeStamper(fmt="iso")

    processors: list[structlog.typing.Processor] = [
        timestamper,
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        logger_name,
        redact_jwt,
        structlog.processors.StackInfoRenderer(),
    ]

    # Imports to suppress showing code from these modules. We need the import to get the filepath for
    # structlog to ignore.
    import contextlib

    import click
    import httpcore
    import httpx

    suppress = (
        click,
        contextlib,
        httpx,
        httpcore,
        httpx,
    )

    if enable_pretty_log:
        rich_exc_formatter = structlog.dev.RichTracebackFormatter(
            # These values are picked somewhat arbitrarily to produce useful-but-compact tracebacks. If
            # we ever need to change these then they should be configurable.
            extra_lines=0,
            max_frames=30,
            indent_guides=False,
            suppress=suppress,
        )
        my_styles = structlog.dev.ConsoleRenderer.get_default_level_styles()
        my_styles["debug"] = structlog.dev.CYAN

        console = structlog.dev.ConsoleRenderer(
            exception_formatter=rich_exc_formatter, level_styles=my_styles
        )
        processors.append(console)
        return processors, {
            "timestamper": timestamper,
            "console": console,
        }
    else:
        # Imports to suppress showing code from these modules
        import contextlib

        import click
        import httpcore
        import httpx

        dict_exc_formatter = structlog.tracebacks.ExceptionDictTransformer(
            use_rich=False, show_locals=False, suppress=suppress
        )

        dict_tracebacks = structlog.processors.ExceptionRenderer(dict_exc_formatter)
        if hasattr(__builtins__, "BaseExceptionGroup"):
            exc_group_processor = exception_group_tracebacks(dict_exc_formatter)
            processors.append(exc_group_processor)
        else:
            exc_group_processor = None

        encoder = msgspec.json.Encoder()

        def json_dumps(msg, default):
            return encoder.encode(msg)

        def json_processor(logger: Any, method_name: Any, event_dict: EventDict) -> str:
            return encoder.encode(event_dict).decode("utf-8")

        json = structlog.processors.JSONRenderer(serializer=json_dumps)

        processors.extend(
            (
                dict_tracebacks,
                structlog.processors.UnicodeDecoder(),
                json,
            ),
        )

        return processors, {
            "timestamper": timestamper,
            "exc_group_processor": exc_group_processor,
            "dict_tracebacks": dict_tracebacks,
            "json": json_processor,
        }


@cache
def configure_logging(
    enable_pretty_log: bool = True,
    log_level: str = "DEBUG",
    output: BinaryIO | None = None,
    cache_logger_on_first_use: bool = True,
):
    """Set up struct logging and stdlib logging config."""
    if enable_pretty_log and output is not None:
        raise ValueError("output can only be set if enable_pretty_log is not")

    lvl = structlog.stdlib.NAME_TO_LEVEL[log_level.lower()]

    if enable_pretty_log:
        formatter = "colored"
    else:
        formatter = "plain"
    processors, named = logging_processors(enable_pretty_log)
    timestamper = named["timestamper"]

    pre_chain: list[structlog.typing.Processor] = [
        # Add the log level and a timestamp to the event_dict if the log entry
        # is not from structlog.
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
    ]

    # Don't cache the loggers during tests, it make it hard to capture them
    if "PYTEST_CURRENT_TEST" in os.environ:
        cache_logger_on_first_use = False

    color_formatter: list[structlog.typing.Processor] = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        drop_positional_args,
    ]
    std_lib_formatter: list[structlog.typing.Processor] = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        drop_positional_args,
    ]

    wrapper_class = structlog.make_filtering_bound_logger(lvl)
    if enable_pretty_log:
        structlog.configure(
            processors=processors,
            cache_logger_on_first_use=cache_logger_on_first_use,
            wrapper_class=wrapper_class,
        )
        color_formatter.append(named["console"])
    else:
        structlog.configure(
            processors=processors,
            cache_logger_on_first_use=cache_logger_on_first_use,
            wrapper_class=wrapper_class,
            logger_factory=structlog.BytesLoggerFactory(output),
        )

        if processor := named["exc_group_processor"]:
            pre_chain.append(processor)
        pre_chain.append(named["dict_tracebacks"])
        color_formatter.append(named["json"])
        std_lib_formatter.append(named["json"])

    global _warnings_showwarning

    if _warnings_showwarning is None:
        _warnings_showwarning = warnings.showwarning
        # Capture warnings and show them via structlog
        warnings.showwarning = _showwarning

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "plain": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "processors": std_lib_formatter,
                    "foreign_pre_chain": pre_chain,
                    "pass_foreign_args": True,
                },
                "colored": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "processors": color_formatter,
                    "foreign_pre_chain": pre_chain,
                    "pass_foreign_args": True,
                },
            },
            "handlers": {
                "default": {
                    "level": log_level.upper(),
                    "class": "logging.StreamHandler",
                    "formatter": formatter,
                },
                "to_supervisor": {
                    "level": log_level.upper(),
                    "()": StdBinaryStreamHandler,
                    "formatter": formatter,
                    "stream": output,
                },
            },
            "loggers": {
                # Set Airflow logging to the level requested, but most everything else at "INFO"
                "": {
                    "handlers": ["to_supervisor" if output else "default"],
                    "level": "INFO",
                    "propagate": True,
                },
                "airflow": {"level": log_level.upper()},
                # These ones are too chatty even at info
                "httpx": {"level": "WARN"},
                "sqlalchemy.engine": {"level": "WARN"},
            },
        }
    )


def reset_logging():
    global _warnings_showwarning
    warnings.showwarning = _warnings_showwarning
    configure_logging.cache_clear()


_warnings_showwarning: Any = None


def _showwarning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: TextIO | None = None,
    line: str | None = None,
) -> Any:
    """
    Redirects warnings to structlog so they appear in task logs etc.

    Implementation of showwarnings which redirects to logging, which will first
    check to see if the file parameter is None. If a file is specified, it will
    delegate to the original warnings implementation of showwarning. Otherwise,
    it will call warnings.formatwarning and will log the resulting string to a
    warnings logger named "py.warnings" with level logging.WARNING.
    """
    if file is not None:
        if _warnings_showwarning is not None:
            _warnings_showwarning(message, category, filename, lineno, file, line)
    else:
        log = structlog.get_logger(logger_name="py.warnings")
        log.warning(str(message), category=category.__name__, filename=filename, lineno=lineno)


def _prepare_log_folder(directory: Path, mode: int):
    """
    Prepare the log folder and ensure its mode is as configured.

    To handle log writing when tasks are impersonated, the log files need to
    be writable by the user that runs the Airflow command and the user
    that is impersonated. This is mainly to handle corner cases with the
    SubDagOperator. When the SubDagOperator is run, all of the operators
    run under the impersonated user and create appropriate log files
    as the impersonated user. However, if the user manually runs tasks
    of the SubDagOperator through the UI, then the log files are created
    by the user that runs the Airflow command. For example, the Airflow
    run command may be run by the `airflow_sudoable` user, but the Airflow
    tasks may be run by the `airflow` user. If the log files are not
    writable by both users, then it's possible that re-running a task
    via the UI (or vice versa) results in a permission error as the task
    tries to write to a log file created by the other user.

    We leave it up to the user to manage their permissions by exposing configuration for both
    new folders and new log files. Default is to make new log folders and files group-writeable
    to handle most common impersonation use cases. The requirement in this case will be to make
    sure that the same group is set as default group for both - impersonated user and main airflow
    user.
    """
    for parent in reversed(directory.parents):
        parent.mkdir(mode=mode, exist_ok=True)
    directory.mkdir(mode=mode, exist_ok=True)


def init_log_file(local_relative_path: str) -> Path:
    """
    Ensure log file and parent directories are created.

    Any directories that are missing are created with the right permission bits.

    See above ``_prepare_log_folder`` method for more detailed explanation.
    """
    # NOTE: This is duplicated from airflow.utils.log.file_task_handler:FileTaskHandler._init_file, but we
    # want to remove that
    from airflow.configuration import conf

    new_file_permissions = int(
        conf.get("logging", "file_task_handler_new_file_permissions", fallback="0o664"), 8
    )
    new_folder_permissions = int(
        conf.get("logging", "file_task_handler_new_folder_permissions", fallback="0o775"), 8
    )

    base_log_folder = conf.get("logging", "base_log_folder")
    full_path = Path(base_log_folder, local_relative_path)

    _prepare_log_folder(full_path.parent, new_folder_permissions)

    try:
        full_path.touch(new_file_permissions)
    except OSError as e:
        log = structlog.get_logger()
        log.warning("OSError while changing ownership of the log file. %s", e)

    return full_path
