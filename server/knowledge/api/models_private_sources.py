"""Private source input errors omit credentials, selected IDs and queries."""

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ModelWrapValidatorHandler, model_validator
from pydantic import ValidationError as ModelValidationError


class PrivateSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    org: str = Field(description="The authenticated caller's organization.")

    @model_validator(mode="wrap")
    @classmethod
    def _redact_errors(cls, value: Any, handler: ModelWrapValidatorHandler[Self]) -> Self:
        try:
            return handler(value)
        except ModelValidationError:
            raise ModelValidationError.from_exception_data(
                cls.__name__,
                [
                    {
                        "type": "value_error",
                        "loc": (),
                        "input": "<redacted>",
                        "ctx": {"error": ValueError("PRIVATE_SOURCE_INPUT_INVALID")},
                    }
                ],
            ) from None
