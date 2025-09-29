#!/usr/bin/env python3
"""Convert an OpenAPI 3.0 specification to Swagger 2.0."""
from __future__ import annotations

import copy
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import urlparse

import yaml
from yaml.representer import SafeRepresenter

yaml.SafeDumper.add_representer(OrderedDict, SafeRepresenter.represent_dict)

PARAMETER_PRIMITIVE_FIELDS = {
    "type",
    "format",
    "default",
    "maximum",
    "exclusiveMaximum",
    "minimum",
    "exclusiveMinimum",
    "maxLength",
    "minLength",
    "pattern",
    "maxItems",
    "minItems",
    "uniqueItems",
    "multipleOf",
    "enum",
    "collectionFormat",
    "x-nullable",
    "x-deprecated",
    "x-example",
    "x-examples",
}


def load_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text())


def dump_yaml(data: Dict[str, Any], path: Path) -> None:
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
    )


def convert_ref(ref: str) -> str:
    if ref.startswith("#/components/schemas/"):
        return ref.replace("#/components/schemas/", "#/definitions/")
    if ref.startswith("#/components/parameters/"):
        return ref.replace("#/components/parameters/", "#/parameters/")
    if ref.startswith("#/components/responses/"):
        return ref.replace("#/components/responses/", "#/responses/")
    if ref.startswith("#/components/requestBodies/"):
        return ref.replace("#/components/requestBodies/", "#/x-requestBodies/")
    if ref.startswith("#/components/securitySchemes/"):
        return ref.replace("#/components/securitySchemes/", "#/securityDefinitions/")
    return ref


