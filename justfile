set shell := ["bash", "-cu"]

default:
    just --list

validate:
    python -m compileall -q benchmarks/craft

test:
    pytest

check: validate test
