import itertools
from typing import List, Tuple

import numpy as np
import openmm
import pytest
from openff.toolkit.tests.test_forcefield import create_ethanol, create_reversed_ethanol
from openff.toolkit.tests.utils import get_data_file_path, requires_openeye
from openff.toolkit.topology import Molecule, Topology
from openff.toolkit.typing.engines.smirnoff.forcefield import ForceField
from openff.toolkit.typing.engines.smirnoff.parameters import (
    AngleHandler,
    BondHandler,
    ChargeIncrementModelHandler,
    ElectrostaticsHandler,
    ImproperTorsionHandler,
    LibraryChargeHandler,
    ParameterHandler,
    ToolkitAM1BCCHandler,
    UnassignedProperTorsionParameterException,
    UnassignedValenceParameterException,
    VirtualSiteHandler,
    vdWHandler,
)
from openff.units import unit
from openff.units.openmm import to_openmm
from openff.utilities.testing import skip_if_missing
from openmm import unit as openmm_unit
from pydantic import ValidationError

from openff.interchange import Interchange
from openff.interchange.components.smirnoff import (
    SMIRNOFFAngleHandler,
    SMIRNOFFBondHandler,
    SMIRNOFFConstraintHandler,
    SMIRNOFFElectrostaticsHandler,
    SMIRNOFFImproperTorsionHandler,
    SMIRNOFFPotentialHandler,
    SMIRNOFFvdWHandler,
    library_charge_from_molecule,
)
from openff.interchange.exceptions import InvalidParameterHandlerError
from openff.interchange.models import TopologyKey
from openff.interchange.tests import _BaseTest, get_test_file_path

kcal_mol = unit.Unit("kilocalorie / mol")
kcal_mol_a2 = unit.Unit("kilocalorie / (angstrom ** 2 * mole)")
kcal_mol_rad2 = unit.Unit("kilocalorie / (mole * radian ** 2)")


def hydrogen_cyanide(reversed: bool = False) -> Molecule:
    return Molecule.from_mapped_smiles(
        "[H:3][C:2]#[N:1]" if reversed else "[H:1][C:2]#[N:3]"
    )


def hydrogen_cyanide_charge_increments() -> ChargeIncrementModelHandler:
    handler = ChargeIncrementModelHandler(
        version=0.4,
        partial_charge_method="formal_charge",
    )
    handler.add_parameter(
        {
            "smirks": "[H:1][C:2]",
            "charge_increment1": -0.111 * unit.elementary_charge,
            "charge_increment2": 0.111 * unit.elementary_charge,
        }
    )
    handler.add_parameter(
        {
            "smirks": "[C:1]#[N:2]",
            "charge_increment1": 0.5 * unit.elementary_charge,
            "charge_increment2": -0.5 * unit.elementary_charge,
        }
    )

    return handler


class TestSMIRNOFFPotentialHandler(_BaseTest):
    def test_allowed_parameter_handler_types(self):
        class DummyParameterHandler(ParameterHandler):
            pass

        class DummySMIRNOFFHandler(SMIRNOFFPotentialHandler):
            type = "Bonds"
            expression = "1+1"

            @classmethod
            def allowed_parameter_handlers(cls):
                return [DummyParameterHandler]

            @classmethod
            def supported_parameters(cls):
                return list()

        dummy_handler = DummySMIRNOFFHandler()
        angle_Handler = AngleHandler(version=0.3)

        assert DummyParameterHandler in dummy_handler.allowed_parameter_handlers()
        assert AngleHandler not in dummy_handler.allowed_parameter_handlers()
        assert (
            DummyParameterHandler
            not in SMIRNOFFAngleHandler.allowed_parameter_handlers()
        )

        dummy_handler = DummyParameterHandler(version=0.3)

        with pytest.raises(InvalidParameterHandlerError):
            SMIRNOFFAngleHandler._from_toolkit(
                parameter_handler=dummy_handler,
                topology=Topology(),
            )

        with pytest.raises(InvalidParameterHandlerError):
            DummySMIRNOFFHandler._from_toolkit(
                parameter_handler=angle_Handler,
                topology=Topology(),
            )


