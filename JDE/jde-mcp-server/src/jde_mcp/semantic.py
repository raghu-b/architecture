"""Loads and exposes the semantic model (config/objects.yaml).

The model is the contract between JDE's storage encoding and the business
vocabulary the tools speak. Keeping it in YAML rather than in code means a JDE
analyst can extend it — adding a custom field or a localisation column — without
touching Python.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class SemanticError(ValueError):
    """The requested object or field is not in the semantic model."""


@dataclass(frozen=True)
class Field:
    name: str
    column: str
    type: str
    label: str = ""
    decimals: int = 2


@dataclass(frozen=True)
class BusinessObject:
    name: str
    table: str
    description: str
    fields: dict[str, Field]
    default_order_by: str | None = None

    def field(self, name: str) -> Field:
        try:
            return self.fields[name]
        except KeyError:
            raise SemanticError(
                f"'{name}' is not a field of {self.name}. "
                f"Available: {', '.join(sorted(self.fields))}"
            ) from None

    def column_for(self, name: str) -> str:
        return self.field(name).column

    @property
    def all_columns(self) -> list[str]:
        return [f.column for f in self.fields.values()]

    def describe(self) -> dict[str, Any]:
        return {
            "object": self.name,
            "jde_table": self.table,
            "description": self.description.strip(),
            "fields": [
                {"name": f.name, "type": f.type, "jde_column": f.column,
                 "label": f.label}
                for f in self.fields.values()
            ],
        }


@dataclass(frozen=True)
class WriteTarget:
    name: str
    orchestration: str
    description: str
    required_fields: list[str] = field(default_factory=list)


class SemanticModel:
    def __init__(self, objects: dict[str, BusinessObject],
                 writeback: dict[str, WriteTarget]):
        self._objects = objects
        self._writeback = writeback

    # -- loading ------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "SemanticModel":
        path = Path(path)
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        objects: dict[str, BusinessObject] = {}
        for obj_name, spec in (raw.get("objects") or {}).items():
            fields: dict[str, Field] = {}
            for f_name, f_spec in (spec.get("fields") or {}).items():
                fields[f_name] = Field(
                    name=f_name,
                    column=f_spec["column"],
                    type=f_spec.get("type", "string"),
                    label=f_spec.get("label", ""),
                    decimals=int(f_spec.get("decimals", 2)),
                )
            objects[obj_name] = BusinessObject(
                name=obj_name,
                table=spec["table"],
                description=spec.get("description", ""),
                fields=fields,
                default_order_by=spec.get("default_order_by"),
            )

        writeback: dict[str, WriteTarget] = {}
        for w_name, w_spec in (raw.get("writeback") or {}).items():
            writeback[w_name] = WriteTarget(
                name=w_name,
                orchestration=w_spec["orchestration"],
                description=w_spec.get("description", ""),
                required_fields=list(w_spec.get("required_fields") or []),
            )

        if not objects:
            raise SemanticError(f"no objects defined in {path}")
        return cls(objects, writeback)

    # -- access -------------------------------------------------------------

    def object(self, name: str) -> BusinessObject:
        try:
            return self._objects[name]
        except KeyError:
            raise SemanticError(
                f"unknown business object '{name}'. "
                f"Available: {', '.join(sorted(self._objects))}"
            ) from None

    def write_target(self, name: str) -> WriteTarget:
        try:
            return self._writeback[name]
        except KeyError:
            raise SemanticError(
                f"no write-back route configured for '{name}'. "
                f"Configured: {', '.join(sorted(self._writeback)) or 'none'}"
            ) from None

    @property
    def object_names(self) -> list[str]:
        return sorted(self._objects)

    def catalog(self) -> list[dict[str, Any]]:
        """Compact listing used by the discovery tool."""
        return [
            {"object": o.name, "jde_table": o.table,
             "description": " ".join(o.description.split())[:240],
             "field_count": len(o.fields)}
            for o in self._objects.values()
        ]
