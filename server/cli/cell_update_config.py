"""An explicit nonsecret settings overlay for a cell's target configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from server.code.domain.index import repos
from server.core import cell
from server.router import fingerprints
from server.settings import Settings
from server.settings._file import SettingsFileError, read_settings_file
from server.settings._live import coerce


class _Target(Settings):
    """Validate the supplied complete model without rereading external sources."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def read_target(path: Path, current: Settings) -> Settings:
    """Overlay FILE on the CLI's effective settings; install neither the file nor values."""
    try:
        values = read_settings_file(str(path), set(Settings.model_fields))
    except Exception:  # noqa: BLE001 — parser failures must not expose target file values.
        # YAML constructors can raise builtin errors that contain their input.
        raise SettingsFileError(
            f"{path}: invalid target settings file; use version: 1, unique setting names "
            "and nonsecret values"
        ) from None
    checked: dict[str, Any] = {}
    for name, value in values.items():
        try:
            checked[name] = coerce(Settings, name, value)
        except ValidationError:
            raise SettingsFileError(f"{path}: {name} has an invalid target value") from None
    try:
        return _Target(**(current.model_dump() | checked))
    except ValidationError:
        # Validators can embed supplied values in their own messages despite hidden inputs.
        raise SettingsFileError(
            f"{path}: invalid target configuration; check the setting constraints"
        ) from None


def target_fingerprint(
    target: Settings, deployment_id: str, registered: dict[str, Any]
) -> dict[str, Any]:
    """Validate the candidate without admitting it or changing process settings."""
    value = cell.as_value(
        cell.fingerprint_for(target, deployment_id, repos.operator_orgs_for(target))
    )
    if reason := fingerprints.registration_refusal(value, registered):
        raise ValueError(reason)
    if value["fields"]["scim"]:
        from server.auth.scim_routes import scim_settings_problem

        problem = scim_settings_problem(
            target.scim_token,
            target.oauth_login_provider,
            target.auth_actually_required(),
            cell_name=registered["cell"],
            first=(registered["is_first"], registered["cell"]),
        )
        if problem:
            raise ValueError(problem)
    if reason := fingerprints.placement_refusal(registered["cell"], "candidate", value):
        raise ValueError(reason)
    return value
