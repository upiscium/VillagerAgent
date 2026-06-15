set shell := ["bash", "-cu"]

default:
    just --list

validate:
    python -m compileall -q benchmarks/craft env

test:
    pytest

check: validate test
