#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build optimisation problems from PyPSA networks with Linopy.
"""
import os
import logging

from linopy import Model
import numpy as np
import pandas as pd

from ..descriptors import (
    nominal_attrs,
    get_switchable_as_dense as get_as_dense,
    additional_linkports,
)
from ..pf import _as_snapshots

from .common import set_from_frame

from .variables import (
    define_nominal_variables,
    define_operational_variables,
    define_status_variables,
    define_spillage_variables,
)
from .constraints import (
    define_operational_constraints_for_non_extendables,
    define_operational_constraints_for_committables,
    define_operational_constraints_for_extendables,
    define_nominal_constraints_for_extendables,
    define_fixed_operation_constraints,
    define_fixed_nominal_constraints,
    define_ramp_limit_constraints,
    define_nodal_balance_constraints,
    define_kirchhoff_constraints,
    define_storage_unit_constraints,
    define_store_constraints,
)

from .global_constraints import (
    define_growth_limit,
    define_nominal_constraints_per_bus_carrier,
    define_primary_energy_limit,
    define_transmission_expansion_cost_limit,
    define_transmission_volume_expansion_limit,
)

from .abstract import iterative_transmission_capacity_expansion

logger = logging.getLogger(__name__)


lookup = pd.read_csv(
    os.path.join(os.path.dirname(__file__), "..", "variables.csv"),
    index_col=["component", "variable"],
)


def sanity_check(n):
    for c in {"Generator", "Link"}:
        intersection = n.get_committable_i(c).intersection(n.get_extendable_i(c))
        if not intersection.empty:
            raise ValueError(
                "Assets can only be committable or extendable. Found "
                f"assets in component {c} which are both:"
                f"\n\n\t{', '.join(intersection)}"
            )


def define_objective(n, sns):
    """
    Defines and writes out the objective function

    """
    m = n.model

    if n._multi_invest:
        periods = sns.unique("period")
        period_weighting = n.investment_period_weightings.objective[periods]

    # constant for already done investment
    nom_attr = nominal_attrs.items()
    constant = 0
    for c, attr in nom_attr:
        ext_i = n.get_extendable_i(c)
        cost = n.df(c)["capital_cost"][ext_i]
        if cost.empty:
            continue

        if n._multi_invest:
            active = pd.concat(
                {
                    period: n.get_active_assets(c, period)[ext_i]
                    for period in sns.unique("period")
                },
                axis=1,
            )
            cost = active @ period_weighting * cost

        constant += cost @ n.df(c)[attr][ext_i]

    if constant != 0:
        object_const = m.add_variables(constant, constant, name="objective_constant")
        m.objective = m.objective - 1 * object_const
        n.objective_constant = constant

    # marginal cost
    weighting = n.snapshot_weightings.objective
    if n._multi_invest:
        weighting = weighting.mul(period_weighting, level=0).loc[sns]
    else:
        weighting = weighting.loc[sns]

    for c, attr in lookup.query("marginal_cost").index:
        cost = (
            get_as_dense(n, c, "marginal_cost", sns)
            .loc[:, lambda ds: (ds != 0).all()]
            .mul(weighting, axis=0)
        )
        if cost.empty:
            continue
        operation = m[f"{c}-{attr}"].sel({"snapshot": sns, c: cost.columns})
        m.objective = m.objective + (operation * cost).sum()

    # investment
    for c, attr in nominal_attrs.items():
        ext_i = n.get_extendable_i(c)
        cost = n.df(c)["capital_cost"][ext_i]
        if cost.empty:
            continue

        if n._multi_invest:
            active = pd.concat(
                {
                    period: n.get_active_assets(c, period)[ext_i]
                    for period in sns.unique("period")
                },
                axis=1,
            )
            cost = active @ period_weighting * cost

        caps = m[f"{c}-{attr}"]
        m.objective = m.objective + (caps * cost).sum()


def create_model(n, snapshots=None, multi_investment_periods=False, **kwargs):
    """
    Create a linopy.Model instance from a pypsa network.


    Parameters
    ----------
    n : pypsa.Network
    snapshots : list or index slice
        A list of snapshots to optimise, must be a subset of
        network.snapshots, defaults to network.snapshots
    multi_investment_periods : bool, default False
        Whether to optimise as a single investment period or to optimise in multiple
        investment periods. Then, snapshots should be a ``pd.MultiIndex``.
    **kwargs:
        Keyword arguments used by `linopy.Model()`, such as `solver_dir` or `chunk`.

    Returns
    -------
    linopy.model
    """
    sns = _as_snapshots(n, snapshots)
    n._multi_invest = int(multi_investment_periods)

    kwargs.setdefault("force_dim_names", True)
    n.model = Model(**kwargs)

    # Define variables
    for c, attr in lookup.query("nominal").index:
        define_nominal_variables(n, c, attr)

    for c, attr in lookup.query("not nominal and not handle_separately").index:
        define_operational_variables(n, sns, c, attr)
        define_status_variables(n, sns, c)

    define_spillage_variables(n, sns)
    define_operational_variables(n, sns, "Store", "p")

    # Define constraints
    for c, attr in lookup.query("nominal").index:
        define_nominal_constraints_for_extendables(n, c, attr)
        define_fixed_nominal_constraints(n, c, attr)

    for c, attr in lookup.query("not nominal and not handle_separately").index:
        define_operational_constraints_for_non_extendables(n, sns, c, attr)
        define_operational_constraints_for_extendables(n, sns, c, attr)
        define_operational_constraints_for_committables(n, sns, c)
        define_ramp_limit_constraints(n, sns, c)
        define_fixed_operation_constraints(n, sns, c, attr)

    define_nodal_balance_constraints(n, sns)
    define_kirchhoff_constraints(n, sns)
    define_storage_unit_constraints(n, sns)
    define_store_constraints(n, sns)

    # Define global constraints
    define_nominal_constraints_per_bus_carrier(n, sns)
    define_growth_limit(n, sns)
    define_transmission_expansion_cost_limit(n, sns)
    define_transmission_volume_expansion_limit(n, sns)
    define_primary_energy_limit(n, sns)

    define_objective(n, sns)

    return n.model


def assign_solution(n, sns):
    """
    Map solution to network components.
    """
    m = n.model

    for name, sol in m.solution.items():

        if name == "objective_constant":
            continue

        c, attr = name.split("-", 1)
        df = sol.to_pandas()

        if "snapshot" in sol.dims:

            if c in n.passive_branch_components and attr == "s":
                set_from_frame(n, c, "p0", df)
                set_from_frame(n, c, "p1", -df)

            elif c == "Link" and attr == "p":
                set_from_frame(n, c, "p0", df)

                for i in ["1"] + additional_linkports(n):
                    i_eff = "" if i == "1" else i
                    eff = get_as_dense(n, "Link", f"efficiency{i_eff}", sns)
                    set_from_frame(n, c, f"p{i}", -df * eff)
                    n.pnl(c)[f"p{i}"].loc[
                        sns, n.links.index[n.links[f"bus{i}"] == ""]
                    ] = n.component_attrs["Link"].loc[f"p{i}", "default"]

            else:
                set_from_frame(n, c, attr, df)
        else:
            n.df(c)[attr + "_opt"].update(df)

    # if nominal capacity was no variable set optimal value to nominal
    for (c, attr) in lookup.query("nominal").index:
        if f"{c}-{attr}" not in m.variables:
            n.df(c)[attr + "_opt"] = n.df(c)[attr]

    # recalculate storageunit net dispatch
    if not n.df("StorageUnit").empty:
        c = "StorageUnit"
        n.pnl(c)["p"] = n.pnl(c)["p_dispatch"] - n.pnl(c)["p_store"]

    n.objective = m.objective_value
    n.objective_constant = m.solution["objective_constant"].item()


# TODO
# def assign_duals(n, sns):
#     """
#     Map dual values i.e. shadow prices to network components.
#     """
#     m = n.model

