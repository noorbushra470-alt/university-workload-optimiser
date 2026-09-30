"""
Workload optimiser — student template

- You can edit:
    - DecisionVars
    - build_model(instance: InstanceData) -> tuple[cp_model.CpModel, DecisionVars]
    - solve(instance: InstanceData, config: SolverConfig) -> cp_model.Solver
    - extract_solution(instance: InstanceData, solver: cp_model.CpSolver, vars_: DecisionVars) -> WorkloadOutput
    - apply_search_strategy(model: cp_model.CpModel, vars_: DecisionVars) -> None


These classes and functions currently raise exceptions if not implemented. You must add your logic to them.

An automated testing system relies on these specific function names and argument types to operate. DO NOT MODIFY their signatures

Failure to comply (changing arguments, names, or return types) will result in a non-executable solution and a mark of 0.

Notes:
- Keep all IDs as strings from InstanceData.
- Keep time units as minutes (integers) when you compute any summaries.
- You may return partial results (leave WorkloadOutput fields as None).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import argparse
import json
from pathlib import Path

from ortools.sat.python import cp_model
from interface import *


# -------------------------
# DO NOT MODIFY
# -------------------------
def config_solver(model: cp_model.CpModel, config: SolverConfig) -> cp_model.CpSolver:
    """
    Configure and solve the CP-SAT model.
    """
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(config.time_limit_seconds)
    solver.parameters.num_search_workers = int(config.num_workers)
    solver.parameters.random_seed = int(config.random_seed)
    solver.parameters.log_search_progress = bool(config.log_search_progress)

    return solver


# -----------------------------------------
# Your implemention starts from this point
# -----------------------------------------

# ---------------------------------------------------------
# Decision variables container
# ---------------------------------------------------------

@dataclass
class DecisionVars:
    # Assignment variables
    module_leader: Dict[Tuple[str, str], cp_model.IntVar] = field(default_factory=dict)
    practical_assignment: Dict[Tuple[str, str, int], cp_model.IntVar] = field(default_factory=dict)
    supervision: Dict[Tuple[str, str], cp_model.IntVar] = field(default_factory=dict)

    # Workload variables (minutes)
    teaching_minutes: Dict[str, cp_model.IntVar] = field(default_factory=dict)
    admin_minutes: Dict[str, cp_model.IntVar] = field(default_factory=dict)
    research_minutes: Dict[str, cp_model.IntVar] = field(default_factory=dict)
    total_minutes: Dict[str, cp_model.IntVar] = field(default_factory=dict)

    # Objective helpers
    max_load: Optional[cp_model.IntVar] = None
    min_load: Optional[cp_model.IntVar] = None
    fairness_cost: Optional[cp_model.IntVar] = None
    quality_cost: Optional[cp_model.IntVar] = None
    efficiency_cost: Optional[cp_model.IntVar] = None
    total_research: Optional[cp_model.IntVar] = None



# -------------------------
# Model implementation
# -------------------------

def build_model(instance: InstanceData):

    model = cp_model.CpModel()
    vars_ = DecisionVars()

    staff = instance.staff_ids
    modules = instance.module_ids

    # ----------------------------------------------------
    # MODULE LEADERS
    # ----------------------------------------------------

    for s in staff:
        for m in modules:
            vars_.module_leader[(s, m)] = model.NewBoolVar(f"leader_{s}_{m}")

    for m in modules:
        model.Add(sum(vars_.module_leader[(s, m)] for s in staff) == 1)

    for s in staff:
        model.Add(
            sum(vars_.module_leader[(s, m)] for m in modules)
            <= instance.max_modules_led_per_staff
        )

    for s in staff:
        for m in modules:
            if instance.skill_level[s][m] < instance.leader_min_skill:
                model.Add(vars_.module_leader[(s, m)] == 0)


    # -------------------------------------------------------
    # PRACTICAL GROUPS
    # -------------------------------------------------------

    module_groups = {}

    for m in modules:
        students = instance.module_enrolment[m]
        groups = (students + instance.practical_group_max_students - 1) // instance.practical_group_max_students
        module_groups[m] = groups

        for g in range(groups):
            for s in staff:
                vars_.practical_assignment[(s, m, g)] = model.NewBoolVar(
                    f"prac_{s}_{m}_{g}"
                )

            model.Add(
                sum(vars_.practical_assignment[(s, m, g)] for s in staff) == 1
            )

    # ---------------------------------------------------------
    # LEADER MUST TEACH AT LEAST ONE PRACTICAL
    # ---------------------------------------------------------
    for m in modules:
        for s in staff:
            practicals = [
                vars_.practical_assignment[(s, m, g)]
                for g in range(module_groups[m])
            ]

            model.Add(sum(practicals) >= 1).OnlyEnforceIf(
                vars_.module_leader[(s, m)]
            )
    # ------------------------------------------------------------
    # MAX 3 TUTORS PER MODULE
    # ------------------------------------------------------------

    for m in modules:
        tutors = []

        for s in staff:
            teaches = model.NewBoolVar(f"{s}_teaches_{m}")

            practicals = [
                vars_.practical_assignment[(s, m, g)]
                for g in range(module_groups[m])
            ]

            model.AddMaxEquality(teaches, practicals)
            tutors.append(teaches)

        model.Add(sum(tutors) <= 3)

    # ---------------------------------------------------------
    # SUPERVISION
    # ----------------------------------------------------------

    for s in staff:
        for st in instance.student_ids:
            vars_.supervision[(s, st)] = model.NewBoolVar(f"sup_{s}_{st}")

    for st in instance.student_ids:
        model.Add(sum(vars_.supervision[(s, st)] for s in staff) == 1)

    for s in staff:
        model.Add(
            sum(vars_.supervision[(s, st)] for st in instance.student_ids)
            <= instance.supervision_max_students_per_staff
        )

    # --------------------------------------------------------------
    # WORKLOAD CALCULATION
    # --------------------------------------------------------------
    MAX_WEEK = instance.weekly_total

    for s in staff:

        vars_.teaching_minutes[s] = model.NewIntVar(0, MAX_WEEK, f"teach_{s}")
        vars_.admin_minutes[s] = model.NewIntVar(0, MAX_WEEK, f"admin_{s}")
        vars_.research_minutes[s] = model.NewIntVar(0, MAX_WEEK, f"research_{s}")
        vars_.total_minutes[s] = model.NewIntVar(0, MAX_WEEK, f"total_{s}")

        teaching_terms = []

        # --------------------------------------------------
        # LECTURES
        # -------------------------------------------------- 
        for m in modules:

            skill = instance.skill_level[s][m]
            extra = 0 if skill == 3 else 30 if skill == 2 else 60

            delivery = instance.lecture_delivery_per_module
            prep = delivery // 2 + extra

            teaching_terms.append(
                (delivery + prep) * vars_.module_leader[(s, m)]
            )

        # -------------------------------------------
        #  PRACTICALS 
        # -------------------------------------------
        for (s2, m, g), var in vars_.practical_assignment.items():
            if s2 == s:
                skill = instance.skill_level[s][m]
                extra = 0 if skill == 3 else 30 if skill == 2 else 60

                delivery = instance.practical_delivery_per_group_per_module
                prep = delivery // 2 + extra

                teaching_terms.append((delivery + prep) * var)

        # ----------------------------------------------------
        # SUPERVISION 
        # ----------------------------------------------------
        for (s2, st), var in vars_.supervision.items():
            if s2 == s:
                delivery = instance.supervision_meeting_duration
                prep = delivery // 2
                teaching_terms.append((delivery + prep) * var)

        model.Add(vars_.teaching_minutes[s] == sum(teaching_terms))

        # -----------------------------------------------------
        # HARD TEACHING LIMITS
        # -----------------------------------------------------
        model.Add(vars_.teaching_minutes[s] <= instance.teaching_max)
        model.Add(vars_.teaching_minutes[s] >= instance.teaching_min)

        # -----------------------------------------------------
        #  ADMIN
        # ----------------------------------------------------- 
        admin_terms = []
        admin_terms.append(instance.base_admin_per_staff)

        for m in modules:
            students = instance.module_enrolment[m]
            proportional_admin = int(
                instance.leadership_admin_per_100_students * students / 100
            )
            admin_terms.append(
                proportional_admin * vars_.module_leader[(s, m)]
            )

        for (s2, m, g), var in vars_.practical_assignment.items():
            if s2 == s:
                admin_terms.append(instance.practical_admin_per_group * var)

        model.Add(vars_.admin_minutes[s] == sum(admin_terms))
        model.Add(vars_.admin_minutes[s] >= instance.admin_min)
        model.Add(vars_.admin_minutes[s] <= instance.admin_max)
         
        # ---------------------------------------------------------- 
        # RESEARCH 
        # ----------------------------------------------------------
        model.Add(
            vars_.research_minutes[s] ==
            instance.weekly_total
            - vars_.teaching_minutes[s]
            - vars_.admin_minutes[s]
        )

        model.Add(vars_.research_minutes[s] >= instance.research_min)
        
        # ---------------------------------------------------------
        # TOTAL 
        # ---------------------------------------------------------
        model.Add(
            vars_.total_minutes[s] ==
            vars_.teaching_minutes[s]
            + vars_.admin_minutes[s]
            + vars_.research_minutes[s]
        )

        model.Add(vars_.total_minutes[s] == instance.weekly_total)

    # -------------------------------------------------------------
    # FAIRNESS
    # -------------------------------------------------------------
    vars_.max_load = model.NewIntVar(0, instance.weekly_total, "max_load")
    vars_.min_load = model.NewIntVar(0, instance.weekly_total, "min_load")

    for s in staff:
        model.Add(vars_.max_load >= vars_.total_minutes[s])
        model.Add(vars_.min_load <= vars_.total_minutes[s])

    vars_.fairness_cost = model.NewIntVar(
        0, instance.weekly_total, "fairness_cost"
    )

    model.Add(vars_.fairness_cost == vars_.max_load - vars_.min_load)

    vars_.total_research = model.NewIntVar(
        0, len(staff) * instance.weekly_total, "total_research"
    )

    model.Add(
        vars_.total_research ==
        sum(vars_.research_minutes[s] for s in staff)
    )
    return model, vars_

def add_objectives(model, vars_, instance):
    """
    Add soft constraints (strategic goals).
    """
    # -----------------------------------------------------------
    # QUALITY OBJECTIVE
    # -----------------------------------------------------------
    leader_skill_penalties = []

    for m in instance.module_ids:
        for s in instance.staff_ids:

            skill = instance.skill_level[s][m]
            penalty_value = 3 - skill

            penalty_var = model.NewIntVar(0, 2, f"leader_skill_penalty_{s}_{m}")

            model.Add(penalty_var == penalty_value).OnlyEnforceIf(
                vars_.module_leader[(s, m)]
            )

            model.Add(penalty_var == 0).OnlyEnforceIf(
                vars_.module_leader[(s, m)].Not()
            )

            leader_skill_penalties.append(penalty_var)

    vars_.quality_cost = model.NewIntVar(
        0, 2 * len(instance.module_ids), "quality_cost"
    )

    model.Add(vars_.quality_cost == sum(leader_skill_penalties))

    # ------------------------------------------------------------------
    # PRACTICAL-LEADER SKILL MISMATCH PENALTY
    # ------------------------------------------------------------------
    mismatch_penalties = []

    for m in instance.module_ids:

        for (s, m2, g), var in vars_.practical_assignment.items():

            if m2 != m:
                continue

            for leader in instance.staff_ids:

                leader_var = vars_.module_leader[(leader, m)]

                tutor_skill = instance.skill_level[s][m]
                leader_skill = instance.skill_level[leader][m]

                if tutor_skill > leader_skill:

                    penalty = model.NewBoolVar(
                        f"mismatch_{s}_{leader}_{m}_{g}"
                    )

                    model.AddBoolAnd([var, leader_var]).OnlyEnforceIf(penalty)
                    model.AddBoolOr(
                        [var.Not(), leader_var.Not()]
                    ).OnlyEnforceIf(penalty.Not())

                    mismatch_penalties.append(penalty)

                    if mismatch_penalties:

                            mismatch_cost = model.NewIntVar(
                            0,
                            len(mismatch_penalties),
                            "mismatch_cost"
                            )

                            model.Add(mismatch_cost == sum(mismatch_penalties))

                            total_quality = model.NewIntVar(
                                0,
                                1000,
                                "quality_with_mismatch"
                            )

                            model.Add(
                                total_quality == vars_.quality_cost + mismatch_cost
                            )

                            vars_.quality_cost = total_quality

    # --------------------------------------------------------------------
    # LEADER PRACTICAL PRIORITY
    # --------------------------------------------------------------------
    leader_practical_penalties = []

    for m in instance.module_ids:

        for (s, m2, g), var in vars_.practical_assignment.items():

            if m2 != m:
                continue

            leader_var = vars_.module_leader[(s, m)]

            penalty = model.NewBoolVar(
                f"leader_not_teaching_{s}_{m}_{g}"
            )

            model.AddBoolAnd([var, leader_var.Not()]).OnlyEnforceIf(penalty)

            model.AddBoolOr(
                [var.Not(), leader_var]
            ).OnlyEnforceIf(penalty.Not())

            leader_practical_penalties.append(penalty)

    if leader_practical_penalties:

        leader_priority_cost = model.NewIntVar(
            0,
            len(leader_practical_penalties),
            "leader_priority_cost"
        )

        model.Add(
            leader_priority_cost == sum(leader_practical_penalties)
        )

        total_quality = model.NewIntVar(
            0,
            1000,
            "quality_with_leader_priority"
        )

        model.Add(
            total_quality == vars_.quality_cost + leader_priority_cost
        )

        vars_.quality_cost = total_quality

    # -----------------------------------------------------------------------
    # EFFICIENCY OBJECTIVE
    # Minimise number of distinct modules per staff
    # -----------------------------------------------------------------------
    switching_costs = []

    for s in instance.staff_ids:

        touched_modules = []

        for m in instance.module_ids:

            touched = model.NewBoolVar(f"{s}_touches_{m}")

            practical_vars = [
                vars_.practical_assignment[(s2, m2, g)]
                for (s2, m2, g) in vars_.practical_assignment
                if s2 == s and m2 == m
            ]

            if practical_vars:
                model.AddMaxEquality(
                    touched,
                    [vars_.module_leader[(s, m)]] + practical_vars
                )
            else:
                model.Add(touched == vars_.module_leader[(s, m)])

            touched_modules.append(touched)

        count_modules = model.NewIntVar(
            0, len(instance.module_ids), f"{s}_module_count"
        )

        model.Add(count_modules == sum(touched_modules))

        switching_costs.append(count_modules)

    vars_.efficiency_cost = model.NewIntVar(
        0,
        len(instance.staff_ids) * len(instance.module_ids),
        "efficiency_cost"
    )

    model.Add(vars_.efficiency_cost == sum(switching_costs))



def add_baseline_optimisation(model, vars_, instance):
    """
    Balanced multi-objective optimisation
    """

    model.Minimize(
          10 * vars_.fairness_cost
        + 5 * vars_.quality_cost
        + 2 * vars_.efficiency_cost
        - 1 * vars_.total_research
    )

def add_prioritised_optimisation(model, vars_, instance):
    """
    Prioritised optimisation:
    Primary → Fairness & Quality
    Secondary → Efficiency & Research
    """

    primary = 20 * vars_.fairness_cost + 15 * vars_.quality_cost
    secondary = 5 * vars_.efficiency_cost - vars_.total_research

    model.Minimize(primary + secondary)

# --------------------------------------------------------------------
# Solution Generator
# --------------------------------------------------------------------
def extract_solution(solver, vars_, instance):

    # ----------------------------------------------------------------
    # MODULE LEADERS
    # ----------------------------------------------------------------
    module_leaders = {}
    for (s, m), var in vars_.module_leader.items():
        if solver.Value(var) == 1:
            module_leaders[m] = s

    # ----------------------------------------------------------------
    # PRACTICALS
    # -----------------------------------------------------------------
    practicals = {}

    for (s, m, g), var in vars_.practical_assignment.items():
        if solver.Value(var) == 1:
            if m not in practicals:
                practicals[m] = []
            practicals[m].append({
                "group_index": g,
                "staff_id": s
            })

    # -------------------------------------------------------------------
    # SUPERVISION 
    # -------------------------------------------------------------------
    supervision = {}

    for (s, st), var in vars_.supervision.items():
        if solver.Value(var) == 1:
            supervision[st] = s

    # -------------------------------------------------------------------
    # STAFF WORKLOAD
    # -------------------------------------------------------------------
    staff_workload = {}

    for s in instance.staff_ids:
        staff_workload[s] = {
            "teaching": solver.Value(vars_.teaching_minutes[s]),
            "admin": solver.Value(vars_.admin_minutes[s]),
            "research": solver.Value(vars_.research_minutes[s]),
            "total": solver.Value(vars_.total_minutes[s]),
        }

    return WorkloadOutput(
        module_leaders=module_leaders,
        practicals=practicals,
        supervision=supervision,
        staff_workload=staff_workload
    )

def apply_search_strategy(model: cp_model.CpModel, vars_: DecisionVars):

    # --------------------------------------------------------------
    # Deciding Module Leader
    # --------------------------------------------------------------
    model.AddDecisionStrategy(
        list(vars_.module_leader.values()),
        cp_model.CHOOSE_FIRST,
        cp_model.SELECT_MAX_VALUE
    )
    
    # ---------------------------------------------------------------
    # Then Assign Practicals
    # ---------------------------------------------------------------
    model.AddDecisionStrategy(
        list(vars_.practical_assignment.values()),
        cp_model.CHOOSE_FIRST,
        cp_model.SELECT_MAX_VALUE
    )
    
    # -----------------------------------------------------------------
    # Then supervision
    # -----------------------------------------------------------------
    model.AddDecisionStrategy(
        list(vars_.supervision.values()),
        cp_model.CHOOSE_FIRST,
        cp_model.SELECT_MAX_VALUE
    )

def solve(instance: InstanceData, config: SolverConfig) -> WorkloadOutput:

    # -----------------------------------------------------------------------
    # Required entrypoint for autograder.
    # -----------------------------------------------------------------------
    print(' defining decision variables and building the model ')
    model, vars_ = build_model(instance)

    print(' defining soft constraints ')
    add_objectives(model=model, vars_=vars_, instance=instance)

    if config.baseline:
        print(' applying baseline optimisation ')
        add_baseline_optimisation(model, vars_, instance)
    else:
        print(' applying priorities over objectives ')
        add_prioritised_optimisation(model, vars_, instance)

    if config.heuristics:
        print(' applying decision strategies ')
        apply_search_strategy(model, vars_)

    print(' configuring solver to experiment settings ')
    solver = config_solver(model, config)

    print(' solving... ')
    status = solver.Solve(model)

    print(f' Status: {solver.StatusName(status)} ')

    if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
        print(' generating solutions ')
        return extract_solution(solver, vars_, instance)
    else:
        print("No solution found!")
        return WorkloadOutput()
    
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", type=str, default=None, help="Path to instance JSON")
    ap.add_argument("--config", type=str, default=None, help="Path to config JSON")
    ap.add_argument("--out", type=str, default="allocation.json", help="Output JSON path")
    ap.add_argument("--demo", action="store_true", help="Write a full toy output JSON without solving")
    args = ap.parse_args()

    if args.instance is None or args.config is None or args.out is None:
        raise RuntimeError("please provide a correct path for instance, config and workloadoutput path")
    
    # -------------------------------------------------------------------------
    # Assuming interface.py handles loading these correctly
    # -------------------------------------------------------------------------
    instance = InstanceData.from_json(Path(args.instance)) 
    config = SolverConfig.from_json(Path(args.config))
    out_path = Path(args.out)

    output = solve(instance=instance, config=config)
    output.to_json(path=out_path)


if __name__ == "__main__":
    main()
