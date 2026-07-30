from unilabos.resources.bioyond.bottle_carriers import (
    BIOYOND_Electrolyte_1BottleCarrier,
    BIOYOND_Electrolyte_6VialCarrier,
)
from unilabos.resources.itemized_carrier import Bottle


def test_bottle_carrier_factories_populate_direct_resource_sites():
    bottle_carrier = BIOYOND_Electrolyte_6VialCarrier("powder_carrier_01")
    beaker_carrier = BIOYOND_Electrolyte_1BottleCarrier("solution_carrier_01")

    assert len(bottle_carrier.sites) == 6
    assert len(beaker_carrier.sites) == 1

    bottle_at_0 = bottle_carrier[0]
    beaker_at_0 = beaker_carrier[0]

    assert isinstance(bottle_at_0, Bottle)
    assert isinstance(beaker_at_0, Bottle)
    assert bottle_at_0 is bottle_carrier.sites[0]
    assert beaker_at_0 is beaker_carrier.sites[0]
    assert bottle_at_0.parent is bottle_carrier
    assert beaker_at_0.parent is beaker_carrier