#         sign = 1 if "upper" in attr or attr == "marginal_price" else -1
#         n.dualvalues.at[(c, attr), "pnl"] = is_pnl
#         to_component = c in n.all_components
#         if is_pnl:
#             n.dualvalues.at[(c, attr), "in_comp"] = to_component
#             duals = constraints.applymap(
#                 lambda x: sign * constraints_dual.loc[x]
#                 if x in constraints_dual.index
#                 else np.nan
#             )
#             if c not in n.duals and not to_component:
#                 n.duals[c] = Dict(df=pd.DataFrame(), pnl={})
#             pnl = n.pnl(c) if to_component else n.duals[c].pnl
#             set_from_frame(pnl, attr, duals)
#         else:
#             # here to_component can change
#             duals = constraints.map(sign * constraints_dual)
#             if to_component:
#                 to_component = duals.index.isin(n.df(c).index).all()
#             n.dualvalues.at[(c, attr), "in_comp"] = to_component
#             if c not in n.duals and not to_component:
#                 n.duals[c] = Dict(df=pd.DataFrame(), pnl={})
#             df = n.df(c) if to_component else n.duals[c].df
#             df[attr] = duals

#     n.duals = Dict()
#     n.dualvalues = pd.DataFrame(index=sp, columns=["in_comp", "pnl"])
#     # extract shadow prices attached to components
#     for c, attr in sp:
#         map_dual(c, attr)