class TestSMIRNOFFHandlers(_BaseTest):
    def test_bond_potential_handler(self):
        bond_handler = BondHandler(version=0.3)
        bond_handler.fractional_bondorder_method = "AM1-Wiberg"
        bond_parameter = BondHandler.BondType(
            smirks="[*:1]~[*:2]",
            k=1.5 * unit.kilocalorie_per_mole / unit.angstrom**2,
            length=1.5 * unit.angstrom,
            id="b1000",
        )
        bond_handler.add_parameter(bond_parameter.to_dict())

        forcefield = ForceField()
        forcefield.register_parameter_handler(bond_handler)
        bond_potentials = SMIRNOFFBondHandler._from_toolkit(
            parameter_handler=forcefield["Bonds"],
            topology=Molecule.from_smiles("O").to_topology(),
        )

        top_key = TopologyKey(atom_indices=(0, 1))
        pot_key = bond_potentials.slot_map[top_key]
        assert pot_key.associated_handler == "Bonds"
        pot = bond_potentials.potentials[pot_key]

        assert pot.parameters["k"].to(kcal_mol_a2).magnitude == pytest.approx(1.5)

    def test_angle_potential_handler(self):
        angle_handler = AngleHandler(version=0.3)
        angle_parameter = AngleHandler.AngleType(
            smirks="[*:1]~[*:2]~[*:3]",
            k=2.5 * unit.kilocalorie_per_mole / unit.radian**2,
            angle=100 * unit.degree,
            id="b1000",
        )
        angle_handler.add_parameter(angle_parameter.to_dict())

        forcefield = ForceField()
        forcefield.register_parameter_handler(angle_handler)
        angle_potentials = SMIRNOFFAngleHandler._from_toolkit(
            parameter_handler=forcefield["Angles"],
            topology=Molecule.from_smiles("CCC").to_topology(),
        )

        top_key = TopologyKey(atom_indices=(0, 1, 2))
        pot_key = angle_potentials.slot_map[top_key]
        assert pot_key.associated_handler == "Angles"
        pot = angle_potentials.potentials[pot_key]

        assert pot.parameters["k"].to(kcal_mol_rad2).magnitude == pytest.approx(2.5)

    def test_store_improper_torsion_matches(self):
        formaldehyde: Molecule = Molecule.from_mapped_smiles("[H:3][C:1]([H:4])=[O:2]")

        parameter_handler = ImproperTorsionHandler(version=0.3)
        parameter_handler.add_parameter(
            parameter=ImproperTorsionHandler.ImproperTorsionType(
                smirks="[*:1]~[#6X3:2](~[*:3])~[*:4]",
                periodicity1=2,
                phase1=180.0 * unit.degree,
                k1=1.1 * unit.kilocalorie_per_mole,
            )
        )

        potential_handler = SMIRNOFFImproperTorsionHandler()
        potential_handler.store_matches(parameter_handler, formaldehyde.to_topology())

        assert len(potential_handler.slot_map) == 3

        assert (
            TopologyKey(atom_indices=(0, 1, 2, 3), mult=0) in potential_handler.slot_map
        )
        assert (
            TopologyKey(atom_indices=(0, 2, 3, 1), mult=0) in potential_handler.slot_map
        )
        assert (
            TopologyKey(atom_indices=(0, 3, 1, 2), mult=0) in potential_handler.slot_map
        )

    def test_store_nonstandard_improper_idivf(self):
        acetaldehyde = Molecule.from_mapped_smiles(
            "[C:1]([C:2](=[O:3])[H:7])([H:4])([H:5])[H:6]"
        )
        topology = acetaldehyde.to_topology()

        handler = ImproperTorsionHandler(version=0.3)
        handler.add_parameter(
            {
                "smirks": "[*:1]~[#6:2](~[#8:3])~[*:4]",
                "periodicity1": 2,
                "phase1": 180.0 * unit.degree,
                "k1": 1.1 * unit.kilocalorie_per_mole,
                "idivf1": 1.234 * unit.dimensionless,
                "id": "i1",
            }
        )

        potential_handler = SMIRNOFFImproperTorsionHandler._from_toolkit(
            parameter_handler=handler, topology=topology
        )

        handler = ImproperTorsionHandler(version=0.3)
        handler.default_idivf = 5.555
        handler.add_parameter(
            {
                "smirks": "[*:1]~[#6:2](~[#8:3])~[*:4]",
                "periodicity1": 2,
                "phase1": 180.0 * unit.degree,
                "k1": 1.1 * unit.kilocalorie_per_mole,
                "id": "i1",
            }
        )

        potential_handler = SMIRNOFFImproperTorsionHandler._from_toolkit(
            parameter_handler=handler, topology=topology
        )

        assert [*potential_handler.potentials.values()][0].parameters[
            "idivf"
        ] == 5.555 * unit.dimensionless

    def test_electrostatics_am1_handler(self):
        molecule = Molecule.from_smiles("C")
        molecule.assign_partial_charges(partial_charge_method="am1bcc")

        # Explicitly store these, since results differ RDKit/AmberTools vs. OpenEye
        reference_charges = [c.m for c in molecule.partial_charges]

        top = molecule.to_topology()

        parameter_handlers = [
            ElectrostaticsHandler(version=0.3),
            ToolkitAM1BCCHandler(version=0.3),
        ]

        electrostatics_handler = SMIRNOFFElectrostaticsHandler._from_toolkit(
            parameter_handlers, top
        )
        np.testing.assert_allclose(
            [charge.m_as(unit.e) for charge in electrostatics_handler.charges.values()],
            reference_charges,
        )

    def test_electrostatics_library_charges(self):
        top = Molecule.from_smiles("C").to_topology()

        library_charge_handler = LibraryChargeHandler(version=0.3)
        library_charge_handler.add_parameter(
            {
                "smirks": "[#6X4:1]-[#1:2]",
                "charge1": -0.1 * unit.elementary_charge,
                "charge2": 0.025 * unit.elementary_charge,
            }
        )

        parameter_handlers = [
            ElectrostaticsHandler(version=0.3),
            library_charge_handler,
        ]

        electrostatics_handler = SMIRNOFFElectrostaticsHandler._from_toolkit(
            parameter_handlers, top
        )

        np.testing.assert_allclose(
            [charge.m_as(unit.e) for charge in electrostatics_handler.charges.values()],
            [-0.1, 0.025, 0.025, 0.025, 0.025],
        )

    def test_electrostatics_charge_increments(self):
        molecule = Molecule.from_mapped_smiles("[Cl:1][H:2]")
        top = molecule.to_topology()

        molecule.assign_partial_charges(partial_charge_method="am1-mulliken")

        reference_charges = [c.m for c in molecule.partial_charges]
        reference_charges[0] += 0.1
        reference_charges[1] -= 0.1

        charge_increment_handler = ChargeIncrementModelHandler(version=0.3)
        charge_increment_handler.add_parameter(
            {
                "smirks": "[#17:1]-[#1:2]",
                "charge_increment1": 0.1 * unit.elementary_charge,
                "charge_increment2": -0.1 * unit.elementary_charge,
            }
        )

        parameter_handlers = [
            ElectrostaticsHandler(version=0.3),
            charge_increment_handler,
        ]

        electrostatics_handler = SMIRNOFFElectrostaticsHandler._from_toolkit(
            parameter_handlers, top
        )

        # AM1-Mulliken charges are [-0.168,  0.168], increments are [0.1, -0.1],
        # sum is [-0.068,  0.068]
        np.testing.assert_allclose(
            [charge.m_as(unit.e) for charge in electrostatics_handler.charges.values()],
            reference_charges,
        )

    def test_toolkit_am1bcc_uses_elf10_if_oe_is_available(self, sage):
        """
        Ensure that the ToolkitAM1BCCHandler assigns ELF10 charges if OpenEye is available.

        Taken from https://github.com/openforcefield/openff-toolkit/pull/1214,
        """
        molecule = Molecule.from_smiles("OCCCCCCO")

        try:
            molecule.assign_partial_charges(partial_charge_method="am1bccelf10")
            uses_elf10 = True
        except ValueError:
            molecule.assign_partial_charges(partial_charge_method="am1bcc")
            uses_elf10 = False

        partial_charges = [c.m for c in molecule.partial_charges]

        assigned_charges = [
            v.m
            for v in Interchange.from_smirnoff(sage, [molecule])[
                "Electrostatics"
            ].charges.values()
        ]

        try:
            from openeye import oechem

            openeye_available = oechem.OEChemIsLicensed()
        except ImportError:
            openeye_available = False

        if openeye_available:
            assert uses_elf10
            np.testing.assert_allclose(partial_charges, assigned_charges)
        else:
            assert not uses_elf10
            np.testing.assert_allclose(partial_charges, assigned_charges)


class TestInterchangeFromSMIRNOFF(_BaseTest):
    """General tests for Interchange.from_smirnoff. Some are ported from the toolkit."""

    def test_modified_nonbonded_cutoffs(self, sage):
        from openff.toolkit.tests.create_molecules import create_ethanol

        topology = Topology.from_molecules(create_ethanol())
        modified_sage = ForceField(sage.to_string())

        modified_sage["vdW"].cutoff = 0.777 * unit.angstrom
        modified_sage["Electrostatics"].cutoff = 0.777 * unit.angstrom

        out = Interchange.from_smirnoff(force_field=modified_sage, topology=topology)

        assert out["vdW"].cutoff == 0.777 * unit.angstrom
        assert out["Electrostatics"].cutoff == 0.777 * unit.angstrom

    def test_sage_tip3p_charges(self, tip3p_xml):
        """Ensure tip3p charges packaged with sage are applied over AM1-BCC charges.
        https://github.com/openforcefield/openff-toolkit/issues/1199"""
        sage = ForceField("openff-2.0.0.offxml")

        topology = Molecule.from_smiles("O").to_topology()
        topology.box_vectors = [4, 4, 4] * unit.nanometer

        out = Interchange.from_smirnoff(force_field=sage, topology=topology)
        found_charges = [v.m for v in out["Electrostatics"].charges.values()]

        assert np.allclose(found_charges, [-0.834, 0.417, 0.417])

    def test_infer_positions(self, sage):
        from openff.toolkit.tests.create_molecules import create_ethanol

        molecule = create_ethanol()

        assert Interchange.from_smirnoff(sage, [molecule]).positions is None

        molecule.generate_conformers(n_conformers=1)

        assert Interchange.from_smirnoff(sage, [molecule]).positions.shape == (
            molecule.n_atoms,
            3,
        )


