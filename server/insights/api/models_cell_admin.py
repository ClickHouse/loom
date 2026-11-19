"""Closed inputs for administration of one verified cell."""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator


def _text(value: str, maximum: int, *, empty: bool = False, controls: bool = False) -> str:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError("Text must be UTF-8") from None
    if (not empty and not size) or size > maximum or "\0" in value:
        raise ValueError("Text is empty, oversized or contains NUL")
    if not controls and any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Identifier contains control characters")
    return value


def _uuid(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError("Expected a canonical UUID") from None
    if str(parsed) != value:
        raise ValueError("Expected a canonical UUID")
    return value


def _finite_json(value: Any) -> Any:
    pending = [value]
    while pending:
        item = pending.pop()
        if item is None or type(item) in (bool, int):
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("Setting input must contain only finite JSON values")
        elif type(item) is str:
            try:
                item.encode("utf-8")
            except UnicodeError:
                raise ValueError("Setting input must be UTF-8") from None
        elif type(item) is list:
            pending.extend(item)
        elif type(item) is dict and all(type(key) is str for key in item):
            pending.extend(item.keys())
            pending.extend(item.values())
        else:
            raise ValueError("Setting input must be JSON")
    return value


Name = Annotated[str, AfterValidator(lambda value: _text(value, 256))]
ShortName = Annotated[str, AfterValidator(lambda value: _text(value, 64))]
Prefix = Annotated[str, AfterValidator(lambda value: _text(value, 256, empty=True))]
PatternID = Annotated[str, AfterValidator(_uuid)]
Reason = Annotated[str, AfterValidator(lambda value: _text(value, 1024, empty=True, controls=True))]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class CellAdminModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class CellTargetContext(CellAdminModel):
    version: Literal[1]
    expected_cell: Name
    expected_deployment_id: Name
    expected_bot: Name

    @field_validator("version", mode="before")
    @classmethod
    def _version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported cell administration version")
        return value


class CellActionContext(CellTargetContext):
    run_id: PatternID
    attempt: int = Field(ge=1, le=2**31 - 1)
    command_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @field_validator("run_id")
    @classmethod
    def _run_id(cls, value: str) -> str:
        if UUID(value).version != 4:
            raise ValueError("Run identity must be a UUIDv4")
        return value


class CellCapabilitiesInput(CellAdminModel):
    context: CellTargetContext


class CellActionInput(CellAdminModel):
    context: CellActionContext

    @model_validator(mode="after")
    def _argument_size(self) -> CellActionInput:
        arguments = self.model_dump(exclude={"context"})
        try:
            encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError, OverflowError):
            raise ValueError("Arguments must be finite JSON") from None
        if len(encoded.encode("utf-8")) > 65536:
            raise ValueError("Arguments exceed the cell administration limit")
        return self


class CellSettingsListInput(CellActionInput):
    prefix: Prefix = ""


class CellSettingsSetInput(CellActionInput):
    name: Name
    # The owning setting coerces its raw value; model validation must not change it.
    value: Any = Field(default=None, repr=False)
    clear: bool = False
    expected: Any = Field(default=None, repr=False)

    @field_validator("value", "expected", mode="before")
    @classmethod
    def _json(cls, value: Any) -> Any:
        return _finite_json(value)

    @model_validator(mode="after")
    def _value_or_clear(self) -> CellSettingsSetInput:
        if self.clear and self.value is not None:
            raise ValueError("A cleared setting cannot carry a value")
        if not self.clear and "value" not in self.model_fields_set:
            raise ValueError("A setting change requires a value")
        return self


class CellSettingsHistoryInput(CellActionInput):
    name: Name
    limit: int = Field(default=50, ge=1, le=50)


class CellSigningKeysListInput(CellActionInput):
    pass


class CellSigningKeysRotateInput(CellActionInput):
    algorithm: Literal["HS256", "RS256"] = "HS256"
    activate_in: Nonnegative = 300.0


class CellSigningKeysRetireInput(CellActionInput):
    kid: Name


class CellSubjectRevokeInput(CellActionInput):
    subject: Annotated[str, AfterValidator(lambda value: _text(value, 512))]
    reason: Reason = Field(default="", repr=False)

    @field_validator("subject")
    @classmethod
    def _global_subject(cls, value: str) -> str:
        if "/user:" in value:
            raise ValueError("An org-qualified end user is not a global subject")
        return value


class CellRatelimitNamespaceSetInput(CellActionInput):
    namespace: Name
    op_class: ShortName
    capacity: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    rate: Nonnegative | None = None
    clear: bool = False

    @field_validator("namespace")
    @classmethod
    def _namespace_label(cls, value: str) -> str:
        if value == "*":
            raise ValueError("A namespace-name rate override requires an exact label")
        return value

    @model_validator(mode="after")
    def _values_or_clear(self) -> CellRatelimitNamespaceSetInput:
        if self.clear:
            if self.capacity is not None or self.rate is not None:
                raise ValueError("A cleared rate override cannot carry values")
        elif self.capacity is None or self.rate is None:
            raise ValueError("A rate override requires capacity and rate")
        return self


class CellBudgetSetInput(CellActionInput):
    op: Annotated[str, AfterValidator(lambda value: _text(value, 128))]
    consumer: ShortName | None = None
    budget_ms: int | None = Field(default=None, ge=0, le=86400000)
    budget: str | None = Field(default=None, max_length=32)
    clear: bool = False

    @field_validator("budget")
    @classmethod
    def _multiplier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # The command sends a bounded canonical decimal, never an exponent or float.
        if not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?x", value):
            raise ValueError("Expected a canonical budget multiplier")
        if not 0 <= Decimal(value[:-1]) <= 1000:
            raise ValueError("Budget multiplier is outside its permitted range")
        return value

    @model_validator(mode="after")
    def _value_or_clear(self) -> CellBudgetSetInput:
        if self.op == "*" and self.consumer is None:
            raise ValueError("An all-operation budget requires a consumer")
        if self.clear:
            if self.budget_ms is not None or self.budget is not None:
                raise ValueError("A cleared budget cannot carry a value")
        elif (self.budget_ms is None) == (self.budget is None):
            raise ValueError("A budget requires exactly one value")
        if self.budget is not None and self.consumer is None:
            raise ValueError("A budget multiplier requires a consumer")
        return self


class CellBlacklistGlobalAddInput(CellActionInput):
    pattern_id: PatternID
    pattern: Annotated[str, AfterValidator(lambda value: _text(value, 4096, controls=True))] = (
        Field(repr=False)
    )
    pattern_type: Literal["substring", "regex"] = "substring"
    reason: Reason = Field(default="", repr=False)

    @model_validator(mode="after")
    def _new_pattern(self) -> CellBlacklistGlobalAddInput:
        if UUID(self.pattern_id).version != 4:
            raise ValueError("A new pattern identity must be a UUIDv4")
        if self.pattern_type == "regex" and len(self.pattern) > 256:
            raise ValueError("Regex pattern exceeds its character limit")
        return self


class CellBlacklistGlobalListInput(CellActionInput):
    limit: int = Field(default=50, ge=1, le=50)
