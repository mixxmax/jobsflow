# Frozen legacy materials chain

This directory is a quarantine marker for the retired materials workflow.
The legacy implementation is retained in its historical modules for package
inspection and migration/rollback work, but it is not a product entrypoint.

The only supported materials path is:

```text
python3 -m tools.workflow materials ...
  -> tools.workflow.engine
  -> tools.workflow.adapters.materials (vNext dispatch only)
  -> tools.workflow.materials_vnext
```

The gateway forces `materials_engine=vnext` for `materials`, `audit`,
`format`, and `apply`. Direct calls that try to select the old adapter fail
closed with a `legacy_materials_*_entrypoint_disabled` blocker. Do not add new
features to the retired modules; changes there are permitted only for an
explicit migration or rollback tool.

The vNext chain owns the current contracts, state transitions, baseline
compiler, independent CV/CL content audit, template renderer, PDF conversion,
and final mechanical gate.
