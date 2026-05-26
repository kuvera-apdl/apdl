from app.utils import serialize_flag


def test_serialize_flag_always_includes_rules_and_variants():
    flag = {
        "key": "checkout",
        "enabled": True,
        "variant_type": "boolean",
        "default_value": "false",
        "rollout_percentage": 25.0,
        "rules_json": "[]",
        "variants_json": "[]",
    }

    assert serialize_flag(flag) == {
        "key": "checkout",
        "enabled": True,
        "variant_type": "boolean",
        "default_value": "false",
        "rollout_percentage": 25.0,
        "rules": [],
        "variants": [],
        "description": "",
        "updated_at": "",
    }


def test_serialize_flag_parses_rules_and_variants():
    flag = {
        "key": "checkout_variant",
        "enabled": True,
        "variant_type": "string",
        "default_value": "control",
        "rollout_percentage": 100.0,
        "rules_json": '[{"attribute":"plan","operator":"equals","value":"pro"}]',
        "variants_json": (
            '[{"key":"control","value":"control","weight":50},'
            '{"key":"treatment","value":"treatment","weight":50}]'
        ),
    }

    serialized = serialize_flag(flag, include_description=False)

    assert serialized["rules"] == [
        {"attribute": "plan", "operator": "equals", "value": "pro"}
    ]
    assert serialized["variants"] == [
        {"key": "control", "value": "control", "weight": 50},
        {"key": "treatment", "value": "treatment", "weight": 50},
    ]
    assert "description" not in serialized