@pytest.mark.slow()
class TestUnassignedParameters(_BaseTest):
    def test_catch_unassigned_bonds(self, sage, ethanol_top):
        for param in sage["Bonds"].parameters:
            param.smirks = "[#99:1]-[#99:2]"

        sage.deregister_parameter_handler(sage["Constraints"])

        with pytest.raises(
            UnassignedValenceParameterException,
            match="BondHandler was not able to find par",
        ):
            Interchange.from_smirnoff(force_field=sage, topology=ethanol_top)

    def test_catch_unassigned_angles(self, sage, ethanol_top):
        for param in sage["Angles"].parameters:
            param.smirks = "[#99:1]-[#99:2]-[#99:3]"

        with pytest.raises(
            UnassignedValenceParameterException,
            match="AngleHandler was not able to find par",
        ):
            Interchange.from_smirnoff(force_field=sage, topology=ethanol_top)

    def test_catch_unassigned_torsions(self, sage, ethanol_top):
        for param in sage["ProperTorsions"].parameters:
            param.smirks = "[#99:1]-[#99:2]-[#99:3]-[#99:4]"

        with pytest.raises(
            UnassignedProperTorsionParameterException,
            match="- Topology indices [(]5, 0, 1, 6[)]: "
            r"names and elements [(](H\d+)? H[)], [(](C\d+)? C[)], [(](C\d+)? C[)], [(](H\d+)? H[)],",
        ):
            Interchange.from_smirnoff(force_field=sage, topology=ethanol_top)


class TestConstraints(_BaseTest):
    @pytest.mark.parametrize(
        ("mol", "n_constraints"),
        [
            ("C", 4),
            ("CC", 6),
        ],
    )
    def test_num_constraints(self, sage, mol, n_constraints):
        bond_handler = sage["Bonds"]
        constraint_handler = sage["Constraints"]

        topology = Molecule.from_smiles(mol).to_topology()

        constraints = SMIRNOFFConstraintHandler._from_toolkit(
            parameter_handler=[
                val for val in [bond_handler, constraint_handler] if val is not None
            ],
            topology=topology,
        )

        assert len(constraints.slot_map) == n_constraints

    def test_constraints_with_distance(self, tip3p_xml):
        tip3p = ForceField(tip3p_xml)

        topology = Molecule.from_smiles("O").to_topology()
        topology.box_vectors = [4, 4, 4] * unit.nanometer

        constraints = SMIRNOFFConstraintHandler._from_toolkit(
            parameter_handler=tip3p["Constraints"], topology=topology
        )

        assert len(constraints.slot_map) == 3
        assert len(constraints.constraints) == 2


# TODO: Remove xfail after openff-toolkit 0.10.0
@pytest.mark.xfail()
def test_library_charges_from_molecule():
    mol = Molecule.from_mapped_smiles("[Cl:1][C:2]#[C:3][F:4]")

    with pytest.raises(ValueError, match="missing partial"):
        library_charge_from_molecule(mol)

    mol.partial_charges = np.linspace(-0.3, 0.3, 4) * unit.elementary_charge

    library_charges = library_charge_from_molecule(mol)

    assert isinstance(library_charges, LibraryChargeHandler.LibraryChargeType)
    assert library_charges.smirks == mol.to_smiles(mapped=True)
    assert library_charges.charge == [*mol.partial_charges]


class TestChargeFromMolecules(_BaseTest):
    def test_charge_from_molecules_basic(self, sage):

        molecule = Molecule.from_smiles("CCO")
        molecule.assign_partial_charges(partial_charge_method="am1bcc")
        molecule.partial_charges *= -1

        default = Interchange.from_smirnoff(sage, molecule.to_topology())
        uses = Interchange.from_smirnoff(
            sage,
            molecule.to_topology(),
            charge_from_molecules=[molecule],
        )

        found_charges_no_uses = [
            v.m for v in default["Electrostatics"].charges.values()
        ]
        found_charges_uses = [v.m for v in uses["Electrostatics"].charges.values()]

        assert not np.allclose(found_charges_no_uses, found_charges_uses)

        assert np.allclose(found_charges_uses, molecule.partial_charges.m)

    def test_charges_on_molecules_in_topology(self, sage):
        ethanol = Molecule.from_smiles("CCO")
        water = Molecule.from_mapped_smiles("[H:2][O:1][H:3]")
        ethanol_charges = np.linspace(-1, 1, 9) * 0.4
        water_charges = np.linspace(-1, 1, 3)

        ethanol.partial_charges = unit.Quantity(ethanol_charges, unit.elementary_charge)
        water.partial_charges = unit.Quantity(water_charges, unit.elementary_charge)

        out = Interchange.from_smirnoff(
            sage,
            [ethanol],
            charge_from_molecules=[ethanol, water],
        )

        for molecule in out.topology.molecules:
            if "C" in molecule.to_smiles():
                assert np.allclose(molecule.partial_charges.m, ethanol_charges)
            else:
                assert np.allclose(molecule.partial_charges.m, water_charges)

    def test_charges_from_molecule_reordered(self, sage):
        """Test the behavior of charge_from_molecules when the atom ordering differs with the topology"""

        # H - C # N
        molecule = hydrogen_cyanide(reversed=False)

        #  N  # C  - H
        # -0.3, 0.0, 0.3
        molecule_with_charges = hydrogen_cyanide(reversed=True)
        molecule_with_charges.partial_charges = unit.Quantity(
            [-0.3, 0.0, 0.3], unit.elementary_charge
        )

        out = Interchange.from_smirnoff(
            sage, molecule.to_topology(), charge_from_molecules=[molecule_with_charges]
        )

        expected_charges = [0.3, 0.0, -0.3]
        found_charges = [v.m for v in out["Electrostatics"].charges.values()]

        assert np.allclose(expected_charges, found_charges)


