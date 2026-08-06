from importlib.metadata import metadata, version

import apdl
from apdl.types import SDK_IDENTIFIER, SDK_VERSION


def test_runtime_and_distribution_versions_match():
    assert version("apdl-sdk") == apdl.__version__
    assert SDK_VERSION == apdl.__version__
    assert SDK_IDENTIFIER == f"python/{SDK_VERSION}"


def test_distribution_metadata_is_release_ready():
    package_metadata = metadata("apdl-sdk")

    assert package_metadata["Name"] == "apdl-sdk"
    assert package_metadata["License-File"] == "LICENSE"
    assert package_metadata.get_all("Project-URL") == [
        "Homepage, https://github.com/kuvera-apdl/apdl/tree/main/sdk/python#readme",
        "Repository, https://github.com/kuvera-apdl/apdl",
        "Issues, https://github.com/kuvera-apdl/apdl/issues",
    ]
