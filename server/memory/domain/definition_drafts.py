"""The bounded JSON value a model may propose for a definition."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Name = Annotated[str, Field(min_length=1, max_length=200)]


class DefinitionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term: Name
    aliases: list[Name] = Field(default_factory=list, max_length=20)
    body: str = Field(min_length=1, max_length=4000)
    tables: list[Name] = Field(default_factory=list, max_length=100)
    columns: list[Name] = Field(default_factory=list, max_length=200)