class TestPartialBondOrdersFromMolecules(_BaseTest):
    from openff.toolkit.tests.create_molecules import (
        create_ethanol,
        create_reversed_ethanol,
    )

    @pytest.mark.parametrize(
        (
            "get_molecule",
            "central_atoms",
        ),
        [
            (create_ethanol, (1, 2)),
            (create_reversed_ethanol, (7, 6)),
        ],
    )
    def test_interpolated_partial_bond_orders_from_molecules(
        self,
        get_molecule,
        central_atoms,
    ):
        """Test the fractional bond orders are used to interpolate k and length values as we expect,
        including that the fractional bond order is defined by the value on the input molecule via
        `partial_bond_order_from_molecules`, not whatever is produced by a default call to
        `Molecule.assign_fractional_bond_orders`.

        This test is adapted from test_fractional_bondorder_from_molecule in the toolkit.

        Parameter   | param values at bond orders 1, 2  | used bond order   | expected value
        bond k        101, 123 kcal/mol/A**2              1.55                113.1 kcal/mol/A**2
        bond length   1.4, 1.3 A                          1.55                1.345 A
        torsion k     1, 1.8 kcal/mol                     1.55                1.44 kcal/mol
        """
        mol = get_molecule()
        mol.get_bond_between(*central_atoms).fractional_bond_order = 1.55

        sorted_indices = tuple(sorted(central_atoms))

        from openff.toolkit.tests.test_forcefield import xml_ff_bo

        forcefield = ForceField("test_forcefields/test_forcefield.offxml", xml_ff_bo)
        topology = Topology.from_molecules(mol)

        out = Interchange.from_smirnoff(
            force_field=forcefield,
            topology=topology,
            partial_bond_orders_from_molecules=[mol],
        )

        bond_key = TopologyKey(atom_indices=sorted_indices, bond_order=1.55)
        bond_potential = out["Bonds"].slot_map[bond_key]
        found_bond_k = out["Bonds"].potentials[bond_potential].parameters["k"]
        found_bond_length = out["Bonds"].potentials[bond_potential].parameters["length"]

        assert found_bond_k.m_as(kcal_mol_a2) == pytest.approx(113.1)
        assert found_bond_length.m_as(unit.angstrom) == pytest.approx(1.345)

        # TODO: There should be a better way of plucking this torsion's TopologyKey
        for topology_key in out["ProperTorsions"].slot_map.keys():
            if (
                tuple(sorted(topology_key.atom_indices))[1:3] == sorted_indices
            ) and topology_key.bond_order == 1.55:
                torsion_key = topology_key
                break

        torsion_potential = out["ProperTorsions"].slot_map[torsion_key]
        found_torsion_k = (
            out["ProperTorsions"].potentials[torsion_potential].parameters["k"]
        )

        assert found_torsion_k.m_as(kcal_mol) == pytest.approx(1.44)

    def test_partial_bond_order_from_molecules_empty(self):
        from openff.toolkit.tests.test_forcefield import xml_ff_bo

        forcefield = ForceField("test_forcefields/test_forcefield.offxml", xml_ff_bo)

        molecule = Molecule.from_smiles("CCO")

        default = Interchange.from_smirnoff(forcefield, molecule.to_topology())
        empty = Interchange.from_smirnoff(
            forcefield,
            molecule.to_topology(),
            partial_bond_orders_from_molecules=list(),
        )

        assert _get_interpolated_bond_k(default["Bonds"]) == pytest.approx(
            _get_interpolated_bond_k(empty["Bonds"])
        )

    def test_partial_bond_order_from_molecules_no_matches(self):
        from openff.toolkit.tests.test_forcefield import xml_ff_bo

        forcefield = ForceField("test_forcefields/test_forcefield.offxml", xml_ff_bo)

        molecule = Molecule.from_smiles("CCO")
        decoy = Molecule.from_smiles("C#N")
        decoy.assign_fractional_bond_orders(bond_order_model="am1-wiberg")

        default = Interchange.from_smirnoff(forcefield, molecule.to_topology())
        uses = Interchange.from_smirnoff(
            forcefield,
            molecule.to_topology(),
            partial_bond_orders_from_molecules=[decoy],
        )

        assert _get_interpolated_bond_k(default["Bonds"]) == pytest.approx(
            _get_interpolated_bond_k(uses["Bonds"])
        )


class TestBondOrderInterpolation(_BaseTest):
    @pytest.mark.slow()
    def test_input_bond_orders_ignored(self):
        """Test that conformers existing in the topology are not considered in the bond order interpolation
        part of the parametrization process"""
        from openff.toolkit.tests.test_forcefield import create_ethanol

        mol = create_ethanol()
        mol.assign_fractional_bond_orders(bond_order_model="am1-wiberg")
        mod_mol = Molecule(mol)
        for bond in mod_mol.bonds:
            bond.fractional_bond_order += 0.1

        top = Topology.from_molecules(mol)
        mod_top = Topology.from_molecules(mod_mol)

        forcefield = ForceField(
            get_data_file_path("test_forcefields/test_forcefield.offxml"),
            self.xml_ff_bo_bonds,
        )

        bonds = SMIRNOFFBondHandler._from_toolkit(
            parameter_handler=forcefield["Bonds"], topology=top
        )
        bonds_mod = SMIRNOFFBondHandler._from_toolkit(
            parameter_handler=forcefield["Bonds"], topology=mod_top
        )

        for pot_key1, pot_key2 in zip(
            bonds.slot_map.values(), bonds_mod.slot_map.values()
        ):
            k1 = bonds.potentials[pot_key1].parameters["k"].m_as(kcal_mol_a2)
            k2 = bonds_mod.potentials[pot_key2].parameters["k"].m_as(kcal_mol_a2)
            assert k1 == pytest.approx(k2, rel=1e-5), (k1, k2)

    def test_input_conformers_ignored(self):
        """Test that conformers existing in the topology are not considered in the bond order interpolation
        part of the parametrization process"""
        from openff.toolkit.tests.test_forcefield import create_ethanol

        mol = create_ethanol()
        mol.assign_fractional_bond_orders(bond_order_model="am1-wiberg")
        mod_mol = Molecule(mol)
        mod_mol.generate_conformers()
        tmp = mod_mol._conformers[0][0][0]
        mod_mol._conformers[0][0][0] = mod_mol._conformers[0][1][0]
        mod_mol._conformers[0][1][0] = tmp

        top = Topology.from_molecules(mol)
        mod_top = Topology.from_molecules(mod_mol)

        forcefield = ForceField(
            get_data_file_path("test_forcefields/test_forcefield.offxml"),
            self.xml_ff_bo_bonds,
        )

        bonds = SMIRNOFFBondHandler._from_toolkit(
            parameter_handler=forcefield["Bonds"], topology=top
        )
        bonds_mod = SMIRNOFFBondHandler._from_toolkit(
            parameter_handler=forcefield["Bonds"], topology=mod_top
        )

        for key1, key2 in zip(bonds.potentials, bonds_mod.potentials):
            k1 = bonds.potentials[key1].parameters["k"].m_as(kcal_mol_a2)
            k2 = bonds_mod.potentials[key2].parameters["k"].m_as(kcal_mol_a2)
            assert k1 == pytest.approx(k2, rel=1e-5), (k1, k2)

    def test_fractional_bondorder_invalid_interpolation_method(self):
        """
        Ensure that requesting an invalid interpolation method leads to a
        FractionalBondOrderInterpolationMethodUnsupportedError
        """
        mol = Molecule.from_smiles("CCO")

        forcefield = ForceField(
            get_data_file_path("test_forcefields/test_forcefield.offxml"),
            self.xml_ff_bo_bonds,
        )
        forcefield["Bonds"]._fractional_bondorder_interpolation = "invalid method name"
        topology = Topology.from_molecules([mol])

        # TODO: Make this a more descriptive custom exception
        with pytest.raises(ValidationError):
            Interchange.from_smirnoff(forcefield, topology)


