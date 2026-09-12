# Vendored dependencies

## `sopcontrol/`

Pinned SOP Control control-plane runtime shipped with JobsFlow so a normal
`git clone` does not require a second download.

- Pin file: `../tools/sopcontrol_pin.txt`
- Upstream: https://github.com/mixxmax/sopcontrol
- Import: JobsFlow’s adapter loads this tree automatically when the package is
  not already installed into the active virtualenv.

Optional explicit install (same pin, editable):

```bash
python3 -m pip install -e vendor/sopcontrol
```
