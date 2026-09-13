from benchmarks.minecraft.k12_live_fixture import build_live_fixture, build_live_schedule, qualification_ids


def test_live_schedule_is_exact_and_deterministic():
    schedule = build_live_schedule()
    assert len(schedule) == 5
    assert schedule == build_live_schedule()
    assert qualification_ids() == tuple(
        f"K12Q-S{s}-T1-N1-{arm}" for s, arms in ((1, "ARS"), (2, "RSA"),
        (3, "SAR"), (4, "ASR"), (5, "RAS")) for arm in arms)


def test_live_contracts_have_final_action_mappings():
    assert build_live_fixture("S2", 1, 1).contract == "Option B: facing legality only"
    s4 = build_live_fixture("S4", 2, 3)
    assert "unique" in s4.contract and "damage" not in dict(s4.arguments)
    s5 = build_live_fixture("S5", 2, 1)
    assert s5.mapping == "post_hand" and "FastAPI" in s5.contract
    assert s5.arguments[0][1] != s5.arguments[1][1]


def test_all_thirty_descriptors_are_deterministic_and_mapped():
    values = [build_live_fixture(s, t, n) for s in ("S1", "S2", "S3", "S4", "S5")
              for t in (1, 2) for n in (1, 2, 3)]
    assert len({item.cell_id for item in values}) == 30
    assert {item.mapping for item in values} == {
        "post_dig", "post_place", "post_move_to_pos", "post_attack", "post_hand",
    }
    centers=[item.geometry.observation_center for item in values]
    assert len(set(centers))==30
    assert all(abs(left[0]-right[0])>32 for index,left in enumerate(centers) for right in centers[index+1:])
    for fixture in values:
        low,high=fixture.geometry.region_bounds
        for point in (fixture.geometry.actor_start,fixture.geometry.target,fixture.geometry.support):
            assert all(low[i]<=point[i]<=high[i] for i in range(3))
        covered={(x//16,z//16) for x,z in fixture.geometry.force_loaded_chunks}
        assert (fixture.geometry.target[0]//16,fixture.geometry.target[2]//16) in covered
        assert all(x%16==0 and z%16==0 for x,z in fixture.geometry.force_loaded_chunks)

def test_all_final_descriptors_generate_geometry_bound_reset_plans():
    from benchmarks.minecraft.k12_live_reset import reset_plan
    for stratum in ("S1","S2","S3","S4","S5"):
        for template in (1,2):
            for seed in (1,2,3):
                fixture=build_live_fixture(stratum,template,seed)
                from benchmarks.minecraft.k12_guarded_backend import K12AuthenticatedProfile
                from benchmarks.minecraft.k12_live_state import ParentPlanAuthority
                from benchmarks.minecraft.k12_runtime_profile import load_k12_live_runtime_profile
                authority=ParentPlanAuthority(K12AuthenticatedProfile.from_runtime_profile(load_k12_live_runtime_profile()),"c")
                plan=reset_plan(stratum,authority=authority,cell=fixture.cell_id,
                    reset_token=f"t{template}n{seed}",generation=1,fixture=fixture)
                text=";".join(command.text for command in plan.commands)
                assert f"{fixture.geometry.actor_start[0]} {fixture.geometry.actor_start[1]} {fixture.geometry.actor_start[2]}" in text
                if stratum in {"S4","S5"}: assert "distance=..16" in text
