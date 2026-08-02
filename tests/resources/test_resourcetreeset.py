import pytest

from tests.resources.bioyond_contract_support import run_isolated_contract


@pytest.mark.parametrize("case_name", ["reaction", "preparation"])
def test_resourcetreeset_from_plr(case_name) -> None:
    result = run_isolated_contract("tree", case_name)

    assert result.returncode == 0, result.stdout + result.stderr