def convert_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [convert_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    converted: Dict[str, Any] = OrderedDict()
    for key, value in schema.items():
        if key == "$ref":
            converted[key] = convert_ref(value)
        elif key == "nullable":
            if value:
                converted["x-nullable"] = True
        elif key == "deprecated":
            converted["x-deprecated"] = value
        elif key == "discriminator":
            converted["x-discriminator"] = value
        elif key == "example":
            converted["example"] = value
        elif key == "examples":
            converted["x-examples"] = value
        elif key in {"allOf", "anyOf", "oneOf", "not"}:
            if key == "not":
                converted["x-not"] = convert_schema(value)
            else:
                converted[f"x-{key}"] = convert_schema(value)
        elif key in {"items", "additionalProperties"}:
            converted[key] = convert_schema(value)
        elif key == "properties":
            props = OrderedDict()
            for prop_name, prop_schema in value.items():
                props[prop_name] = convert_schema(prop_schema)
            converted[key] = props
        elif key == "patternProperties":
            patterns = OrderedDict()
            for pattern, pattern_schema in value.items():
                patterns[pattern] = convert_schema(pattern_schema)
            converted[key] = patterns
        elif key == "xml":
            converted[key] = value
        else:
            converted[key] = convert_schema(value)
    return converted


def schema_to_parameter_fields(schema: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = OrderedDict()
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        result["items"] = schema_to_parameter_fields(schema["items"])
    for field in PARAMETER_PRIMITIVE_FIELDS:
        if field in schema:
            result[field] = schema[field]
    # Preserve vendor extensions
    for key, value in schema.items():
        if key.startswith("x-") and key not in result:
            result[key] = value
    return result


def convert_parameter(parameter: Dict[str, Any]) -> Dict[str, Any]:
    new_param: Dict[str, Any] = OrderedDict()
    for key in ("name", "in", "description", "required", "deprecated", "allowEmptyValue"):
        if key in parameter:
            if key == "deprecated":
                new_param["x-deprecated"] = parameter[key]
            else:
                new_param[key] = parameter[key]

    if parameter.get("in") == "body":
        schema = convert_schema(parameter.get("schema", {}))
        if schema:
            new_param["schema"] = schema
        for key in parameter:
            if key.startswith("x-"):
                new_param[key] = parameter[key]
        return new_param

    schema = parameter.get("schema")
    if schema:
        converted_schema = convert_schema(schema)
        fields = schema_to_parameter_fields(converted_schema)
        for key, value in fields.items():
            new_param[key] = value
        if "example" in converted_schema and "x-example" not in new_param:
            new_param["x-example"] = converted_schema["example"]
    if parameter.get("style") == "form" and parameter.get("explode"):
        new_param["collectionFormat"] = "multi"
    elif parameter.get("style") == "form":
        new_param["collectionFormat"] = "csv"
    if parameter.get("style") == "spaceDelimited":
        new_param["collectionFormat"] = "ssv"
    if parameter.get("style") == "pipeDelimited":
        new_param["collectionFormat"] = "pipes"

    for key in parameter:
        if key.startswith("x-"):
            new_param[key] = parameter[key]

    return new_param


def convert_request_body(request_body: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    consumes: List[str] = []
    body_param: Dict[str, Any] = OrderedDict()

    content = request_body.get("content", {})
    if not content:
        return body_param, consumes

    media_type, media_obj = next(iter(content.items()))
    consumes.append(media_type)

    body_param["name"] = "body"
    body_param["in"] = "body"
    if request_body.get("description"):
        body_param["description"] = request_body["description"]
    body_param["required"] = request_body.get("required", False)

    schema = media_obj.get("schema")
    if schema:
        body_param["schema"] = convert_schema(schema)
    example = media_obj.get("example")
    if example is not None:
        body_param["x-example"] = example
    examples = media_obj.get("examples")
    if isinstance(examples, dict):
        extracted = OrderedDict()
        for name, payload in examples.items():
            if isinstance(payload, dict) and "value" in payload:
                extracted[name] = payload["value"]
            else:
                extracted[name] = payload
        if extracted:
            body_param["x-examples"] = extracted

    for key in request_body:
        if key.startswith("x-"):
            body_param[key] = request_body[key]

    return body_param, consumes


def convert_header(header: Dict[str, Any]) -> Dict[str, Any]:
    new_header: Dict[str, Any] = OrderedDict()
    if "description" in header:
        new_header["description"] = header["description"]
    schema = header.get("schema")
    if schema:
        converted_schema = convert_schema(schema)
        fields = schema_to_parameter_fields(converted_schema)
        for key, value in fields.items():
            new_header[key] = value
    for key in header:
        if key.startswith("x-"):
            new_header[key] = header[key]
    return new_header


def convert_responses(responses: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    converted_responses: Dict[str, Any] = OrderedDict()
    produces: List[str] = []
    for status, response in responses.items():
        new_response: Dict[str, Any] = OrderedDict()
        new_response["description"] = response.get("description", "")

        headers = response.get("headers")
        if headers:
            header_map = OrderedDict()
            for name, header in headers.items():
                header_map[name] = convert_header(header)
            if header_map:
                new_response["headers"] = header_map

        content = response.get("content", {})
        if content:
            media_type, media_obj = next(iter(content.items()))
            produces.append(media_type)
            schema = media_obj.get("schema")
            if media_type == "application/pdf":
                new_response["schema"] = OrderedDict([("type", "file")])
            elif schema:
                new_response["schema"] = convert_schema(schema)
            example = media_obj.get("example")
            if example is not None:
                new_response.setdefault("examples", OrderedDict())[media_type] = example
            examples = media_obj.get("examples")
            if isinstance(examples, dict):
                dest = new_response.setdefault("examples", OrderedDict())
                for _, payload in examples.items():
                    if isinstance(payload, dict) and "value" in payload:
                        dest[media_type] = payload["value"]
                    else:
                        dest[media_type] = payload
        for key in response:
            if key.startswith("x-"):
                new_response[key] = response[key]
        converted_responses[status] = new_response
    return converted_responses, produces


def convert_operation(operation: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], List[str]]:
    converted_operation: Dict[str, Any] = OrderedDict()
    for key in (
        "tags",
        "summary",
        "description",
        "operationId",
        "deprecated",
        "security",
        "externalDocs",
    ):
        if key in operation:
            converted_operation[key] = copy.deepcopy(operation[key])
    if "deprecated" in operation:
        converted_operation["deprecated"] = operation["deprecated"]

    parameters = [convert_parameter(p) for p in operation.get("parameters", [])]

    consumes: List[str] = []
    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        body_param, body_consumes = convert_request_body(request_body)
        if body_param:
            parameters.append(body_param)
        consumes.extend(body_consumes)

    if parameters:
        converted_operation["parameters"] = parameters

    responses, produces = convert_responses(operation.get("responses", {}))
    converted_operation["responses"] = responses

    if consumes:
        converted_operation["consumes"] = sorted(dict.fromkeys(consumes))
    if produces:
        converted_operation["produces"] = sorted(dict.fromkeys(produces))

    for key in operation:
        if key.startswith("x-"):
            converted_operation[key] = operation[key]
        if key == "callbacks":
            converted_operation["x-callbacks"] = operation[key]
        if key == "servers":
            converted_operation["x-servers"] = operation[key]

    return converted_operation, consumes, produces


def convert_path_item(path_item: Dict[str, Any]) -> Dict[str, Any]:
    converted_path: Dict[str, Any] = OrderedDict()
    if "parameters" in path_item:
        converted_path["parameters"] = [
            convert_parameter(parameter) for parameter in path_item["parameters"]
        ]
    for method in ("get", "put", "post", "delete", "options", "head", "patch"):
        if method in path_item:
            operation = path_item[method]
            converted_operation, _, _ = convert_operation(operation)
            converted_path[method] = converted_operation
    for key in path_item:
        if key.startswith("x-"):
            converted_path[key] = path_item[key]
        if key == "servers":
            converted_path["x-servers"] = path_item[key]
    return converted_path


def parse_server(server: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    url = server.get("url", "/")
    parsed = urlparse(url)
    schemes: List[str] = []
    host = ""
    base_path = ""
    if parsed.scheme:
        schemes = [parsed.scheme]
    if parsed.netloc:
        host = parsed.netloc
    if parsed.path:
        base_path = parsed.path
    return host, base_path, schemes


def convert_components(components: Dict[str, Any]) -> Dict[str, Any]:
    converted: Dict[str, Any] = {}
    schemas = components.get("schemas")
    if schemas:
        converted["definitions"] = OrderedDict(
            (name, convert_schema(schema)) for name, schema in schemas.items()
        )
    parameters = components.get("parameters")
    if parameters:
        converted["parameters"] = OrderedDict(
            (name, convert_parameter(parameter))
            for name, parameter in parameters.items()
        )
    responses = components.get("responses")
    if responses:
        converted_responses = OrderedDict()
        for name, response in responses.items():
            converted_response, _ = convert_responses({"default": response})
            converted_responses[name] = converted_response.get("default", {})
        converted["responses"] = converted_responses
    security = components.get("securitySchemes")
    if security:
        sec_defs = OrderedDict()
        for name, scheme in security.items():
            scheme_copy = copy.deepcopy(scheme)
            if scheme_copy.get("type") == "http":
                if scheme_copy.get("scheme") == "basic":
                    scheme_copy["type"] = "basic"
                    scheme_copy.pop("scheme", None)
                elif scheme_copy.get("scheme") == "bearer":
                    scheme_copy["type"] = "apiKey"
                    scheme_copy["name"] = scheme_copy.get("name", "Authorization")
                    scheme_copy["in"] = scheme_copy.get("in", "header")
                    scheme_copy["x-original-http-scheme"] = "bearer"
                    scheme_copy.pop("scheme", None)
            scheme_copy.setdefault("x-ms-visibility", "important")
            sec_defs[name] = scheme_copy
        converted["securityDefinitions"] = sec_defs
    return converted


def convert_openapi_to_swagger(openapi_spec: Dict[str, Any]) -> Dict[str, Any]:
    swagger: Dict[str, Any] = OrderedDict()
    swagger["swagger"] = "2.0"
    swagger["info"] = copy.deepcopy(openapi_spec.get("info", {}))
    swagger["consumes"] = ["application/json"]
    swagger["produces"] = ["application/json"]
    swagger["x-ms-connector-metadata"] = {
        "categories": ["eSignature"],
        "visibility": "important",
    }

    servers = openapi_spec.get("servers") or []
    if servers:
        host, base_path, schemes = parse_server(servers[0])
        if host:
            swagger["host"] = host
        if base_path and base_path != "/":
            swagger["basePath"] = base_path
        if schemes:
            swagger["schemes"] = schemes

    if openapi_spec.get("tags"):
        swagger["tags"] = copy.deepcopy(openapi_spec["tags"])
    if openapi_spec.get("externalDocs"):
        swagger["externalDocs"] = copy.deepcopy(openapi_spec["externalDocs"])
    if openapi_spec.get("security"):
        swagger["security"] = copy.deepcopy(openapi_spec["security"])

    components = openapi_spec.get("components") or {}
    converted_components = convert_components(components)
    swagger.update(converted_components)

    paths = openapi_spec.get("paths", {})
    swagger["paths"] = OrderedDict(
        (path, convert_path_item(path_item)) for path, path_item in paths.items()
    )

    for key in openapi_spec:
        if key.startswith("x-"):
            swagger[key] = copy.deepcopy(openapi_spec[key])

    return swagger


def main() -> None:
    source = Path("legalesign-api-v1.yaml")
    target = Path("legalesign-api-v1-swagger.yaml")
    openapi_spec = load_yaml(source)
    swagger_spec = convert_openapi_to_swagger(openapi_spec)
    dump_yaml(swagger_spec, target)
    print(f"Converted {source.name} -> {target.name}")


if __name__ == "__main__":
    main()
