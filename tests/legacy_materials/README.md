# Legacy materials tests

Historical tests for the retired materials adapter are kept for reference and
for explicit migration/rollback work. They are not part of the product-line
release gate. Current acceptance tests live in `tests/test_materials_vnext*.py`
and the other vNext-specific suites.

Do not update these tests for new product behavior. If a migration needs them,
run them explicitly and label the result as legacy compatibility evidence.
