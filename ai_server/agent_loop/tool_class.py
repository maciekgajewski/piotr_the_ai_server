from __future__ import annotations

import inspect
import json
import math
from dataclasses import dataclass
from enum import Enum
from types import NoneType, UnionType
from typing import Annotated, Any, Callable, Union, get_args, get_origin, get_type_hints


@dataclass(frozen=True)
class ToolParameter:
    name: str
    annotation: object
    schema: dict[str, Any]
    required: bool
    default: object = inspect.Parameter.empty


@dataclass(frozen=True)
class ToolMethod:
    method_name: str
    tool_name: str
    description: str
    parameters: tuple[ToolParameter, ...]


@dataclass(frozen=True)
class ToolMetadata:
    name: str | None
    description: str | None


class ToolClass:
    __agent_loop_tools__: dict[str, ToolMethod] = {}

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        registry: dict[str, ToolMethod] = {}
        for base in reversed(cls.__mro__[1:]):
            registry.update(getattr(base, "__agent_loop_tools__", {}))

        for method_name, raw_member in cls.__dict__.items():
            member = _unwrap_method(raw_member)
            metadata = getattr(member, "__agent_loop_tool_metadata__", None)
            if metadata is None:
                continue

            tool_method = _build_tool_method(cls, method_name, member, metadata)
            for inherited_tool_name, inherited_tool in tuple(registry.items()):
                if inherited_tool.method_name == method_name:
                    del registry[inherited_tool_name]
            if tool_method.tool_name in registry and registry[tool_method.tool_name].method_name != method_name:
                raise ValueError(f"duplicate tool name: {tool_method.tool_name}")
            registry[tool_method.tool_name] = tool_method

        cls.__agent_loop_tools__ = registry

    @classmethod
    def tool(
        cls,
        method: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Callable[..., Any]:
        if method is None:
            return lambda decorated: cls.tool(decorated, name=name, description=description)

        if not inspect.iscoroutinefunction(method):
            raise TypeError("@ToolClass.tool can only decorate async methods")
        if name is not None and not name:
            raise ValueError("tool name must be a non-empty string")

        setattr(method, "__agent_loop_tool_metadata__", ToolMetadata(name=name, description=description))
        return method

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [_tool_schema(tool_method) for tool_method in self.__agent_loop_tools__.values()]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        tool_method = self.__agent_loop_tools__.get(name)
        if tool_method is None:
            raise ValueError(f"unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise ValueError(f"tool arguments for {name} must be an object")

        validated_arguments = _validate_arguments(tool_method, arguments)
        result = await getattr(self, tool_method.method_name)(**validated_arguments)
        return to_json_value(result)


def _unwrap_method(raw_member: object) -> object:
    if isinstance(raw_member, (classmethod, staticmethod)):
        return raw_member.__func__
    return raw_member


def _build_tool_method(cls: type, method_name: str, method: object, metadata: ToolMetadata) -> ToolMethod:
    if isinstance(cls.__dict__[method_name], (classmethod, staticmethod)):
        raise TypeError("@ToolClass.tool supports regular instance methods only")
    if not callable(method):
        raise TypeError("@ToolClass.tool can only decorate methods")

    signature = inspect.signature(method)
    parameters = list(signature.parameters.values())
    if not parameters or parameters[0].name != "self":
        raise TypeError(f"tool method {method_name} must be an instance method with self")

    type_hints = get_type_hints(method, include_extras=True)
    tool_parameters = []
    for parameter in parameters[1:]:
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise TypeError(f"tool method {method_name} cannot use *args or **kwargs")
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            raise TypeError(f"tool method {method_name} cannot use positional-only parameters")
        if parameter.name not in type_hints:
            raise TypeError(f"tool parameter {method_name}.{parameter.name} must have a type annotation")

        annotation, description = _unwrap_annotated(type_hints[parameter.name])
        schema = _schema_for_type(annotation)
        if description:
            schema["description"] = description
        if parameter.default is not inspect.Parameter.empty:
            schema["default"] = to_json_value(parameter.default)
            _validate_value(annotation, parameter.default, f"{method_name}.{parameter.name}")

        tool_parameters.append(
            ToolParameter(
                name=parameter.name,
                annotation=annotation,
                schema=schema,
                required=parameter.default is inspect.Parameter.empty,
                default=parameter.default,
            )
        )

    return ToolMethod(
        method_name=method_name,
        tool_name=metadata.name or method_name,
        description=metadata.description or inspect.getdoc(method) or "",
        parameters=tuple(tool_parameters),
    )


def _tool_schema(tool_method: ToolMethod) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool_method.tool_name,
            "description": tool_method.description,
            "parameters": {
                "type": "object",
                "properties": {parameter.name: parameter.schema for parameter in tool_method.parameters},
                "required": [parameter.name for parameter in tool_method.parameters if parameter.required],
                "additionalProperties": False,
            },
        },
    }


