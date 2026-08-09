from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, Node, SequenceNode

from epub_news_feeder.models import Configuration

_PLACEHOLDER = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class ConfigError(Exception):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


def _reject_duplicate_keys(node: Node) -> None:
    if isinstance(node, MappingNode):
        keys: set[str] = set()
        for key_node, value_node in node.value:
            key = key_node.value
            if key in keys:
                raise ConfigError("CONFIG_INVALID", "Configuration is invalid")
            keys.add(key)
            _reject_duplicate_keys(value_node)
    elif isinstance(node, SequenceNode):
        for value_node in node.value:
            _reject_duplicate_keys(value_node)


def _resolve_placeholders(value: object, environment: Mapping[str, str]) -> object:
    if isinstance(value, str):
        match = _PLACEHOLDER.fullmatch(value)
        if match is not None:
            variable = match.group(1)
            if variable not in environment:
                raise ConfigError("CONFIG_ENV_MISSING", "Environment placeholder is unresolved")
            return environment[variable]
        if "${" in value:
            raise ConfigError("CONFIG_INVALID", "Configuration is invalid")
        return value
    if isinstance(value, list):
        return [_resolve_placeholders(item, environment) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_placeholders(item, environment)
            for key, item in cast(dict[object, object], value).items()
        }
    return value


def load_config(path: Path, *, environment: Mapping[str, str] | None = None) -> Configuration:
    if not path.is_file():
        raise ConfigError("CONFIG_NOT_FOUND", "Configuration file not found")

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ConfigError("CONFIG_READ_FAILED", "Configuration file could not be read") from error

    try:
        document = yaml.compose(text, Loader=yaml.SafeLoader)
        if document is None:
            raise ConfigError("CONFIG_INVALID", "Configuration is invalid")
        _reject_duplicate_keys(document)
        parsed = cast(object, yaml.safe_load(text))
    except ConfigError:
        raise
    except yaml.YAMLError as error:
        raise ConfigError("CONFIG_INVALID", "Configuration is invalid") from error

    resolved = _resolve_placeholders(parsed, environment if environment is not None else os.environ)
    try:
        return Configuration.model_validate(resolved)
    except ValidationError as error:
        raise ConfigError("CONFIG_INVALID", "Configuration is invalid") from error