@skip_if_missing("jax")
class TestMatrixRepresentations(_BaseTest):
    @pytest.mark.parametrize(
        ("handler_name", "n_ff_terms", "n_sys_terms"),
        [("vdW", 10, 72), ("Bonds", 8, 64), ("Angles", 6, 104)],
    )
    def test_to_force_field_to_system_parameters(
        self, sage, ethanol_top, handler_name, n_ff_terms, n_sys_terms
    ):
        import jax

        if handler_name == "Bonds":
            handler = SMIRNOFFBondHandler._from_toolkit(
                parameter_handler=sage["Bonds"],
                topology=ethanol_top,
            )
        elif handler_name == "Angles":
            handler = SMIRNOFFAngleHandler._from_toolkit(
                parameter_handler=sage[handler_name],
                topology=ethanol_top,
            )
        elif handler_name == "vdW":
            handler = SMIRNOFFvdWHandler._from_toolkit(
                parameter_handler=sage[handler_name],
                topology=ethanol_top,
            )
        else:
            raise NotImplementedError()

        p = handler.get_force_field_parameters(use_jax=True)

        assert isinstance(p, jax.interpreters.xla.DeviceArray)
        assert np.prod(p.shape) == n_ff_terms

        q = handler.get_system_parameters(use_jax=True)

        assert isinstance(q, jax.interpreters.xla.DeviceArray)
        assert np.prod(q.shape) == n_sys_terms

        assert jax.numpy.allclose(q, handler.parametrize(p))

        param_matrix = handler.get_param_matrix()

        ref_file = get_test_file_path(f"ethanol_param_{handler_name.lower()}.npy")
        ref = jax.numpy.load(ref_file)

        assert jax.numpy.allclose(ref, param_matrix)

        # TODO: Update with other handlers that can safely be assumed to follow 1:1 slot:smirks mapping
        if handler_name in ["vdW", "Bonds", "Angles"]:
            assert np.allclose(
                np.sum(param_matrix, axis=1), np.ones(param_matrix.shape[0])
            )

    def test_set_force_field_parameters(self, sage, ethanol):
        import jax

        bond_handler = SMIRNOFFBondHandler._from_toolkit(
            parameter_handler=sage["Bonds"],
            topology=ethanol.to_topology(),
        )

        original = bond_handler.get_force_field_parameters(use_jax=True)
        modified = original * jax.numpy.array([1.1, 0.5])

        bond_handler.set_force_field_parameters(modified)

        assert (bond_handler.get_force_field_parameters(use_jax=True) == modified).all()


class TestParameterInterpolation(_BaseTest):
    xml_ff_bo = """<?xml version='1.0' encoding='ASCII'?>
    <SMIRNOFF version="0.3" aromaticity_model="OEAroModel_MDL">
      <Bonds version="0.3" fractional_bondorder_method="AM1-Wiberg"
        fractional_bondorder_interpolation="linear">
        <Bond
          smirks="[#6X4:1]~[#8X2:2]"
          id="bbo1"
          k_bondorder1="101.0 * kilocalories_per_mole/angstrom**2"
          k_bondorder2="123.0 * kilocalories_per_mole/angstrom**2"
          length_bondorder1="1.4 * angstrom"
          length_bondorder2="1.3 * angstrom"
          />
      </Bonds>
      <ProperTorsions version="0.3" potential="k*(1+cos(periodicity*theta-phase))">
        <Proper smirks="[*:1]~[#6X3:2]~[#6X3:3]~[*:4]" id="tbo1" periodicity1="2" phase1="0.0 * degree"
        k1_bondorder1="1.00*kilocalories_per_mole" k1_bondorder2="1.80*kilocalories_per_mole" idivf1="1.0"/>
        <Proper smirks="[*:1]~[#6X4:2]~[#8X2:3]~[*:4]" id="tbo2" periodicity1="2" phase1="0.0 * degree"
        k1_bondorder1="1.00*kilocalories_per_mole" k1_bondorder2="1.80*kilocalories_per_mole" idivf1="1.0"/>
      </ProperTorsions>
    </SMIRNOFF>
    """

    @pytest.mark.xfail(reason="Not yet implemented using input bond orders")
    def test_bond_order_interpolation(self):
        forcefield = ForceField(
            "test_forcefields/test_forcefield.offxml", self.xml_ff_bo
        )

        mol = Molecule.from_smiles("CCO")
        mol.generate_conformers(n_conformers=1)

        mol.bonds[1].fractional_bond_order = 1.5

        top = mol.to_topology()

        out = Interchange.from_smirnoff(forcefield, mol.to_topology())

        top_key = TopologyKey(
            atom_indices=(1, 2),
            bond_order=top.get_bond_between(1, 2).bond.fractional_bond_order,
        )
        assert out["Bonds"].potentials[out["Bonds"].slot_map[top_key]].parameters[
            "k"
        ] == 300 * unit.Unit("kilocalories / mol / angstrom ** 2")

    @pytest.mark.slow()
    @pytest.mark.xfail(reason="Not yet implemented using input bond orders")
    def test_bond_order_interpolation_similar_bonds(self):
        """Test that key mappings do not get confused when two bonds having similar SMIRKS matches
        have different bond orders"""
        forcefield = ForceField(
            "test_forcefields/test_forcefield.offxml", self.xml_ff_bo
        )

        # TODO: Construct manually to avoid relying on atom ordering
        mol = Molecule.from_smiles("C(CCO)O")
        mol.generate_conformers(n_conformers=1)

        mol.bonds[2].fractional_bond_order = 1.5
        mol.bonds[3].fractional_bond_order = 1.2

        top = mol.to_topology()

        out = Interchange.from_smirnoff(forcefield, top)

        bond1_top_key = TopologyKey(
            atom_indices=(2, 3),
            bond_order=top.get_bond_between(2, 3).bond.fractional_bond_order,
        )
        bond1_pot_key = out["Bonds"].slot_map[bond1_top_key]

        bond2_top_key = TopologyKey(
            atom_indices=(0, 4),
            bond_order=top.get_bond_between(0, 4).bond.fractional_bond_order,
        )
        bond2_pot_key = out["Bonds"].slot_map[bond2_top_key]

        assert np.allclose(
            out["Bonds"].potentials[bond1_pot_key].parameters["k"],
            300.0 * unit.Unit("kilocalories / mol / angstrom ** 2"),
        )

        assert np.allclose(
            out["Bonds"].potentials[bond2_pot_key].parameters["k"],
            180.0 * unit.Unit("kilocalories / mol / angstrom ** 2"),
        )

    @requires_openeye
    @pytest.mark.parametrize(
        (
            "get_molecule",
            "k_torsion_interpolated",
            "k_bond_interpolated",
            "length_bond_interpolated",
            "central_atoms",
        ),
        [
            (create_ethanol, 4.16586914, 42208.5402, 0.140054167256, (1, 2)),
            (create_reversed_ethanol, 4.16564555, 42207.9252, 0.14005483525, (7, 6)),
        ],
    )
    def test_fractional_bondorder_from_molecule(
        self,
        get_molecule,
        k_torsion_interpolated,
        k_bond_interpolated,
        length_bond_interpolated,
        central_atoms,
    ):
        """Copied from the toolkit with modified reference constants.
        Force constant computed by interpolating (k1, k2) = (101, 123) kcal/A**2/mol
        with bond order 1.00093035 (AmberTools 21.4, Python 3.8, macOS):
            101 + (123 - 101) * (0.00093035) = 101.0204677 kcal/A**2/mol
            = 42266.9637 kJ/nm**2/mol

        Same process with bond length (1.4, 1.3) A gives 0.1399906965 nm
        Same process with torsion k (1.0, 1.8) kcal/mol gives 4.18711406752 kJ/mol

        Using OpenEye (openeye-toolkits 2021.1.1, Python 3.8, macOS):
            bond order 0.9945832743790813
            bond k = 42208.5402 kJ/nm**2/mol
            bond length = 0.14005416725620918 nm
            torsion k = 4.16586914 kilojoules kJ/mol

        ... except OpenEye has a different fractional bond order for reversed ethanol
            bond order 0.9945164749654242
            bond k = 42207.9252 kJ/nm**2/mol
            bond length = 0.14005483525034576 nm
            torsion k = 4.16564555 kJ/mol

        """
        mol = get_molecule()
        forcefield = ForceField(
            "test_forcefields/test_forcefield.offxml", self.xml_ff_bo
        )
        topology = Topology.from_molecules(mol)

        out = Interchange.from_smirnoff(forcefield, topology)
        out.box = unit.Quantity(4 * np.eye(3), unit.nanometer)
        omm_system = out.to_openmm(combine_nonbonded_forces=True)

        # Verify that the assigned bond parameters were correctly interpolated
        off_bond_force = [
            force
            for force in omm_system.getForces()
            if isinstance(force, openmm.HarmonicBondForce)
        ][0]

        for idx in range(off_bond_force.getNumBonds()):
            params = off_bond_force.getBondParameters(idx)

            atom1, atom2 = params[0], params[1]
            atom1_mol, atom2_mol = central_atoms

            if ((atom1 == atom1_mol) and (atom2 == atom2_mol)) or (
                (atom1 == atom2_mol) and (atom2 == atom1_mol)
            ):
                k = params[-1]
                length = params[-2]
                np.testing.assert_allclose(
                    k / k.unit,
                    k_bond_interpolated,
                    atol=0,
                    rtol=2e-6,
                )
                np.testing.assert_allclose(
                    length / length.unit,
                    length_bond_interpolated,
                    atol=0,
                    rtol=2e-6,
                )

        # Verify that the assigned torsion parameters were correctly interpolated
        off_torsion_force = [
            force
            for force in omm_system.getForces()
            if isinstance(force, openmm.PeriodicTorsionForce)
        ][0]

        for idx in range(off_torsion_force.getNumTorsions()):
            params = off_torsion_force.getTorsionParameters(idx)

            atom2, atom3 = params[1], params[2]
            atom2_mol, atom3_mol = central_atoms

            if ((atom2 == atom2_mol) and (atom3 == atom3_mol)) or (
                (atom2 == atom3_mol) and (atom3 == atom2_mol)
            ):
                k = params[-1]
                np.testing.assert_allclose(
                    k / k.unit, k_torsion_interpolated, atol=0, rtol=2e-6
                )


