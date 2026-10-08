from benchmarks.minecraft.k11_shutdown_qualification import _category


def test_shutdown_qualification_classifies_identity_without_argv():
    assert _category({"executable": "node"}) == "Node.js/Mineflayer"
    assert _category({"executable": "python3.10"}) == "Minecraft bridge/client"
    assert _category({"executable": "other"}) == "other"