def post_processing(n, sns):
    """
    Post-process the optimzed network.

    This calculates quantities derived from the optimized values such as
    power injection per bus and snapshot, voltage angle.

    """
    # correct prices with objective weightings
    if n._multi_invest:
        period_weighting = n.investment_period_weightings.objective
        weightings = n.snapshot_weightings.objective.mul(
            period_weighting, level=0, axis=0
        ).loc[sns]
    else:
        weightings = n.snapshot_weightings.objective.loc[sns]

    n.buses_t.marginal_price.loc[sns] = n.buses_t.marginal_price.loc[sns].divide(
        weightings, axis=0
    )

    # load
    if len(n.loads):
        set_from_frame(n, "Load", "p", get_as_dense(n, "Load", "p_set", sns))

    # recalculate injection
    ca = [
        ("Generator", "p", "bus"),
        ("Store", "p", "bus"),
        ("Load", "p", "bus"),
        ("StorageUnit", "p", "bus"),
        ("Link", "p0", "bus0"),
        ("Link", "p1", "bus1"),
    ]
    for i in additional_linkports(n):
        ca.append(("Link", f"p{i}", f"bus{i}"))

    sign = lambda c: n.df(c).sign if "sign" in n.df(c) else -1  # sign for 'Link'
    n.buses_t.p = (
        pd.concat(
            [
                n.pnl(c)[attr].mul(sign(c)).rename(columns=n.df(c)[group])
                for c, attr, group in ca
            ],
            axis=1,
        )
        .groupby(level=0, axis=1)
        .sum()
        .reindex(columns=n.buses.index, fill_value=0)
    )

    def v_ang_for_(sub):
        buses_i = sub.buses_o
        if len(buses_i) == 1:
            return pd.DataFrame(0, index=sns, columns=buses_i)
        sub.calculate_B_H(skip_pre=True)
        Z = pd.DataFrame(np.linalg.pinv((sub.B).todense()), buses_i, buses_i)
        Z -= Z[sub.slack_bus]
        return n.buses_t.p.reindex(columns=buses_i) @ Z

    n.buses_t.v_ang = pd.concat(
        [v_ang_for_(sub) for sub in n.sub_networks.obj], axis=1
    ).reindex(columns=n.buses.index, fill_value=0)


def optimize(
    n, snapshots=None, multi_investment_periods=False, model_kwargs={}, **solve_kwargs
):
    """
    Optimize the pypsa network.

    n : pypsa.Network
    snapshots : list or index slice
        A list of snapshots to optimise, must be a subset of
        n.snapshots, defaults to n.snapshots
    multi_investment_periods : bool, default False
        Whether to optimise as a single investment period or to optimise in multiple
        investment periods. Then, snapshots should be a ``pd.MultiIndex``.
    model_kwargs: dict
        Keyword arguments used by `linopy.Model`, such as `solver_dir` or `chunk`.
    **kwargs:
        Keyword argument used by `linopy.Model.solve`, such as `solver_name`,
        `problem_fn` or solver options directly passed to the solver.

    Returns
    -------
    None.

    """

    sns = _as_snapshots(n, snapshots)
    n._multi_invest = int(multi_investment_periods)

    sanity_check(n)
    m = create_model(n, sns, multi_investment_periods, **model_kwargs)
    status, condition = m.solve(**solve_kwargs)

    if status == "ok":
        assign_solution(n, sns)
        post_processing(n, sns)

    return status, condition


def is_documented_by(original):
    def wrapper(target):
        target.__doc__ = original.__doc__
        return target

    return wrapper


class OptimizationAccessor:
    """Optimization accessor for building and solving models using linopy."""

    def __init__(self, network):
        self._parent = network

    def __call__(self, *args, **kwargs):
        return optimize(self._parent, *args, **kwargs)

    __call__.__doc__ = optimize.__doc__

    @is_documented_by(create_model)
    def create_model(self, **kwargs):
        return create_model(self._parent, **kwargs)

    @is_documented_by(assign_solution)
    def assign_solution(self, **kwargs):
        return assign_solution(self._parent, **kwargs)

    @is_documented_by(post_processing)
    def post_processing(self, **kwargs):
        return post_processing(self._parent, **kwargs)

    @is_documented_by(iterative_transmission_capacity_expansion)
    def iterative_transmission_capacity_expansion(self, **kwargs):
        iterative_transmission_capacity_expansion(self._parent, **kwargs)

    def fix_optimal_capacities(self):
        n = self._parent
        for c, attr in nominal_attrs.items():
            ext_i = n.get_extendable_i(c)
            n.df(c).loc[ext_i, attr] = n.df(c).loc[ext_i, attr + "_opt"]
            n.df(c)[attr + "_extendable"] = False
