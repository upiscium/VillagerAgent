from benchmarks.minecraft.k12_trace import TraceChain, GENESIS_DIGEST, TRACE_SCHEMA

def test_trace_is_parent_owned_hash_chain():
    chain = TraceChain(clock_ns=lambda: 3)
    record = chain.append(worker_id="w", event="cell_started", payload={}, message={"x": 1})
    assert record.previous_digest == GENESIS_DIGEST
    assert record.schema == TRACE_SCHEMA and chain.verify()