def _validate_arguments(tool_method: ToolMethod, arguments: dict[str, Any]) -> dict[str, Any]:
    parameters_by_name = {parameter.name: parameter for parameter in tool_method.parameters}
    extra_names = set(arguments) - set(parameters_by_name)
    if extra_names:
        raise ValueError(f"unexpected tool arguments for {tool_method.tool_name}: {', '.join(sorted(extra_names))}")

    validated_arguments = {}
    for parameter in tool_method.parameters:
        if parameter.name not in arguments:
            if parameter.required:
                raise ValueError(f"missing required tool argument for {tool_method.tool_name}: {parameter.name}")
            validated_arguments[parameter.name] = parameter.default
            continue

        validated_arguments[parameter.name] = _validate_value(
            parameter.annotation,
            arguments[parameter.name],
            f"{tool_method.tool_name}.{parameter.name}",
        )

    return validated_arguments


def _unwrap_annotated(annotation: object) -> tuple[object, str | None]:
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        description = next((item for item in args[1:] if isinstance(item, str)), None)
        return args[0], description
    return annotation, None


def _schema_for_type(annotation: object) -> dict[str, Any]:
    if annotation is Any:
        return {}
    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is None or annotation is NoneType:
        return {"type": "null"}
    if inspect.isclass(annotation) and issubclass(annotation, Enum):
        return _schema_for_enum(annotation)

    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is dict:
        if len(args) != 2 or args[0] is not str:
            raise TypeError("dict tool parameters must be annotated as dict[str, T]")
        value_schema = _schema_for_type(_unwrap_annotated(args[1])[0])
        schema: dict[str, Any] = {"type": "object"}
        if value_schema:
            schema["additionalProperties"] = value_schema
        return schema
    if origin is list:
        if len(args) != 1:
            raise TypeError("list tool parameters must be annotated as list[T]")
        return {"type": "array", "items": _schema_for_type(_unwrap_annotated(args[0])[0])}

    if origin in (UnionType, Union):
        union_args = tuple(_unwrap_annotated(arg)[0] for arg in args)
        if len(union_args) == 2 and NoneType in union_args:
            value_type = next(arg for arg in union_args if arg is not NoneType)
            return {"anyOf": [_schema_for_type(value_type), {"type": "null"}]}

    raise TypeError(f"unsupported JSON tool type annotation: {annotation!r}")


def _schema_for_enum(enum_class: type[Enum]) -> dict[str, Any]:
    values = [to_json_value(member.value) for member in enum_class]
    schema: dict[str, Any] = {"enum": values}
    value_types = {type(value) for value in values}
    if value_types == {str}:
        schema["type"] = "string"
    elif value_types == {bool}:
        schema["type"] = "boolean"
    elif value_types == {int}:
        schema["type"] = "integer"
    elif value_types <= {int, float}:
        schema["type"] = "number"
    return schema


def _validate_value(annotation: object, value: Any, path: str) -> Any:
    if annotation is Any:
        return _validate_json_value(value, path)
    if annotation is str:
        if not isinstance(value, str):
            raise ValueError(f"{path} must be a string")
        return value
    if annotation is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{path} must be a boolean")
        return value
    if annotation is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{path} must be an integer")
        return value
    if annotation is float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{path} must be a number")
        return float(value)
    if annotation is None or annotation is NoneType:
        if value is not None:
            raise ValueError(f"{path} must be null")
        return None
    if inspect.isclass(annotation) and issubclass(annotation, Enum):
        return _validate_enum(annotation, value, path)

    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        value_type = _unwrap_annotated(args[1])[0]
        return {
            _validate_dict_key(key, path): _validate_value(value_type, item, f"{path}.{key}")
            for key, item in value.items()
        }
    if origin is list:
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        item_type = _unwrap_annotated(args[0])[0]
        return [_validate_value(item_type, item, f"{path}[{index}]") for index, item in enumerate(value)]

    if origin in (UnionType, Union):
        union_args = tuple(_unwrap_annotated(arg)[0] for arg in args)
        if len(union_args) == 2 and NoneType in union_args:
            if value is None:
                return None
            value_type = next(arg for arg in union_args if arg is not NoneType)
            return _validate_value(value_type, value, path)

    raise TypeError(f"unsupported JSON tool type annotation: {annotation!r}")


def _validate_enum(enum_class: type[Enum], value: Any, path: str) -> Enum:
    for member in enum_class:
        if member.value == value:
            return member
    allowed = ", ".join(repr(member.value) for member in enum_class)
    raise ValueError(f"{path} must be one of: {allowed}")


def _validate_dict_key(key: Any, path: str) -> str:
    if not isinstance(key, str):
        raise ValueError(f"{path} keys must be strings")
    return key


def _validate_json_value(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must be a finite number")
        return value
    if isinstance(value, Enum):
        return _validate_json_value(value.value, path)
    if isinstance(value, list):
        return [_validate_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        return {_validate_dict_key(key, path): _validate_json_value(item, f"{path}.{key}") for key, item in value.items()}
    raise ValueError(f"{path} must be JSON-serializable")


def to_json_value(value: Any) -> Any:
    json_value = _validate_json_value(value, "tool result")
    json.dumps(json_value, ensure_ascii=False, allow_nan=False)
    return json_value
