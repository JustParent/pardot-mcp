import re

from pardot_mcp import registry


def test_object_names_are_kebab_case_plurals():
    for name in registry.OBJECTS:
        assert re.fullmatch(r"[a-z]+(-[a-z]+)*", name), name


def test_every_object_has_id_field():
    for name, spec in registry.OBJECTS.items():
        assert spec.fields, name
        assert "id" in spec.fields, name
        assert len(set(spec.fields)) == len(spec.fields), f"duplicate fields in {name}"


def test_normalize():
    assert registry.normalize("Visitor_Activities") == "visitor-activities"
    assert registry.normalize("  prospects ") == "prospects"


def test_default_fields_lookup():
    assert "email" in registry.default_fields("prospects")
    assert registry.default_fields("list_memberships") == list(registry.OBJECTS["list-memberships"].fields)
    assert registry.default_fields("no-such-object") is None
