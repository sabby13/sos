"""Tool schema generation.

These are the schemas sent to the model on every single request, so a regression here
degrades every interaction rather than failing loudly.
"""

from __future__ import annotations

import json

from openjarvis.tools.base import _clean_schema
from openjarvis.tools.registry import default_registry


def test_every_tool_produces_a_valid_function_schema():
    for tool in default_registry():
        schema = tool.to_schema()
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"] == tool.name
        assert function["description"].strip(), f"{tool.name} has an empty description"
        assert function["parameters"]["type"] == "object"
        # Must survive a JSON round trip - it goes over the wire.
        json.loads(json.dumps(schema))


def walk_schema_objects(node, in_properties: bool = False):
    """Yield every dict that is a *schema*, skipping parameter-name keys.

    Needed because a parameter may legitimately be called "title" - the check below is
    about the `title` schema keyword, not about a key that happens to share its name.
    """
    if isinstance(node, list):
        for item in node:
            yield from walk_schema_objects(item)
        return
    if not isinstance(node, dict):
        return
    if not in_properties:
        yield node
    for key, value in node.items():
        yield from walk_schema_objects(value, in_properties=(key == "properties"))


def test_schemas_have_no_refs_or_titles():
    """Small models follow flat, description-only schemas far more reliably."""
    for tool in default_registry():
        rendered = json.dumps(tool.to_schema())
        assert "$ref" not in rendered, f"{tool.name} leaks a $ref"
        assert "$defs" not in rendered, f"{tool.name} leaks a $defs"
        for obj in walk_schema_objects(tool.to_schema()):
            assert "title" not in obj, f"{tool.name} leaks a title keyword: {obj}"


def test_a_parameter_named_title_survives_cleaning():
    """Regression: `title` was being stripped as schema noise, deleting the parameter."""
    from openjarvis.tools.windows_ctl import ControlWindow

    params = ControlWindow.to_schema()["function"]["parameters"]
    assert "title" in params["properties"]
    assert params["properties"]["title"]["type"] == "string"
    # Anything listed in `required` must actually exist, or the request is malformed.
    for name in params.get("required", []):
        assert name in params["properties"]


def test_required_fields_always_exist_in_properties():
    for tool in default_registry():
        params = tool.to_schema()["function"]["parameters"]
        for name in params.get("required", []):
            assert name in params["properties"], f"{tool.name} requires missing {name!r}"


def test_every_parameter_is_documented():
    """An undescribed parameter is a parameter the model will fill in wrong."""
    for tool in default_registry():
        properties = tool.to_schema()["function"]["parameters"]["properties"]
        for name, spec in properties.items():
            assert spec.get("description"), f"{tool.name}.{name} has no description"


def test_tool_names_are_unique_and_snake_case():
    names = [tool.name for tool in default_registry()]
    assert len(names) == len(set(names))
    for name in names:
        assert name.islower() and " " not in name


def test_clean_schema_inlines_refs():
    raw = {
        "type": "object",
        "title": "Args",
        "properties": {"mode": {"$ref": "#/$defs/Mode"}},
        "$defs": {"Mode": {"title": "Mode", "enum": ["a", "b"], "type": "string"}},
    }
    cleaned = _clean_schema(raw)
    assert cleaned["properties"]["mode"] == {"enum": ["a", "b"], "type": "string"}
    assert "$defs" not in cleaned
    assert "title" not in cleaned


def test_clean_schema_flattens_optional_unions():
    """pydantic spells Optional[str] as anyOf[str, null]; models handle a plain type better."""
    raw = {
        "type": "object",
        "properties": {
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "A note."}
        },
    }
    cleaned = _clean_schema(raw)
    assert cleaned["properties"]["note"]["type"] == "string"
    assert "anyOf" not in cleaned["properties"]["note"]


def test_enum_parameters_survive():
    """control_window's action enum is what makes it one tool instead of five."""
    from openjarvis.tools.windows_ctl import ControlWindow

    action = ControlWindow.to_schema()["function"]["parameters"]["properties"]["action"]
    assert set(action["enum"]) == {"focus", "minimize", "maximize", "restore", "close"}
