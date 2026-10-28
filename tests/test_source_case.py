import pytest
from fastapi.testclient import TestClient

from robot_debug_lab.source_case import Proposal, sandbox, validate_candidate


def sample_source():
    return "\n".join(f"safe_line_{line};" for line in range(1, 302)) + "\n"


def test_candidate_is_limited_to_reviewed_publisher_region():
    original = sample_source()
    lines = original.splitlines()
    lines[232] = "if (bounds_are_valid) { publish(); }"
    validate_candidate(original, "\n".join(lines) + "\n")
    lines = original.splitlines()
    lines[20] = "changed_outside_scope();"
    with pytest.raises(ValueError, match="reviewed publisher region"):
        validate_candidate(original, "\n".join(lines) + "\n")


def test_candidate_allows_only_reviewed_costmap_layer_include():
    lines = sample_source().splitlines()
    lines[44] = '#include "nav2_costmap_2d/cost_values.hpp"'
    original = "\n".join(lines) + "\n"
    candidate = original.replace(
        lines[44] + "\n",
        lines[44] + '\n#include "nav2_costmap_2d/costmap_layer.hpp"\n',
    )
    validate_candidate(original, candidate)

    misplaced = original.replace(
        "safe_line_100;\n",
        'safe_line_100;\n#include "nav2_costmap_2d/costmap_layer.hpp"\n',
    )
    with pytest.raises(ValueError, match="include anchor"):
        validate_candidate(original, misplaced)


def test_proposal_uses_truthful_component_scope_when_model_omits_limitations():
    proposal = Proposal.model_validate({
        "diagnosis": "measured source defect",
        "evidence_ids": ["E1", "E2"],
        "edits": [{"before": "0123456789", "after": "abcdefghij"}],
    })
    assert "组件级验证" in proposal.limitations
    assert "实体机器人" in proposal.limitations


@pytest.mark.parametrize("payload", [
    "system(\"id\");",
    "std::ofstream output(\"/tmp/result\");",
    "#include <unistd.h>",
])
def test_candidate_rejects_active_code_sinks(payload):
    original = sample_source()
    lines = original.splitlines()
    lines[232] = payload
    with pytest.raises(ValueError, match="forbidden"):
        validate_candidate(original, "\n".join(lines) + "\n")


def test_sandbox_rejects_non_allowlisted_commands(tmp_path):
    with pytest.raises(ValueError, match="allowlisted"):
        sandbox(tmp_path, "curl https://example.invalid", tmp_path / "run.log")


def test_app_disables_docs_and_sets_browser_security_headers():
    from robot_debug_lab.app import app
    # Do not enter TestClient's lifespan: unit tests must not launch ROS nodes.
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/docs")
    client.close()
    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "object-src 'none'" in response.headers["content-security-policy"]
