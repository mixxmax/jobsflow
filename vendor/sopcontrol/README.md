# Vendored SOP Control (runnable pin)

Version: 0.3.0 (Beta)
Pin: 2eab4a2586e382bfbb4d577fb48c12dec2d3acf8

Includes: `sopcontrol/` + `plugins/`, including the model-neutral learning
event, proposal and `/learn` control surface. JobsFlow loads this vendored
snapshot before any site-packages installation.

Install the CLI in the JobsFlow environment with:

```bash
python -m pip install -e vendor/sopcontrol
```

This is a product snapshot, not a second editable source checkout. Update the
snapshot and both pin files together.