def _get_interpolated_bond_k(bond_handler) -> float:
    for key in bond_handler.slot_map:
        if key.bond_order is not None:
            topology_key = key
            break
    potential_key = bond_handler.slot_map[topology_key]
    return bond_handler.potentials[potential_key].parameters["k"].m


class TestSMIRNOFFChargeIncrements(_BaseTest):
    def test_no_charge_increments_applied(self, sage):
        molecule = Molecule.from_smiles("OCCCCCCO")
        molecule.assign_partial_charges(partial_charge_method="gasteiger")
        gastiger_charges = molecule.partial_charges.m

        sage.deregister_parameter_handler("ToolkitAM1BCC")
        no_increments = ChargeIncrementModelHandler(
            version=0.3, partial_charge_method="gasteiger"
        )
        sage.register_parameter_handler(no_increments)

        assert len(sage["ChargeIncrementModel"].parameters) == 0

        out = Interchange.from_smirnoff(sage, [molecule])
        assert np.allclose(
            np.asarray([v.m for v in out["Electrostatics"].charges.values()]),
            gastiger_charges,
        )

    def test_overlapping_increments(self, sage):
        """Test that separate charge increments can be properly applied to the same atom."""
        molecule = Molecule.from_smiles("C")

        sage = ForceField("openff-2.0.0.offxml")
        sage.deregister_parameter_handler("ToolkitAM1BCC")
        charge_handler = ChargeIncrementModelHandler(
            version=0.3, partial_charge_method="formal_charge"
        )
        charge_handler.add_parameter(
            {
                "smirks": "[C:1][H:2]",
                "charge_increment1": 0.111 * unit.elementary_charge,
                "charge_increment2": -0.111 * unit.elementary_charge,
            }
        )
        sage.register_parameter_handler(charge_handler)
        assert 0.0 == pytest.approx(
            sum(
                v.m
                for v in Interchange.from_smirnoff(sage, [molecule])[
                    "Electrostatics"
                ].charges.values()
            )
        )

    def test_charge_increment_forwawrd_reverse_molecule(self, sage):
        sage.deregister_parameter_handler("ToolkitAM1BCC")
        sage.register_parameter_handler(hydrogen_cyanide_charge_increments())

        topology = Topology.from_molecules(
            [hydrogen_cyanide(reversed=False), hydrogen_cyanide(reversed=True)]
        )

        out = Interchange.from_smirnoff(sage, topology)

        expected_charges = [-0.111, 0.611, -0.5, -0.5, 0.611, -0.111]

        # TODO: Fix get_charges to return the atoms in order
        found_charges = [0.0] * topology.n_atoms
        for key, val in out["Electrostatics"].get_charges().items():
            found_charges[key.atom_indices[0]] = val.m

        assert np.allclose(expected_charges, found_charges)


