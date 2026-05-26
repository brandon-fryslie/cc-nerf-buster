# Package marker. Lets `importlib.import_module` and similar tooling treat
# this directory as a Python package once it is on `sys.path` (the
# script-mode invocation that probe.sh / with-proxy.sh use auto-handles
# that path setup). Does NOT make `tools/capacity-probe/` importable as
# `tools.capacity_probe.<module>` from a parent directory — the hyphen in
# the directory name is not a legal Python identifier, so a future test
# harness wanting cross-directory imports needs either a directory rename
# (capacity-probe → capacity_probe, touches justfile + probe.sh + several
# shell scripts) or a sys.path insertion shim. Both are out of scope here.