class TestSMIRNOFFVirtualSites(_BaseTest):
    from openff.toolkit.tests.mocking import VirtualSiteMocking
    from openmm import unit as openmm_unit

    @classmethod
    def build_force_field(cls, v_site_handler: VirtualSiteHandler) -> ForceField:

        force_field = ForceField()

        force_field.get_parameter_handler("Bonds").add_parameter(
            {
                "smirks": "[*:1]~[*:2]",
                "k": 0.0 * unit.kilojoule_per_mole / unit.angstrom**2,
                "length": 0.0 * unit.angstrom,
            }
        )
        force_field.get_parameter_handler("Angles").add_parameter(
            {
                "smirks": "[*:1]~[*:2]~[*:3]",
                "k": 0.0 * unit.kilojoule_per_mole / unit.degree**2,
                "angle": 60.0 * unit.degree,
            }
        )
        force_field.get_parameter_handler("ProperTorsions").add_parameter(
            {
                "smirks": "[*:1]~[*:2]~[*:3]~[*:4]",
                "k": [0.0] * unit.kilojoule_per_mole,
                "phase": [0.0] * unit.degree,
                "periodicity": [2],
                "idivf": [1.0],
            }
        )
        force_field.get_parameter_handler("vdW").add_parameter(
            {
                "smirks": "[*:1]",
                "epsilon": 0.0 * unit.kilojoule_per_mole,
                "sigma": 1.0 * unit.angstrom,
            }
        )
        force_field.get_parameter_handler("LibraryCharges").add_parameter(
            {"smirks": "[*:1]", "charge": [0.0] * unit.elementary_charge}
        )
        force_field.get_parameter_handler("Electrostatics")

        force_field.register_parameter_handler(v_site_handler)

        return force_field

    @classmethod
    def generate_v_site_coordinates(
        cls,
        molecule: Molecule,
        input_conformer: unit.Quantity,
        parameter: VirtualSiteHandler.VirtualSiteType,
    ) -> unit.Quantity:

        # Compute the coordinates of the virtual site. Unfortunately OpenMM does not
        # seem to offer a more compact way to do this currently.
        handler = VirtualSiteHandler(version="0.3")
        handler.add_parameter(parameter=parameter)

        force_field = cls.build_force_field(handler)

        system = Interchange.from_smirnoff(
            force_field=force_field, topology=molecule.to_topology()
        ).to_openmm(combine_nonbonded_forces=True)

        n_v_sites = sum(
            1 if system.isVirtualSite(i) else 0 for i in range(system.getNumParticles())
        )

        input_conformer = unit.Quantity(
            np.vstack(
                [
                    input_conformer.m_as(unit.angstrom),
                    np.zeros((n_v_sites, 3)),
                ]
            ),
            unit.angstrom,
        )

        context = openmm.Context(
            system,
            openmm.VerletIntegrator(1.0 * openmm_unit.femtosecond),
            openmm.Platform.getPlatformByName("Reference"),
        )

        context.setPositions(to_openmm(input_conformer))
        context.computeVirtualSites()

        output_conformer = context.getState(getPositions=True).getPositions(
            asNumpy=True
        )

        return output_conformer[molecule.n_atoms :, :]

    @pytest.mark.parametrize(
        (
            "parameter",
            "smiles",
            "input_conformer",
            "atoms_to_shuffle",
            "expected_coordinates",
        ),
        [
            (
                VirtualSiteMocking.bond_charge_parameter("[Cl:1]-[C:2]"),
                "[Cl:1][C:2]([H:3])([H:4])[H:5]",
                VirtualSiteMocking.sp3_conformer(),
                (0, 1),
                unit.Quantity(np.array([[0.0, 3.0, 0.0]]), unit.angstrom),
            ),
            (
                VirtualSiteMocking.bond_charge_parameter("[C:1]#[C:2]"),
                "[H:1][C:2]#[C:3][H:4]",
                VirtualSiteMocking.sp1_conformer(),
                (2, 3),
                unit.Quantity(
                    np.array([[-3.0, 0.0, 0.0], [3.0, 0.0, 0.0]]), unit.angstrom
                ),
            ),
            (
                VirtualSiteMocking.monovalent_parameter("[O:1]=[C:2]-[H:3]"),
                "[O:1]=[C:2]([H:3])[H:4]",
                VirtualSiteMocking.sp2_conformer(),
                (0, 1, 2, 3),
                (
                    VirtualSiteMocking.sp2_conformer()[0]
                    + unit.Quantity(  # noqa
                        np.array([[1.0, np.sqrt(2), 1.0], [1.0, -np.sqrt(2), -1.0]]),
                        unit.angstrom,
                    )
                ),
            ),
            (
                VirtualSiteMocking.divalent_parameter(
                    "[H:2][O:1][H:3]", match="once", angle=0.0 * unit.degree
                ),
                "[H:2][O:1][H:3]",
                VirtualSiteMocking.sp2_conformer()[1:, :],
                (0, 1, 2),
                np.array([[2.0, 0.0, 0.0]]) * unit.angstrom,
            ),
            (
                VirtualSiteMocking.divalent_parameter(
                    "[H:2][O:1][H:3]",
                    match="all_permutations",
                    angle=45.0 * unit.degree,
                ),
                "[H:2][O:1][H:3]",
                VirtualSiteMocking.sp2_conformer()[1:, :],
                (0, 1, 2),
                unit.Quantity(
                    np.array(
                        [
                            [np.sqrt(2), np.sqrt(2), 0.0],
                            [np.sqrt(2), -np.sqrt(2), 0.0],
                        ]
                    ),
                    unit.angstrom,
                ),
            ),
            (
                VirtualSiteMocking.trivalent_parameter("[N:1]([H:2])([H:3])[H:4]"),
                "[N:1]([H:2])([H:3])[H:4]",
                VirtualSiteMocking.sp3_conformer()[1:, :],
                (0, 1, 2, 3),
                np.array([[0.0, 2.0, 0.0]]) * unit.angstrom,
            ),
        ],
    )
    def test_v_site_geometry(
        self,
        parameter: VirtualSiteHandler.VirtualSiteType,
        smiles: str,
        input_conformer: unit.Quantity,
        atoms_to_shuffle: Tuple[int, ...],
        expected_coordinates: unit.Quantity,
    ):
        """An integration test that virtual sites are placed correctly relative to the
        parent atoms"""

        for atom_permutation in itertools.permutations(atoms_to_shuffle):

            molecule = Molecule.from_mapped_smiles(smiles, allow_undefined_stereo=True)
            molecule._conformers = [input_conformer]

            # We shuffle the ordering of the atoms involved in the v-site orientation
            # to ensure that the orientation remains invariant as expected.
            shuffled_atom_order = {i: i for i in range(molecule.n_atoms)}
            shuffled_atom_order.update(
                {
                    old_index: new_index
                    for old_index, new_index in zip(atoms_to_shuffle, atom_permutation)
                }
            )

            molecule = molecule.remap(shuffled_atom_order)

            output_coordinates = self.generate_v_site_coordinates(
                molecule, molecule.conformers[0], parameter
            )

            assert output_coordinates.shape == expected_coordinates.shape

            def sort_coordinates(x: np.ndarray) -> np.ndarray:
                # Sort the rows by first, then second, then third columns as row
                # as the order of v-sites is not deterministic.
                return x[np.lexsort((x[:, 2], x[:, 1], x[:, 0])), :]

            found = sort_coordinates(
                output_coordinates.value_in_unit(openmm_unit.angstrom)
            )
            expected = sort_coordinates(expected_coordinates.m_as(unit.angstrom))

            assert np.allclose(found, expected), expected - found

    _E = unit.elementary_charge
    _A = unit.angstrom
    _KJ = unit.kilojoule_per_mole

    @pytest.mark.parametrize(
        ("topology", "parameters", "expected_parameters", "expected_n_v_sites"),
        [
            (
                Topology.from_molecules(
                    [
                        VirtualSiteMocking.chloromethane(reverse=False),
                        VirtualSiteMocking.chloromethane(reverse=True),
                    ]
                ),
                [VirtualSiteMocking.bond_charge_parameter("[Cl:1][C:2]")],
                [
                    # charge, sigma, epsilon
                    (0.1 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.2 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.0 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.0 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.0 * _E, 10.0 * _A, 0.0 * _KJ),
                    (-0.3 * _E, 4.0 * _A, 3.0 * _KJ),
                    (0.0 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.0 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.0 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.2 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.1 * _E, 10.0 * _A, 0.0 * _KJ),
                    (-0.3 * _E, 4.0 * _A, 3.0 * _KJ),
                ],
                2,
            ),
            # Check a complex case of bond charge vsite assignment and overwriting
            (
                Topology.from_molecules(
                    [
                        VirtualSiteMocking.formaldehyde(reverse=False),
                        VirtualSiteMocking.formaldehyde(reverse=True),
                    ]
                ),
                [
                    VirtualSiteMocking.bond_charge_parameter("[O:1]=[*:2]"),
                    VirtualSiteMocking.bond_charge_parameter(
                        "[O:1]=[C:2]", param_multiple=1.5
                    ),
                    VirtualSiteMocking.bond_charge_parameter(
                        "[O:1]=[CX3:2]", param_multiple=2.0, name="EP2"
                    ),
                ],
                [
                    # charge, sigma, epsilon
                    (0.35 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.7 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.0 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.0 * _E, 10.0 * _A, 0.0 * _KJ),
                    (-0.45 * _E, 6.0 * _A, 4.5 * _KJ),  # C=O vsite
                    (-0.6 * _E, 8.0 * _A, 6.0 * _KJ),  # CX3=O vsite
                    (0.0 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.0 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.7 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.35 * _E, 10.0 * _A, 0.0 * _KJ),
                    (-0.45 * _E, 6.0 * _A, 4.5 * _KJ),  # C=O vsite
                    (-0.6 * _E, 8.0 * _A, 6.0 * _KJ),  # CX3=O vsite
                ],
                4,
            ),
            (
                Topology.from_molecules(
                    [
                        VirtualSiteMocking.formaldehyde(reverse=False),
                        VirtualSiteMocking.formaldehyde(reverse=True),
                    ]
                ),
                [VirtualSiteMocking.monovalent_parameter("[O:1]=[C:2]-[H:3]")],
                [
                    # charge, sigma, epsilon
                    (0.2 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.4 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.3 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.3 * _E, 10.0 * _A, 0.0 * _KJ),
                    (-0.6 * _E, 4.0 * _A, 5.0 * _KJ),
                    (-0.6 * _E, 4.0 * _A, 5.0 * _KJ),
                    (0.3 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.3 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.4 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.2 * _E, 10.0 * _A, 0.0 * _KJ),
                    (-0.6 * _E, 4.0 * _A, 5.0 * _KJ),
                    (-0.6 * _E, 4.0 * _A, 5.0 * _KJ),
                ],
                4,
            ),
            (
                Topology.from_molecules(
                    [
                        VirtualSiteMocking.hypochlorous_acid(reverse=False),
                        VirtualSiteMocking.hypochlorous_acid(reverse=True),
                    ]
                ),
                [
                    VirtualSiteMocking.divalent_parameter(
                        "[H:2][O:1][Cl:3]", match="all_permutations"
                    )
                ],
                [
                    # charge, sigma, epsilon
                    (0.2 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.1 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.3 * _E, 10.0 * _A, 0.0 * _KJ),
                    (-0.6 * _E, 4.0 * _A, 5.0 * _KJ),
                    (0.3 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.1 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.2 * _E, 10.0 * _A, 0.0 * _KJ),
                    (-0.6 * _E, 4.0 * _A, 5.0 * _KJ),
                ],
                2,
            ),
            (
                Topology.from_molecules(
                    [
                        VirtualSiteMocking.fake_ammonia(reverse=False),
                        VirtualSiteMocking.fake_ammonia(reverse=True),
                    ]
                ),
                [VirtualSiteMocking.trivalent_parameter("[N:1]([Br:2])([Cl:3])[H:4]")],
                [
                    # charge, sigma, epsilon
                    (0.1 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.3 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.2 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.4 * _E, 10.0 * _A, 0.0 * _KJ),
                    (-1.0 * _E, 5.0 * _A, 6.0 * _KJ),
                    (0.4 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.2 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.3 * _E, 10.0 * _A, 0.0 * _KJ),
                    (0.1 * _E, 10.0 * _A, 0.0 * _KJ),
                    (-1.0 * _E, 5.0 * _A, 6.0 * _KJ),
                ],
                2,
            ),
        ],
    )
    def test_create_force(
        self,
        topology: Topology,
        parameters: List[VirtualSiteHandler.VirtualSiteType],
        expected_parameters: List[
            Tuple[openmm_unit.Quantity, openmm_unit.Quantity, openmm_unit.Quantity]
        ],
        expected_n_v_sites: int,
    ):

        expected_n_total = topology.n_atoms + expected_n_v_sites
        # sanity check the test input
        assert len(expected_parameters) == expected_n_total

        handler = VirtualSiteHandler(version="0.3")

        for parameter in parameters:
            handler.add_parameter(parameter=parameter)

        force_field = ForceField()

        force_field.register_parameter_handler(ElectrostaticsHandler(version=0.4))
        force_field.register_parameter_handler(LibraryChargeHandler(version=0.3))
        force_field.get_parameter_handler("LibraryCharges").add_parameter(
            {
                "smirks": "[*:1]",
                "charge": [0.0] * unit.elementary_charge,
            }
        )
        force_field.register_parameter_handler(vdWHandler(version=0.3))
        force_field.get_parameter_handler("vdW").add_parameter(
            {
                "smirks": "[*:1]",
                "epsilon": 0.0 * unit.kilojoule_per_mole,
                "sigma": 1.0 * unit.nanometer,
            }
        )
        force_field.register_parameter_handler(handler)

        system: openmm.System = Interchange.from_smirnoff(
            force_field, topology
        ).to_openmm(
            combine_nonbonded_forces=True,
        )

        assert system.getNumParticles() == expected_n_total

        # TODO: Explicitly ensure virtual sites are collated between moleucles
        #       This is implicitly tested in the construction of the parameter arrays

        assert system.getNumForces() == 1
        force: openmm.NonbondedForce = next(iter(system.getForces()))

        total_charge = 0.0 * openmm_unit.elementary_charge

        for i, (expected_charge, expected_sigma, expected_epsilon) in enumerate(
            expected_parameters
        ):
            charge, sigma, epsilon = force.getParticleParameters(i)

            # Make sure v-sites are massless.
            assert (
                np.isclose(system.getParticleMass(i)._value, 0.0)
            ) == system.isVirtualSite(i)

            assert np.isclose(
                expected_charge.m_as(unit.elementary_charge),
                charge.value_in_unit(openmm_unit.elementary_charge),
            )
            assert np.isclose(
                expected_sigma.m_as(unit.angstrom),
                sigma.value_in_unit(openmm_unit.angstrom),
            )
            assert np.isclose(
                expected_epsilon.m_as(unit.kilojoule_per_mole),
                epsilon.value_in_unit(openmm_unit.kilojoule_per_mole),
            )

            total_charge += charge

        expected_total_charge = sum(
            molecule.total_charge.m_as(unit.elementary_charge)
            for molecule in topology.molecules
        )

        assert np.isclose(
            expected_total_charge,
            total_charge.value_in_unit(openmm_unit.elementary_charge),
        )
